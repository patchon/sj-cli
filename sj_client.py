import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from datetime import datetime
from typing import Any

import httpx

from sj_errors import SJAPIError
from sj_logger import log_json, log_request, log_response

logger = logging.getLogger(__name__)

# Module-level station name → UIC code map (single source of truth).
STATION_MAP = {
    "Stockholm Central": "740000001",
    "Stockholm C": "740000001",
    "Sthlm Central": "740000001",
    "Linköping Central": "740000009",
    "Linköping C": "740000009",
    "Göteborg Central": "740000002",
    "Göteborg C": "740000002",
    "Malmö Central": "740000003",
    "Malmö C": "740000003",
    "Uppsala Central": "740000005",
    "Uppsala C": "740000005",
    "Lund Central": "740000004",
    "Lund C": "740000004",
}


def parse_json_response(response: httpx.Response, url: str) -> None:
    """
    Parses the response body as JSON and handles decoding errors.

    Args:
        response: The httpx response object to parse.
        url: The URL that was requested, used for error context.

    Raises:
        httpx.HTTPStatusError: If JSON decoding fails and the http status code
            is a non-2xx error, or if the JSON decoding succeeds but we haven't
            been able to catch the error.
        SJAPIError: If the json contains a status key which isn't 200, or if
            it contains the key errorCode or error.
        Exception: If JSON decoding fails despite a http 2xx success status
            code.

    """
    logger.debug(f"parsing response ({response.status_code}) from {url} as json")

    try:
        resp_json = response.json()
    except json.JSONDecodeError:
        # Handle no/invalid json delivered with a non 2xx http status code
        response.raise_for_status()

        # Handle no/invalid json delivered with a 2xx http status code
        msg = (
            f"failed to decode response from {url}, "
            f"body [raw]: {response.text[:1200]} <truncated after 1200 chars> "
            f"..."
        )
        raise Exception(msg) from None
    logger.debug(f"parsed json: as {log_json(resp_json)}")

    # Make sure the status key is 200 if it exists
    status = resp_json.get("status")
    if status:
        logger.debug(f"found status code in json : {status}")
        if str(status) != "200":
            logger.warning(log_json(resp_json))
            raise SJAPIError(resp_json)
    else:
        logger.debug("no status code found in json")

    # If we have a errorCode key,
    if "errorCode" in resp_json or "error" in resp_json:
        logger.warning(log_json(resp_json))
        raise SJAPIError(resp_json)

    # If we have a valid json in the response, make sure we have a valid
    # http status code, otherwise raise an exception (we are getting a json
    # with a non 200 http status code, but we don't have the logic to handle
    # it).
    response.raise_for_status()

    # If we get here, we have a a valid 2xx http response with a json that
    # we consider is a success.


class RetryTransport(httpx.BaseTransport):
    """
    Transport wrapper that retries on transient failures.

    Idempotent requests (GET/HEAD/OPTIONS) are retried on 502/503, timeouts
    and connection errors with exponential backoff (1s, 2s, 4s). Other
    methods (POST/PATCH — booking creation, checkout, cancellation) are
    retried only when the request provably never reached the server
    (connection error / connect timeout): a 502 or read timeout after a POST
    may mean the server already acted on it, and a retry could create a
    duplicate provisional booking. 4xx responses are never retried.
    """

    RETRYABLE_STATUS_CODES = {502, 503}
    RETRY_DELAYS = [1, 2, 4]
    IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, transport: httpx.BaseTransport) -> None:
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send request with retry logic for transient failures."""
        idempotent = request.method.upper() in self.IDEMPOTENT_METHODS
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            try:
                response = self._transport.handle_request(request)
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                # Connection never established → the request was not sent → safe
                # to retry for any method. Otherwise only idempotent methods.
                never_sent = isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
                if (idempotent or never_sent) and attempt < len(self.RETRY_DELAYS):
                    delay = self.RETRY_DELAYS[attempt]
                    logger.warning(
                        f"{type(e).__name__} for {request.method} {request.url}, "
                        f"retrying in {delay}s (attempt {attempt + 1}/{len(self.RETRY_DELAYS)})"
                    )
                    time.sleep(delay)
                    continue
                raise

            if (
                response.status_code in self.RETRYABLE_STATUS_CODES
                and idempotent
                and attempt < len(self.RETRY_DELAYS)
            ):
                delay = self.RETRY_DELAYS[attempt]
                logger.warning(
                    f"got {response.status_code} from {request.method} {request.url}, "
                    f"retrying in {delay}s (attempt {attempt + 1}/{len(self.RETRY_DELAYS)})"
                )
                time.sleep(delay)
                continue
            return response

        # Unreachable: the loop either returns a response or re-raises.
        raise AssertionError("retry loop exited without a result")

    def close(self) -> None:
        """Close the underlying transport."""
        self._transport.close()


class SJClient:
    """
    A client for interacting with the SJ (sj.se) via Azure B2C authentication.
    """

    PATH_B2C_POLICY = "B2C_1A_signup_signin_SE"
    URL_AUTH_BASE = "https://id.sj.se/customeridsj.onmicrosoft.com"
    URL_AUTH_FLOW_BASE = f"{URL_AUTH_BASE}/{PATH_B2C_POLICY}"
    AUTH_BASE = f"{URL_AUTH_FLOW_BASE}/oauth2/v2.0/authorize"

    CLIENT_ID = "9e44e5be-8c3b-4446-a007-4d0734fdf762"
    H_ACCEPT_BROWSER = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    )
    H_ACCEPT_JSON = "application/json"
    H_CONTENT_TYPE_JSON = "application/json"
    H_CONTENT_TYPE_FORM = "application/x-www-form-urlencoded; charset=UTF-8"
    H_OCP_APIM_SUB_KEY = "d6625619def348d38be070027fd24ff6"
    H_SEC_CH_UA = '"Google Chrome";v="120", "Chromium";v="120", "Not?A_Brand";v="24"'
    H_SEC_CH_UA_MOBILE = "?0"
    H_SEC_CH_UA_PLATFORM = '"macOS"'
    H_USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    REDIRECT_URI = "https://www.sj.se/logga-in/hantera"
    SCOPE = (
        "https://customeridsj.onmicrosoft.com/"
        "3ad29c3a-af17-4cca-8256-c15941d36c45/user_impersonation "
        "openid profile offline_access"
    )
    URL_SJ = "https://www.sj.se"
    URL_SJ_ID = "https://id.sj.se"
    URL_API = "https://prod-api.adp.sj.se/public"
    URL_API_BOOKING = f"{URL_API}/sales/booking/v3"
    URL_API_SECURE_BOOKING = f"{URL_API}/sales/secure/booking/v3"
    X_CLIENT_VERSION = "20251217.0004-prod"

    def __init__(self) -> None:
        logger.debug("initializing SJClient")

        # Create a new client instance with retry transport
        base_transport = httpx.HTTPTransport()
        self.client = httpx.Client(
            transport=RetryTransport(base_transport),
            http2=False,
            follow_redirects=True,
            event_hooks={"request": [log_request], "response": [log_response]},
        )

        # Initialize client headers
        self.client.headers.update(
            {
                "User-Agent": self.H_USER_AGENT,
                "Accept": self.H_ACCEPT_BROWSER,
            }
        )
        logger.debug(
            f"initialized client headers with {log_json(dict(self.client.headers))}"
        )

        # Initialize client credentials
        self.code_verifier = self._generate_code_verifier()
        self.code_challenge = self._generate_code_challenge(self.code_verifier)
        self.state = secrets.token_urlsafe(16)
        self.nonce = secrets.token_urlsafe(16)

        # B2C context, filled in step by step during the login flow. All of it
        # lives here so reset() (which re-runs __init__) clears it.
        self.csrf_token = ""
        self.trans_id = ""
        self.page_view_id = ""
        self.b2c_api_type = ""
        self.last_response: httpx.Response | None = None
        self._confirmed_url = ""
        self._verified_device_id = ""
        self._verified_device_unique_id = ""

    def reset(self) -> None:
        """Reset client state for a fresh login attempt."""
        self.__init__()

    def export_cookies(self) -> list[dict[str, str | None]]:
        """Export the client's cookies as a serializable list."""
        cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
            }
            for cookie in self.client.cookies.jar
        ]
        logger.debug(f"exported {len(cookies)} cookies")
        return cookies

    def import_cookies(self, cookies: list[dict[str, str | None]]) -> None:
        """Import previously exported cookies into the client."""
        for c in cookies:
            self.client.cookies.set(
                c["name"] or "",
                c["value"] or "",
                domain=c.get("domain") or "",
                path=c.get("path") or "/",
            )
        logger.debug(f"imported {len(cookies)} cookies")

    #
    # PUBLIC
    #

    def try_silent_login(self) -> str | None:
        """
        Attempt SSO-based silent login using existing cookies.

        Sends the authorize request without following redirects. If the
        x-ms-cpim-sso cookie is valid, the server returns a 302 with an
        auth code directly — no credentials, no MFA needed.

        Returns:
            The authorization code if silent login succeeded, None otherwise.

        """
        logger.info("attempting silent login via SSO cookie ...")

        params = {
            "client_id": self.CLIENT_ID,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "nonce": self.nonce,
            "redirect_uri": self.REDIRECT_URI,
            "response_type": "code",
            "scope": self.SCOPE,
            "state": self.state,
            "prompt": "none",
        }

        resp = self.client.get(
            self.AUTH_BASE, params=params, follow_redirects=False
        )
        logger.info(f"silent login response code: {resp.status_code}")

        if resp.status_code in (301, 302, 303):
            auth_code = self._extract_auth_code(resp)
            if auth_code:
                logger.info("silent login succeeded — SSO cookie is valid")
                return auth_code

        logger.info("silent login failed — SSO cookie expired or missing")
        return None

    def initiate_login(self, email: str, password: str) -> bool:
        """
        Orchestrates the initial login sequence for SJ B2C authentication.

        This method coordinates the first phase of authentication by performing
        the following steps:
        1. Navigates to the login page to extract CSRF and transaction tokens.
        2. Submits user credentials.
        3. Confirms the login stage with the provider.
        4. Sends a generated device fingerprint.
        5. Advances the sequence past credential verification.

        Args:
            email: The user's email address.
            password: The user's password.

        Returns:
            True if MFA (SMS) is required, False if B2C skipped MFA.

        Raises:
            Exception: If any part of the sequence fails, including network
                issues, failure to parse tokens, or if credential submission
                returns an error.
            HTTPStatusError: If the HTTP status code is not 200.

        """
        logger.info("starting login sequence ...")

        # 1. Fetch login page
        self._fetch_login_page()

        # 2. Submit credentials
        self._submit_credentials(email, password)

        # 3. Confirm Login
        self._confirm_login_stage()

        # 4. Device Fingerprint
        self._send_device_fingerprint()

        # 5. Advance past credential verification
        mfa_required = self._advance_to_mfa()

        logger.info("login sequence initiated successfully")
        return mfa_required

    def finalize_login(self) -> str | None:
        """
        Finalize the login flow and extract the authorization code.

        Completes device registration and extracts the authorization code
        from the final redirect.

        Returns:
            The authorization code string if successful, None otherwise.

        Raises:
            Exception: If required context is missing or the flow fails.

        """
        logger.info("finalizing login flow")

        # Ensure session state is present before proceeding
        self._verify_attributes(["csrf_token", "trans_id", "page_view_id"])

        # Use the response from verify_sms (Phonefactor/confirmed)
        # This page contains the device registration SA_FIELDS
        resp = self.last_response
        if resp is None:
            raise Exception("missing last_response, login sequence was not run")

        if "SA_FIELDS" not in resp.text:
            logger.error("missing SA_FIELDS in response")
            raise Exception("missing SA_FIELDS in response")

        logger.info("performing device registration ...")

        # Parse settings and update instance state
        self._extract_b2c_settings(resp.text)
        logger.debug(f"updated transId to: {self.trans_id}")
        logger.debug(f"captured pageViewId to: {self.page_view_id}")

        # Refresh csrf token from the phonefactor confirmed response
        self._get_csrf_token(resp.headers)

        # --- STEP 1: POST device fingerprint to /SelfAsserted ---
        url_fingerprint = (
            f"{self.URL_AUTH_FLOW_BASE}/SelfAsserted"
            f"?tx={self.trans_id}&p={self.PATH_B2C_POLICY}"
        )

        # Extract pre-filled device values from the SA_FIELDS if available,
        # otherwise generate random ones
        device_user_id = self._extract_sa_field(resp.text, "deviceUserId")
        device_id = self._extract_sa_field(resp.text, "deviceId")
        device_unique_id_user = self._extract_sa_field(
            resp.text, "deviceUniqueIdUserId"
        )
        device_unique_id = self._extract_sa_field(resp.text, "deviceUniqueId")

        payload = {
            "request_type": "RESPONSE",
            "deviceUserId": device_user_id
            or base64.b64encode(secrets.token_bytes(24)).decode(),
            "deviceId": device_id
            or base64.b64encode(secrets.token_bytes(24)).decode(),
            "deviceUniqueIdUserId": device_unique_id_user
            or base64.b64encode(secrets.token_bytes(24)).decode(),
            "deviceUniqueId": device_unique_id
            or base64.b64encode(secrets.token_bytes(24)).decode(),
        }

        headers = {
            "Accept": self.H_ACCEPT_JSON,
            "Content-Type": self.H_CONTENT_TYPE_FORM,
            "Origin": self.URL_SJ_ID,
            "Referer": str(resp.url),
            "User-Agent": self.H_USER_AGENT,
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": self.H_SEC_CH_UA,
            "sec-ch-ua-mobile": self.H_SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
        }

        logger.info(f"sending device fingerprint to {url_fingerprint} ...")
        logger.debug(
            f"posting payload {log_json(payload)} and headers "
            f"{log_json(headers)} to {url_fingerprint}"
        )

        fp_resp = self.client.post(url_fingerprint, data=payload, headers=headers)
        logger.info(f"fingerprint response http code: {fp_resp.status_code}")
        logger.debug(f"fingerprint response content: {fp_resp.content}")

        # Update csrf token from response
        self._get_csrf_token(fp_resp.headers)

        # --- STEP 2: GET api/SelfAsserted/confirmed ---
        # This endpoint returns a 302 redirect to sj.se with the auth code.
        # We must NOT follow the redirect, so we can extract the code.
        logger.debug("advancing to final confirmation step ...")

        diags = (
            f'{{"pageViewId":"{self.page_view_id}",'
            f'"pageId":"SelfAsserted","trace":[]}}'
        )
        encoded_diags = urllib.parse.quote(diags)
        encoded_csrf_token = urllib.parse.quote(self.csrf_token)

        url_confirm = (
            f"{self.URL_AUTH_FLOW_BASE}/api/SelfAsserted/confirmed"
            f"?csrf_token={encoded_csrf_token}&tx={self.trans_id}"
            f"&p={self.PATH_B2C_POLICY}&diags={encoded_diags}"
        )

        logger.info(f"requesting confirmation from: {url_confirm} ...")
        confirm_resp = self.client.get(
            url_confirm, headers=headers, follow_redirects=False
        )
        logger.info(f"confirmation response code: {confirm_resp.status_code}")

        # Extract the authorization code from the redirect
        return self._extract_auth_code(confirm_resp)

    def refresh_token(self, refresh_token: str) -> dict[str, Any] | None:
        """
        Attempt to refresh the access token using the refresh_token.

        Args:
            refresh_token: The refresh token string.

        Returns:
            The new token data as a dictionary if successful, None otherwise.

        """
        url_token = f"{self.URL_AUTH_FLOW_BASE}/oauth2/v2.0/token"

        data = {
            "client_id": self.CLIENT_ID,
            "scope": self.SCOPE,
            "redirect_uri": self.REDIRECT_URI,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        logger.info("attempting to refresh token ...")

        resp = self.client.post(url_token, data=data)
        logger.info(f"refresh response http code: {resp.status_code}")

        # Try to parse json response
        try:
            parse_json_response(resp, url_token)
        # If we have an error from the api
        except SJAPIError as e:
            try:
                err_data = e.args[0]
                if isinstance(err_data, dict):
                    err_desc = err_data.get("error_description", "")
                    if "The provided grant has expired" in err_desc:
                        iss_match = re.search(r"Grant issued time: (\d+)", err_desc)
                        exp_match = re.search(r"Grant expiration time: (\d+)", err_desc)

                        if iss_match and exp_match:
                            iss_dt = datetime.fromtimestamp(int(iss_match.group(1)))
                            exp_dt = datetime.fromtimestamp(int(exp_match.group(1)))
                            logger.warning(
                                f"token refresh failed, grant is expired,"
                                f"issued at {iss_dt} and expired: {exp_dt}"
                            )
                            logger.info(
                                "token refresh grant is expired, will do full login"
                            )
                            return None
            except Exception:
                pass

            logger.error(f"api error refreshing token: {e}")
            return None
        except Exception as e:
            logger.error(f"error refreshing token: {e}")
            return None

        return resp.json()

    def sms_trigger(self) -> None:
        """
        Explicitly requests the SMS code using Phonefactor endpoint.

        Raises:
            Exception: If required context is missing or if rate limits are
                detected.

        """
        logger.info("triggering sms ...")

        # Ensure session state is present before proceeding
        self._verify_attributes(["csrf_token", "trans_id", "page_view_id"])

        url_phonefactor = f"{self.URL_AUTH_FLOW_BASE}/Phonefactor/verify"

        tx_param = self.trans_id
        url_sms = f"{url_phonefactor}?tx={tx_param}&p={self.PATH_B2C_POLICY}"
        payload = {
            "auth_type": "onewaysms",
            "id": "1",
            "request_type": "VERIFICATION_REQUEST",
        }

        headers = {
            "Accept": self.H_ACCEPT_JSON,
            "Content-Type": self.H_CONTENT_TYPE_FORM,
            "Origin": self.URL_SJ_ID,
            "Referer": self.AUTH_BASE,
            "User-Agent": self.H_USER_AGENT,
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "sec-ch-ua": self.H_SEC_CH_UA,
            "sec-ch-ua-mobile": self.H_SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

        logger.debug(
            f"posting payload {log_json(payload)} and headers "
            f"{log_json(headers)} to {url_sms}"
        )

        # Post the payload and headers to the url_credentials endpoint
        resp = self.client.post(url_sms, data=payload, headers=headers)
        logger.info(f"sms trigger response code: {resp.status_code}")

        # Check for rate limiting in the text
        if "error_phone_throttled" in resp.text:
            logger.error(f"rate limit detected (error_phone_throttled): {resp.text}")
            raise Exception("rate limit detected (error_phone_throttled)")

        # Try to parse json response
        parse_json_response(resp, url_sms)

        # Set / update csrf
        self._get_csrf_token(resp.headers)

    def sms_verify(self, code: str) -> None:
        """
        Verifies the SMS code via Phonefactor.

        Args:
            code: The verification code entered by the user.

        Raises:
            Exception: If required context is missing.

        """
        logger.info("verifying code ...")

        # Ensure session state is present before proceeding
        self._verify_attributes(["csrf_token", "trans_id", "page_view_id"])

        url_phonefactor = f"{self.URL_AUTH_FLOW_BASE}/Phonefactor/verify"

        tx_param = self.trans_id
        url_sms = f"{url_phonefactor}?tx={tx_param}&p={self.PATH_B2C_POLICY}"
        payload = {"request_type": "VALIDATION_REQUEST", "verification_code": code}

        headers = {
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": self.H_ACCEPT_JSON,
            "Content-Type": self.H_CONTENT_TYPE_FORM,
            "Origin": self.URL_SJ_ID,
            "Referer": self.AUTH_BASE,
            "User-Agent": self.H_USER_AGENT,
            "sec-ch-ua": self.H_SEC_CH_UA,
            "sec-ch-ua-mobile": self.H_SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

        logger.debug(
            f"posting payload {log_json(payload)} and headers "
            f"{log_json(headers)} to {url_sms}"
        )

        # Post the payload and headers to the url_credentials endpoint
        resp = self.client.post(url_sms, data=payload, headers=headers)
        logger.info(f"sms verification response code: {resp.status_code}")

        # Try to parse json response
        parse_json_response(resp, url_sms)

        # Set / update csrf
        self._get_csrf_token(resp.headers)

        logger.info("sms verification successful, completing phonefactor step ...")

        # Call Phonefactor confirmed endpoint
        url_confirm_base = f"{self.URL_AUTH_FLOW_BASE}/api/Phonefactor/confirmed"
        tx_param = self.trans_id
        url_confirm_full = (
            f"{url_confirm_base}?csrf_token={urllib.parse.quote(self.csrf_token)}"
            f"&tx={tx_param}&p={self.PATH_B2C_POLICY}"
        )

        confirmed_resp = self.client.get(url_confirm_full)
        logger.info(
            f"phonefactor verification response code: {confirmed_resp.status_code}"
        )
        confirmed_resp.raise_for_status()

        # Save for the next step (finalize_login reads this)
        self.last_response = confirmed_resp

        # Refresh csrf token from the confirmed response
        self._get_csrf_token(confirmed_resp.headers)

    #
    # PRIVATE
    #

    def _extract_auth_code(self, resp: httpx.Response) -> str | None:
        """
        Extracts the authorization code from a B2C response.

        Handles both 302 redirects (Location header) and responses where
        redirects were followed (checking history).

        Args:
            resp: The httpx response to extract the code from.

        Returns:
            The authorization code string, or None if not found.

        """
        # Case 1: 302 redirect with Location header containing the code
        if resp.status_code == 302:
            location = resp.headers.get("location", "")
            logger.debug(f"got 302 redirect to: {re.sub(r'code=[^&]+', 'code=***', location)}")
            if "code=" in location:
                parsed = urllib.parse.urlparse(location)
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                if code:
                    logger.debug("extracted auth code from redirect location")
                    return code

        # Case 2: Redirect was followed, check history
        if resp.history:
            for r in resp.history:
                loc = r.headers.get("location", "")
                if "code=" in loc:
                    parsed = urllib.parse.urlparse(loc)
                    params = urllib.parse.parse_qs(parsed.query)
                    code = params.get("code", [None])[0]
                    if code:
                        logger.debug("extracted auth code from redirect history")
                        return code

        # Case 3: Check the final URL itself
        url_final = str(resp.url)
        if "code=" in url_final:
            parsed = urllib.parse.urlparse(url_final)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if code:
                logger.debug("extracted auth code from final url")
                return code

        logger.error(
            f"no authorization code found in response "
            f"(status={resp.status_code}, url={resp.url})"
        )
        return None

    def _extract_sa_field(self, html: str, field_id: str) -> str | None:
        """
        Extracts a pre-filled value from the SA_FIELDS JavaScript variable.

        Args:
            html: The HTML content containing SA_FIELDS.
            field_id: The field ID to look for (e.g. "deviceUserId").

        Returns:
            The pre-filled value string, or None if not found.

        """
        # Look for the field's PRE value in the SA_FIELDS JSON
        pattern = rf'"ID":"{field_id}"[^}}]*?"PRE":"([^"]*)"'
        match = re.search(pattern, html)
        if match:
            value = match.group(1)
            logger.debug(f"extracted SA_FIELD {field_id}: {value[:20]}...")
            return value
        return None

    def _advance_to_mfa(self) -> bool:
        """
        Advances the authentication flow past credential verification.

        Sends a GET request to proceed to the next stage. Depending on B2C's
        risk assessment, this either lands on a Phonefactor (MFA/SMS) page
        or a SelfAsserted (device registration) page that skips MFA entirely.

        Returns:
            True if MFA is required (Phonefactor page), False if MFA is
            skipped (SelfAsserted/device-registration page).

        """
        logger.info("advancing to mfa orchestration ...")

        url_mfa = f"{self.URL_AUTH_FLOW_BASE}/api/SelfAsserted/confirmed"
        params = {
            "csrf_token": self.csrf_token,
            "p": self.PATH_B2C_POLICY,
            "tx": self.trans_id,
        }

        resp = self.client.get(url_mfa, params=params)
        logger.debug(f"mfa advance response code: {resp.status_code}")

        # Raise exception if response is a non 2xx
        resp.raise_for_status()

        # Parse settings and update instance state
        self._extract_b2c_settings(resp.text)

        logger.debug(f"updated transId to: {self.trans_id}")
        logger.debug(f"captured pageViewId to: {self.page_view_id}")

        # Set / update csrf
        self._get_csrf_token(resp.headers)

        # Save response for use by finalize_login() when MFA is skipped
        self.last_response = resp

        mfa_required = self.b2c_api_type == "Phonefactor"
        logger.info(
            f"b2c api type: {self.b2c_api_type}, "
            f"mfa {'required' if mfa_required else 'not required (skipping SMS)'}"
        )
        return mfa_required

    def _confirm_login_stage(self) -> None:
        """
        Confirms the login stage with the B2C provider.

        Sends a GET request to the 'confirmed' endpoint to finalize the
        credential submission phase and prepare for device fingerprinting.
        Extracts verified device IDs from SA_FIELDS for use in the
        subsequent device fingerprint step.
        """
        logger.info("fetching 'confirmed' endpoint ...")
        tx_param = self.trans_id

        params = {
            "csrf_token": self.csrf_token,
            "diags": '{"pageViewId":"test","pageId":"CombinedSigninAndSignup","trace":[]}',
            "p": self.PATH_B2C_POLICY,
            "rememberMe": "true",
            "tx": tx_param,
        }

        headers = {
            "User-Agent": self.client.headers["User-Agent"],
            "Accept": self.H_ACCEPT_BROWSER,
            "Origin": self.URL_SJ_ID,
        }

        url_confirm = f"{self.URL_AUTH_FLOW_BASE}/api/CombinedSigninAndSignup/confirmed"
        resp = self.client.get(url_confirm, params=params, headers=headers)
        logger.info(f"confirmation response code: {resp.status_code}")

        # Raise exception if response is a non 2xx
        resp.raise_for_status()

        # Set / update csrf and store confirmation url for referer
        self._get_csrf_token(resp.headers)
        self._confirmed_url = url_confirm

        # Extract verified device IDs from SA_FIELDS — the server populates
        # these when it recognizes the device via SSO cookies. They must be
        # forwarded in the device fingerprint step to skip MFA.
        self._verified_device_id = self._extract_sa_field(
            resp.text, "verifiedDeviceId"
        ) or ""
        self._verified_device_unique_id = self._extract_sa_field(
            resp.text, "verifiedDeviceUniqueId"
        ) or ""
        if self._verified_device_id:
            logger.info("found verified device IDs from server (device is trusted)")
        else:
            logger.info("no verified device IDs from server (new/untrusted device)")

    def _extract_b2c_settings(self, html_content: str) -> None:
        """
        Extracts and parses the SETTINGS JSON blob from the response HTML.

        Args:
            html_content: The raw HTML string containing the 'var SETTINGS' variable.

        Raises:
            ValueError: If the settings blob is missing or contains invalid JSON.

        """
        match = re.search(r"var SETTINGS = (\{.*?\});", html_content)
        if not match:
            logger.error("failed to find 'var SETTINGS' in response")
            raise ValueError("failed to find 'var SETTINGS' in response")

        settings_json = match.group(1)
        try:
            settings = json.loads(settings_json)
        except json.JSONDecodeError as err:
            logger.error("failed to parse settings json: %s", err)
            logger.debug("raw json content: %s", settings_json)
            raise ValueError("failed to decode mfa settings json") from err

        trans_id = settings.get("transId")
        page_view_id = settings.get("pageViewId")

        # Validate required authentication context keys
        if not trans_id:
            logger.error("missing 'transId' in settings: %s", log_json(settings))
            raise ValueError("mfa settings missing 'transId'")

        if not page_view_id:
            logger.error("missing 'pageViewId' in settings: %s", log_json(settings))
            raise ValueError("mfa settings missing 'pageViewId'")

        logger.debug(f"extracted b2c settings: {log_json(settings)}")

        self.trans_id = settings.get("transId")
        self.page_view_id = settings.get("pageViewId")
        self.b2c_api_type = settings.get("api", "")

    def _fetch_login_page(self) -> None:
        """
        Navigates to the initial login page to establish session state.

        This method sends a GET request to the authorization endpoint, updates
        the CSRF token, and extracts the 'transId' necessary for subsequent
        steps.

        Raises:
            Exception: If the 'transId' cannot be found in the page content.
            HTTPStatusError: If the HTTP status code is not 200.

        """
        logger.info("fetching login page ...")
        params = {
            "client_id": self.CLIENT_ID,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "nonce": self.nonce,
            "redirect_uri": self.REDIRECT_URI,
            "response_type": "code",
            "scope": self.SCOPE,
            "state": self.state,
        }

        # Get page content and verify http status code
        resp = self.client.get(self.AUTH_BASE, params=params)
        resp.raise_for_status()

        # Extract transid
        content = resp.text
        trans_match = re.search(r'"transId":"([^"]+)"', content)
        if not trans_match:
            logger.error(f"failed to fetch transid from login page {content}")
            raise Exception("failed to fetch transid")

        # Set / update csrf and transid
        self._get_csrf_token(resp.headers)
        self.trans_id = trans_match.group(1)
        logger.debug(f"transid: {self.trans_id}")

    def _generate_code_verifier(self) -> str:
        """
        Generates a secure random token for use as a PKCE code verifier.

        Returns:
            A URL-safe base64-encoded random string.

        """
        return secrets.token_urlsafe(32)

    def _generate_code_challenge(self, verifier: str) -> str:
        """
        Generates a PKCE code challenge from the provided verifier.

        Args:
            verifier: The PKCE code verifier string.

        Returns:
            A URL-safe base64-encoded SHA-256 hash of the verifier.

        """
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        logger.debug(f"generated code challenge: {challenge}")
        return challenge

    def _get_csrf_token(self, resp: httpx.Headers) -> None:
        """
        Extracts the CSRF token from response headers and cookies.

        This method attempts to retrieve the CSRF token by checking the provided
        response headers first, followed by the client's cookie jar.

        Args:
            resp: The headers object from an httpx response.

        Raises:
            Exception: If the CSRF token could not be found in either the
                headers or the cookies.

        """
        self._get_csrf_token_from_headers(resp)
        self._get_csrf_token_from_cookie()

        if not self.csrf_token:
            logger.error("csrf token could not be found in headers or cookie")
            raise Exception("csrf token not found in headers or cookie")

    def _get_csrf_token_from_cookie(self) -> None:
        """
        Extracts and updates the CSRF token from the client's cookies.

        Searches the client's cookie jar for 'x-ms-cpim-csrf'. If the cookie
        is present and its value differs from the current `csrf_token`
        attribute, the attribute is updated.
        """
        csrf_cookie = self.client.cookies.get("x-ms-cpim-csrf")

        if not csrf_cookie:
            logger.debug("no csrf token found in cookie")
            return

        if csrf_cookie == self.csrf_token:
            logger.debug("csrf token from cookie matches current token")
            return

        logger.debug(f"updating csrf token from cookie: {csrf_cookie[:10]} ...")
        self.csrf_token = csrf_cookie

    def _get_csrf_token_from_headers(self, headers: httpx.Headers) -> None:
        """
        Extracts and updates the CSRF token from the provided HTTP headers.

        Searches the response headers for 'x-ms-cpim-csrf' or 'X-CSRF-TOKEN'.
        If found, updates the instance's `csrf_token` attribute.

        Args:
            headers: The headers object from an httpx response.

        """
        csrf_header = headers.get("x-ms-cpim-csrf") or headers.get("X-CSRF-TOKEN")

        if csrf_header:
            logger.debug(f"got csrf token from headers: {csrf_header[:10]} ...")
            self.csrf_token = csrf_header
        else:
            logger.debug("no csrf token found in headers")

    def _send_device_fingerprint(self) -> None:
        """
        Sends a generated device fingerprint to the provider.

        Constructs a payload with device IDs and submits it to the
        self-asserted endpoint. Includes verified device IDs extracted
        from the confirm stage if available (allows skipping MFA).
        """
        logger.info("sending device fingerprint ...")

        device_user_id = base64.b64encode(secrets.token_bytes(24)).decode()
        device_unique_id = base64.b64encode(secrets.token_bytes(24)).decode()
        unique_id = str(secrets.token_urlsafe(16))

        payload = {
            "deviceUniqueIdUserId": device_unique_id,
            "deviceUserId": device_user_id,
            "request_type": "RESPONSE",
            "uniqueId": unique_id,
            "userAgent": self.client.headers["User-Agent"],
            # Verified device IDs from _confirm_login_stage (empty on a new device)
            "verifiedDeviceId": self._verified_device_id,
            "verifiedDeviceUniqueId": self._verified_device_unique_id,
        }

        headers = {
            "Content-Type": self.H_CONTENT_TYPE_FORM,
            "Origin": self.URL_SJ_ID,
            "Referer": self._confirmed_url,
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }

        url_fingerprint = (
            f"{self.URL_AUTH_FLOW_BASE}/SelfAsserted"
            f"?tx={self.trans_id}&p={self.PATH_B2C_POLICY}"
        )

        logger.debug(
            f"posting payload {log_json(payload)} and headers "
            f"{log_json(headers)} to {url_fingerprint}"
        )

        resp = self.client.post(url_fingerprint, data=payload, headers=headers)
        logger.info(f"fingerprint response code: {resp.status_code}")

        # Raise exception if response is a non 2xx
        resp.raise_for_status()

        # Set / update csrf token
        self._get_csrf_token(resp.headers)

    def _submit_credentials(self, email: str, password: str) -> None:
        """
        Submits the user's email and password to the authentication endpoint.

        Args:
            email: The user's email address.
            password: The user's password.

        Raises:
            Exception: If the server returns a 400 status or error code, or if
                parsing the JSON response fails.

        """
        logger.info("submitting credentials ...")

        headers = {
            "Content-Type": self.H_CONTENT_TYPE_FORM,
            "Origin": self.URL_SJ_ID,
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
        }

        payload = {
            "signInName": email,
            "password": password,
            "request_type": "RESPONSE",
        }

        url_credentials = (
            f"{self.URL_AUTH_FLOW_BASE}/SelfAsserted"
            f"?tx={self.trans_id}&p={self.PATH_B2C_POLICY}"
        )

        logger.debug(
            f"posting payload {log_json(payload)} and headers "
            f"{log_json(headers)} to {url_credentials}"
        )

        # Post the payload and headers to the url_credentials endpoint
        resp = self.client.post(url_credentials, data=payload, headers=headers)
        logger.info(f"login response code: {resp.status_code}")

        # Try to parse json response
        parse_json_response(resp, url_credentials)

        # Set / update csrf
        self._get_csrf_token(resp.headers)

    def _verify_attributes(self, attributes: list[str]) -> None:
        """
        Verifies that the required internal attributes are present and not empty.

        Args:
            attributes: A list of attribute names to check on the current instance.

        Raises:
            Exception: If any of the required attributes are missing or empty.

        """
        for attr in attributes:
            if not getattr(self, attr, None):
                error_msg = f"missing {attr}, can't proceed"
                logger.error(error_msg)
                raise Exception(error_msg)

    def exchange_code(self, code: str) -> dict[str, Any]:
        """
        Exchanges the authorization code for an access token.

        Args:
            code: The authorization code.

        Returns:
            The token response dictionary (access_token, refresh_token, etc).

        Raises:
            httpx.HTTPStatusError: If the token exchange request fails.

        """
        url_token = f"{self.URL_AUTH_FLOW_BASE}/oauth2/v2.0/token"

        data = {
            "client_id": self.CLIENT_ID,
            "scope": self.SCOPE,
            "redirect_uri": self.REDIRECT_URI,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": self.code_verifier,
        }

        logger.info("exchanging code for token ...")
        resp = self.client.post(url_token, data=data)
        if resp.status_code != 200:
            logger.error("token exchange failed:")
            logger.error(resp.text)
            resp.raise_for_status()

        return resp.json()

    def get_membership(self, access_token: str) -> dict[str, Any]:
        """
        Fetches membership info using the access token.

        Args:
            access_token: The OAuth2 access token.

        Returns:
            The membership data dictionary.

        """
        url_api = f"{self.URL_API}/sj-web/customer/v1/Me/Membership"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-account-client",
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "Referer": f"{self.URL_SJ}/",
            "ocp-apim-trace": "false",
        }

        logger.info(f"fetching membership info from {url_api} ...")
        resp = self.client.get(url_api, headers=headers)
        if resp.status_code != 200:
            logger.error(f"membership api failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def get_bookings(
        self, access_token: str, start_date: str, end_date: str, page: int = 0
    ) -> dict[str, Any]:
        """
        Fetches the user's bookings within a date range.

        Args:
            access_token: The OAuth2 access token.
            start_date: Start date (YYYY-MM-DD).
            end_date: End date (YYYY-MM-DD).
            page: The page number to fetch (default 0).

        Returns:
            A dictionary containing the list of bookings and pagination info.

        """
        url_api = f"{self.URL_API_SECURE_BOOKING}/bookings"

        params = {
            "fromPage": str(page),
            "startDate": start_date,  # YYYY-MM-DD
            "endDate": end_date,  # YYYY-MM-DD
            "includeCancelledBookings": "false",
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-account-client",
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "Referer": f"{self.URL_SJ}/",
            "ocp-apim-trace": "false",
        }

        logger.info(
            f"fetching bookings (page {page}) from {start_date} to {end_date} ..."
        )
        resp = self.client.get(url_api, params=params, headers=headers)

        if resp.status_code != 200:
            logger.error(f"bookings api failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def resolve_station(self, station_name: str) -> str:
        """
        Resolves a station name to its internal ID.

        Note: This currently uses a hardcoded map for common stations.

        Args:
            station_name: The name of the station (e.g. "Stockholm C").

        Returns:
            The station ID (UIC code) if found, otherwise the original name.

        """
        # Case insensitive lookup against module-level STATION_MAP
        norm_name = station_name.strip()
        for k, v in STATION_MAP.items():
            if k.lower() == norm_name.lower():
                return v

        logger.warning(
            f"station '{station_name}' not found in station map, using as-is"
        )
        return station_name

    def get_travel_passes(self, access_token: str) -> dict[str, Any]:
        """
        Fetches the user's travel passes.

        Args:
            access_token: The OAuth2 access token.

        Returns:
            A dictionary (or list, depending on API) containing travel passes.

        """
        url_api = f"{self.URL_API_SECURE_BOOKING}/travelpasses"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-booking-client",
            "x-client-version": self.X_CLIENT_VERSION,
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "ocp-apim-trace": "true",
        }

        logger.info("fetching travel passes ...")
        resp = self.client.get(url_api, headers=headers)

        if resp.status_code != 200:
            logger.error(f"travel passes api failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def get_receipt_search(self, booking_id: str) -> list[dict[str, Any]]:
        """
        Searches for receipts associated with a booking ID.

        Args:
            booking_id: The booking ID (e.g. travelPassCreationBookingId).

        Returns:
            A list of receipt summary dicts.

        """
        url_api = f"{self.URL_API_BOOKING}/receipts/search/{booking_id}"

        headers = {
            "Accept": self.H_ACCEPT_JSON,
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "ocp-apim-trace": "true",
            "subscription-id": "sales-web",
            "x-client-name": "sjse-edit-booking-client",
            "Referer": f"{self.URL_SJ}/",
        }
        headers.update(self.generate_trace_headers())

        logger.info(f"searching receipts for booking {booking_id} ...")
        resp = self.client.get(url_api, headers=headers)

        if resp.status_code != 200:
            logger.warning(f"receipt search failed: {resp.status_code}")
            logger.debug(resp.text)
            return []

        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"] if isinstance(data["data"], list) else [data["data"]]
        return data if isinstance(data, list) else [data]

    def search_journey(
        self,
        access_token: str,
        origin_name: str,
        destination_name: str,
        departure_date: str,
        return_date: str | None = None,
        travel_pass_id: str | None = None,
        service_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Searches for a journey between two stations.

        Args:
            access_token: The OAuth2 access token.
            origin_name: The name of the origin station.
            destination_name: The name of the destination station.
            departure_date: The departure date (YYYY-MM-DD).
            return_date: Optional return date (YYYY-MM-DD).
            travel_pass_id: Optional ID of a travel pass to apply.
            service_types: Optional list of allowed service types to filter by.

        Returns:
            A dictionary containing the search results (search IDs).

        """
        url_api = f"{self.URL_API_BOOKING}/search"

        origin_id = self.resolve_station(origin_name)
        dest_id = self.resolve_station(destination_name)

        passenger_data = {"passengerCategory": {"type": "ADULT"}}

        # If we have a travel pass, include it!
        if travel_pass_id:
            passenger_data["travelPass"] = {
                "travelPassId": travel_pass_id,
                "travelPassRole": "HOLDER",
            }

        payload = {
            "origin": origin_id,
            "destination": dest_id,
            "departureDate": departure_date,  # YYYY-MM-DD
            "passengers": [passenger_data],
        }

        if return_date:
            payload["returnDate"] = return_date

        if service_types:
            search_filter = {
                "onlyDirectJourneys": False,
                "allowedServiceTypes": service_types,
            }
            payload["outboundAdditionalSearchFilters"] = search_filter
            if return_date:
                payload["inboundAdditionalSearchFilters"] = search_filter

        # We need the specific Headers for Search (copied from curl)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-booking-client",
            "x-client-version": self.X_CLIENT_VERSION,
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "Referer": f"{self.URL_SJ}/",
        }

        logger.info(
            f"searching journey: {origin_name} ({origin_id}) -> "
            f"{destination_name} ({dest_id}) on {departure_date} ..."
        )
        resp = self.client.post(url_api, json=payload, headers=headers)

        if resp.status_code != 200:
            logger.error(f"search api failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()  # Raise here to catch early

        return resp.json()

    def get_search_results(self, access_token: str, search_id: str) -> dict[str, Any]:
        """
        Fetches the actual departures for a given search ID.

        Args:
            access_token: The OAuth2 access token.
            search_id: The search ID returned by `search_journey`.

        Returns:
            A dictionary containing the list of departures.

        """
        url_api = f"{self.URL_API_BOOKING}/departures/search/{search_id}"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-booking-client",
            "x-client-version": self.X_CLIENT_VERSION,
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "Referer": f"{self.URL_SJ}/",
            "ocp-apim-trace": "true",
        }

        logger.info(f"fetching search results for id {search_id} ...")
        resp = self.client.get(url_api, headers=headers)

        if resp.status_code != 200:
            logger.error(f"search results api failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def get_offers(
        self, access_token: str, departure_id: str, passenger_token: str
    ) -> dict[str, Any]:
        """
        Fetches offers (ticket types/prices) for a specific departure.

        Args:
            access_token: The OAuth2 access token.
            departure_id: The ID of the selected departure.
            passenger_token: The passengerListId from the search response.

        Returns:
            A dictionary containing available offers and prices.

        """
        url_api = f"{self.URL_API_BOOKING}/departures/{departure_id}/offers"
        params = {"passengerListId": passenger_token}

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-booking-client",
            "x-client-version": self.X_CLIENT_VERSION,
            "Referer": f"{self.URL_SJ}/",
        }

        logger.info(f"fetching offers for departure {departure_id} ...")
        resp = self.client.get(url_api, params=params, headers=headers)

        if resp.status_code != 200:
            logger.error(f"offers api failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def generate_trace_headers(self) -> dict[str, str]:
        """
        Generates W3C Trace Context headers (traceparent) and request-id.

        These headers are required to avoid 'trace disabled' (31100) errors
        without triggering 'authorization expired'.

        Returns:
            A dictionary containing 'traceparent' and 'request-id'.

        """
        trace_id = secrets.token_hex(16)  # 32 chars
        span_id = secrets.token_hex(8)  # 16 chars
        traceparent = f"00-{trace_id}-{span_id}-01"
        request_id = f"|{trace_id}.{span_id}"
        # Use lowercase keys to match browser/HAR exactly
        return {"traceparent": traceparent, "request-id": request_id}

    def create_provisional_booking(
        self, access_token: str, offer_id: str, passenger_token: str
    ) -> dict[str, Any]:
        """
        Creates a provisional booking with the selected outbound offer.

        The API requires an outbound offer to create a booking. Inbound legs
        can only be added to an existing booking via add_offer_to_booking.

        Args:
            access_token: The OAuth2 access token.
            offer_id: The offer ID for the outbound journey.
            passenger_token: The passenger list token.

        Returns:
            A dictionary containing the booking details (bookingId, etc).

        """
        url_api = f"{self.URL_API_BOOKING}/bookings/provisional"

        payload = {
            "outboundOfferId": offer_id,
            "passengerToken": passenger_token,
            "usePoints": False,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "ocp-apim-trace": "true",
            "Referer": f"{self.URL_SJ}/",
            "User-Agent": self.H_USER_AGENT,
            "sec-ch-ua": self.H_SEC_CH_UA,
            "sec-ch-ua-mobile": self.H_SEC_CH_UA_MOBILE,
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "x-client-name": "sjse-booking-client",
            "x-client-version": self.X_CLIENT_VERSION,
        }

        # Add required trace headers (W3C standard)
        # Browser sends BOTH ocp-apim-trace AND traceparent.
        headers.update(self.generate_trace_headers())

        logger.info(f"creating provisional booking for offer {offer_id} ...")
        logger.debug(f"creation payload:\n{log_json(payload)}")
        logger.debug(f"creation headers:\n{log_json(headers)}")
        resp = self.client.post(url_api, json=payload, headers=headers)

        if resp.status_code not in [200, 201]:
            logger.error(f"create booking failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def add_offer_to_booking(
        self, access_token: str, booking_id: str, offer_id: str, passenger_token: str
    ) -> dict[str, Any]:
        """
        Adds a secondary offer (e.g. return trip) to an existing provisional booking.

        Args:
            access_token: The OAuth2 access token.
            booking_id: The ID of the provisional booking.
            offer_id: The ID of the offer to add.
            passenger_token: The passenger list token.

        Returns:
            The updated booking dictionary.

        Raises:
            httpx.HTTPStatusError: If the API request fails.

        """
        url_api = f"{self.URL_API_BOOKING}/bookings/provisional/{booking_id}"

        payload = {
            "offerId": offer_id,
            "passengerToken": passenger_token,
            "usePoints": False,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-booking-client",
            "x-client-version": self.X_CLIENT_VERSION,
            "ocp-apim-trace": "true",
            "Referer": f"{self.URL_SJ}/",
            "User-Agent": self.H_USER_AGENT,
        }

        # Add required trace headers (W3C standard)
        headers.update(self.generate_trace_headers())

        logger.info(f"adding offer {offer_id} to booking {booking_id} (patch) ...")
        # NOTE: Using PATCH, not POST
        resp = self.client.patch(url_api, json=payload, headers=headers)

        if resp.status_code not in [200, 201]:
            logger.error(f"add offer (patch) failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def update_booking_customer(
        self,
        access_token: str,
        booking_id: str,
        email: str,
        phone: str,
    ) -> dict[str, Any]:
        """
        Updates customer details for the booking.

        Args:
            access_token: The OAuth2 access token.
            booking_id: The ID of the booking.
            email: The customer's email.
            phone: The customer's phone number.

        Returns:
            The API response dictionary.

        """
        url_api = f"{self.URL_API_BOOKING}/bookings/provisional/{booking_id}/customer"

        payload = {
            "email": email,
            "phoneNumber": phone,
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-booking-client",
            "Referer": f"{self.URL_SJ}/",
        }

        logger.info(f"updating customer for booking {booking_id} ...")
        resp = self.client.patch(url_api, json=payload, headers=headers)

        if resp.status_code != 200:
            logger.error(f"update customer failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def checkout_booking(self, access_token: str, booking_id: str) -> dict[str, Any]:
        """
        Proceeds to checkout, initiating payment or reservation.

        Args:
            access_token: The OAuth2 access token.
            booking_id: The ID of the booking to checkout.

        Returns:
            The checkout response dictionary.

        """
        url_api = f"{self.URL_API_BOOKING}/bookings/{booking_id}/checkout"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-booking-client",
            "Referer": f"{self.URL_SJ}/",
        }

        payload = {"paymentMethod": "PAY_0"}

        logger.info(f"checking out booking {booking_id} ...")
        resp = self.client.post(url_api, json=payload, headers=headers)

        if resp.status_code not in [200, 201]:
            logger.error(f"checkout failed: {resp.status_code}")
            logger.debug(resp.text)
            resp.raise_for_status()

        return resp.json()

    def cancel_provisional_booking(self, access_token: str, booking_id: str) -> bool:
        """
        Cancels a provisional booking (status NEW).

        Args:
            access_token: The OAuth2 access token.
            booking_id: The ID of the provisional booking.

        Returns:
            True if cancellation was successful, False otherwise.

        """
        url_api = f"{self.URL_API_BOOKING}/bookings/provisional/{booking_id}/cancel"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": self.H_ACCEPT_JSON,
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "x-client-name": "sjse-account-client",
            "sec-ch-ua-platform": self.H_SEC_CH_UA_PLATFORM,
            "Referer": f"{self.URL_SJ}/",
            "Origin": self.URL_SJ,
            "ocp-apim-trace": "false",
        }
        logger.info(f"cancelling provisional booking {booking_id} with status NEW")
        logger.debug(f"cancellation headers:\n{log_json(headers)}")

        try:
            resp = self.client.patch(url_api, headers=headers)

            if resp.status_code not in [200, 204]:
                logger.error(f"cancel provisional booking failed: {resp.status_code}")
                logger.debug(resp.text)
                return False

            logger.info("cancellation successful")
            return True

        except Exception as e:
            logger.error(f"exception during cancellation: {e}")
            return False

    def cancel_booking_with_patch(
        self,
        access_token: str,
        booking_id: str,
        segments_and_passengers: list[dict],
    ) -> bool:
        """
        Cancel specific segments/passengers on a booking via PATCH.

        Args:
            access_token: The OAuth2 access token.
            booking_id: The ID of the booking.
            segments_and_passengers: List of dicts with keys
                ``serviceIdentifier`` and ``passengerIds``.

        Returns:
            True if cancellation was successful, False otherwise.

        """
        url_api = f"{self.URL_API_BOOKING}/bookings/{booking_id}/cancel"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "*/*",
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "ocp-apim-trace": "true",
            "subscription-id": "sales-web",
            "x-client-name": "sjse-edit-booking-client",
            "Referer": f"{self.URL_SJ}/",
            "Origin": self.URL_SJ,
        }
        headers.update(self.generate_trace_headers())

        payload = {"segmentAndPassengers": segments_and_passengers}

        logger.info(f"cancelling booking {booking_id} via patch")
        logger.debug(f"cancellation headers:\n{log_json(headers)}")
        logger.debug(f"cancellation payload:\n{log_json(payload)}")

        try:
            resp = self.client.patch(url_api, headers=headers, json=payload)

            if resp.status_code not in [200, 204]:
                logger.error(f"cancel (patch) failed: {resp.status_code}")
                logger.debug(resp.text)
                return False

            logger.info("cancellation successful")
            return True

        except Exception as e:
            logger.error(f"exception during cancellation: {e}")
            return False

    def finalize_cancellation(self, access_token: str, booking_id: str) -> bool:
        """
        Finalizes the cancellation by confirming the refund (Checkout).

        Step 1: Check refunds (GET /checkout/refunds)
        Step 2: Confirm (POST /checkout with paymentMethod: PAY_0)

        Args:
            access_token: The OAuth2 access token.
            booking_id: The ID of the booking to checkout/finalize.

        Returns:
            True if successful, False otherwise.

        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "*/*",
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "ocp-apim-trace": "true",
            "subscription-id": "sales-web",
            "x-client-name": "sjse-edit-booking-client",
            "Referer": f"{self.URL_SJ}/",
            "Origin": self.URL_SJ,
        }
        headers.update(self.generate_trace_headers())

        # 1. Get Refunds (Optional but good to log)
        url_refund = f"{self.URL_API_BOOKING}/bookings/{booking_id}/checkout/refunds"
        try:
            logger.info("checking refund details ...")
            res = self.client.get(url_refund, headers=headers)
            if res.status_code == 200:
                logger.debug(f"refund details: {res.text}")
            else:
                logger.warning(f"failed to get refunds: {res.status_code}")
        except Exception as e:
            logger.warning(f"error checking refunds: {e}")

        # 2. POST Checkout
        url_checkout = f"{self.URL_API_BOOKING}/bookings/{booking_id}/checkout"
        payload = {"paymentMethod": "PAY_0"}

        logger.info("finalizing cancellation (checkout) ...")
        try:
            res = self.client.post(url_checkout, json=payload, headers=headers)
            if res.status_code in [200, 201, 204]:
                logger.info("checkout/confirmation successful")
                logger.debug(f"checkout response: {res.text}")
                return True
            logger.error(f"checkout failed: {res.status_code} - {res.text}")
            return False
        except Exception as e:
            logger.error(f"error finalizing cancellation: {e}")
            return False

    def revert_booking(self, access_token: str, booking_id: str) -> bool:
        """
        Revert a booking to its previous state (undo a pending cancellation).

        Args:
            access_token: The OAuth2 access token.
            booking_id: The ID of the booking to revert.

        Returns:
            True if successful, False otherwise.

        """
        url_api = f"{self.URL_API_BOOKING}/bookings/{booking_id}/revert"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "*/*",
            "Content-Type": self.H_CONTENT_TYPE_JSON,
            "ocp-apim-subscription-key": self.H_OCP_APIM_SUB_KEY,
            "ocp-apim-trace": "true",
            "subscription-id": "sales-web",
            "x-client-name": "sjse-edit-booking-client",
            "Referer": f"{self.URL_SJ}/",
            "Origin": self.URL_SJ,
        }
        headers.update(self.generate_trace_headers())

        logger.info(f"reverting booking {booking_id} ...")
        try:
            resp = self.client.post(url_api, headers=headers, content=b"")
            if resp.status_code in [200, 204]:
                logger.info("revert successful")
                return True
            logger.error(f"revert failed: {resp.status_code}")
            logger.debug(resp.text)
            return False
        except Exception as e:
            logger.error(f"exception during revert: {e}")
            return False

    def close(self) -> None:
        """Closes the underlying HTTP client."""
        self.client.close()
