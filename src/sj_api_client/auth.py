"""Authentication orchestration for the SJ API client."""

import logging
import select
import sys

from sj_api_client.client import SJClient
from sj_api_client.errors import SJAPIError, SJAuthError, error_text
from sj_api_client.output import blank, print_status_card, prompt, pwarn, spinner
from sj_api_client.tokens import TokenManager

logger = logging.getLogger(__name__)

SMS_TIMEOUT_SECONDS = 120
SMS_CODE_ATTEMPTS = 3


def read_sms_code(timeout_seconds: int = SMS_TIMEOUT_SECONDS) -> str | None:
    """
    Read an SMS code from stdin with a timeout.

    Args:
        timeout_seconds: How long to wait for input (default 120s).

    The timeout guards a human at a terminal. An empty line (an Enter
    pressed while the spinners ran) is not an answer: it is re-asked. A
    pipe is read line by line without select(): Python's read-ahead buffer
    hides queued lines from it, and a pipe cannot time out meaningfully.

    Returns:
        The entered code string, or None on timeout or end of input.

    """
    text = f"enter sms code (timeout {timeout_seconds // 60}m): "
    interactive = sys.stdin.isatty()
    prompt(text)
    while True:
        if interactive:
            ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
            if not ready:
                print()  # close the prompt line
                return None
        line = sys.stdin.readline()
        # Close the prompt line unless the user's Enter already echoed a newline
        if not interactive or not line:
            print()
        if not line:
            return None  # end of input
        code = line.strip()
        if code:
            return code
        pwarn("no code entered, try again")
        prompt(text)


def perform_full_login(
    client: SJClient,
    email: str,
    password: str,
    token_manager: TokenManager | None = None,
) -> dict:
    """
    Orchestrate the full B2C login flow with SMS MFA.

    If a token_manager is provided, attempts cookie-based silent login first
    (no SMS required). Falls back to full SMS flow if that fails.

    Args:
        client: The SJ HTTP client.
        email: User's email address.
        password: User's password.
        token_manager: Optional token manager for cookie persistence.

    Returns:
        Token data dict containing access_token, refresh_token, etc.

    Raises:
        SJAuthError: If login fails at any step, or SMS times out.

    """
    logger.info("starting full authentication flow ...")

    try:
        # Try SSO cookie-based silent login first (no credentials needed)
        if token_manager:
            cookies = token_manager.load_cookies()
            if cookies:
                logger.info("attempting cookie-based silent login ...")
                client.import_cookies(cookies)
                try:
                    with spinner("attempting session restore"):
                        auth_code = client.try_silent_login()
                    if auth_code:
                        with spinner("restoring session"):
                            token_data = client.exchange_code(auth_code)
                        token_manager.save_cookies(client.export_cookies())
                        blank()
                        return token_data
                    logger.info("silent login failed, falling back to full login")
                except Exception as e:
                    logger.warning(f"cookie-based silent login failed: {e}")
                # Reset client state for fresh attempt (new PKCE codes etc.)
                client.reset()
                # Re-import cookies so the full login flow can still benefit
                # from the SSO cookie (e.g. for device trust)
                client.import_cookies(cookies)

        # Full login flow (no cookies or cookie attempt failed)
        with spinner("performing login"):
            mfa_required = client.initiate_login(email, password)

        if mfa_required:
            # Step 2: SMS
            with spinner("sending sms code"):
                client.sms_trigger()

            # Step 3: Prompt + verify, retrying on a rejected code (same sms,
            # never re-sent). B2C answers a bare {"status": "449"} ("retry")
            # when the code is rejected — translate it, the body says no more.
            for attempt in range(1, SMS_CODE_ATTEMPTS + 1):
                code = read_sms_code()
                if not code:
                    raise SJAuthError("sms code not provided or timed out after 2 minutes")
                try:
                    with spinner("verifying sms code"):
                        client.sms_verify(code)
                    break
                except SJAPIError as e:
                    if not (e.payload and str(e.payload.get("status")) == "449"):
                        raise
                    if attempt == SMS_CODE_ATTEMPTS:
                        raise SJAuthError(
                            f"sms code rejected {SMS_CODE_ATTEMPTS} times, re-run to try again"
                        ) from e
                    pwarn(f"sms code rejected, {SMS_CODE_ATTEMPTS - attempt} attempt(s) left")
        else:
            logger.info("sms verification not required, skipping")

        # Steps 4+5: finalize (device registration + auth code) and exchange
        with spinner("completing login"):
            auth_code = client.finalize_login()
            if not auth_code:
                raise SJAuthError("could not retrieve authorization code from final redirect")
            token_data = client.exchange_code(auth_code)

        # Save cookies for future silent logins
        if token_manager:
            token_manager.save_cookies(client.export_cookies())

        blank()
        return token_data

    except SJAuthError:
        raise
    except Exception as e:
        raise SJAuthError(f"login failed: {error_text(e)}") from e


def handle_logout(client: SJClient, token_manager: TokenManager) -> None:
    """
    Log out: end the sj.se SSO session server-side and delete the local caches.

    The server-side call (OIDC end-session endpoint, authenticated by the
    cached SSO cookies) is skipped when no cookies are cached. The local
    caches are cleared even when that call fails.

    Args:
        client: The SJ HTTP client.
        token_manager: The token cache manager.

    Raises:
        SJAuthError: If the server-side logout call failed (caches are
            cleared regardless).

    """
    server_error: Exception | None = None
    cookies = token_manager.load_cookies()
    printed_trail = False
    if cookies:
        printed_trail = True
        try:
            with spinner("ending sj.se session"):
                client.import_cookies(cookies)
                client.b2c_logout()
        except Exception as e:
            logger.warning(f"server-side logout failed: {e}")
            server_error = e

    removed = []
    if token_manager.path.exists() or token_manager.cookie_path.exists():
        printed_trail = True
        with spinner("removing cached token and cookies"):
            removed = token_manager.clear()

    if server_error:
        # Caches are cleared; sj_tool renders the failure card from this.
        raise SJAuthError(
            f"server-side logout failed: {error_text(server_error)} (local caches cleared)"
        ) from server_error

    if printed_trail:
        blank()
    print_status_card(True, "logged out" if removed or cookies else "already logged out")


def ensure_authenticated(
    client: SJClient, token_manager: TokenManager, email: str, password: str
) -> tuple[str, str]:
    """
    Ensure a valid access token is available.

    Follows the token lifecycle: validate → refresh → full login.

    Args:
        client: The SJ HTTP client.
        token_manager: The token cache manager.
        email: User's email address (for full login fallback).
        password: User's password (for full login fallback).

    Returns:
        (access_token, method) where method says which lifecycle rung
        supplied the token: "cached" (valid cached token), "refreshed"
        (silent refresh), or "full" (interactive/cookie full login).

    Raises:
        SJAuthError: If all authentication methods fail — or if a refresh
            that should have worked failed transiently (network, 5xx): that
            is reported as such rather than turned into an SMS login the
            refresh token makes unnecessary (SPEC §3.1).

    """
    token_data = token_manager.load()

    if token_data:
        # Valid token — but proactively refresh if refresh token is close to expiry
        if token_manager.is_valid():
            if token_manager.refresh_token_needs_renewal():
                logger.info(
                    "access token valid but refresh token near expiry, "
                    "proactively refreshing to extend session ..."
                )
                try:
                    new_token = client.refresh_token(token_data["refresh_token"])
                except SJAuthError as e:  # transient: the current token still works
                    logger.warning(f"proactive refresh failed: {e}")
                    new_token = None
                if new_token:
                    token_manager.save(new_token)
                    logger.info("session extended via proactive token refresh")
                    return new_token["access_token"], "refreshed"
            logger.info("using cached access token")
            return token_data["access_token"], "cached"

        # Expired but has refresh token → refresh. A transient failure
        # propagates (re-run later); only a rejected refresh token (None)
        # means a full login is due.
        if token_manager.has_refresh_token():
            logger.info("access token expired, refreshing ...")
            new_token = client.refresh_token(token_data["refresh_token"])
            if new_token:
                token_manager.save(new_token)
                logger.info("token refreshed successfully")
                return new_token["access_token"], "refreshed"
            logger.info("refresh token rejected, performing full login ...")
        else:
            logger.info("refresh token expired, performing full login ...")

    # No token or refresh failed → full login (try cookies first)
    logger.info("no valid token found, performing full login ...")
    token_data = perform_full_login(client, email, password, token_manager)
    token_manager.save(token_data)
    return token_data["access_token"], "full"


def ensure_valid_token(
    client: SJClient, token_manager: TokenManager, current_access_token: str
) -> str:
    """
    Check if the current token is still valid and refresh if needed.

    Called before each date iteration to handle mid-run token expiry.

    Args:
        client: The SJ HTTP client.
        token_manager: The token cache manager.
        current_access_token: The currently held access token.

    Returns:
        A valid access token (may be the same or a refreshed one).

    Raises:
        SJAuthError: If the token is expired and cannot be refreshed.

    """
    # Proactively refresh if the refresh token is approaching expiry
    if token_manager.is_valid():
        if token_manager.refresh_token_needs_renewal():
            token_data = token_manager.token
            if token_data and "refresh_token" in token_data:
                try:
                    new_token = client.refresh_token(token_data["refresh_token"])
                except SJAuthError as e:  # transient: the current token still works
                    logger.warning(f"proactive mid-run refresh failed: {e}")
                else:
                    if new_token:
                        token_manager.save(new_token)
                        logger.info("session extended via proactive token refresh")
                        return new_token["access_token"]
        return current_access_token

    logger.info("access token expired during run, attempting refresh ...")

    token_data = token_manager.token
    if not token_data or not token_manager.has_refresh_token():
        raise SJAuthError(
            "access token expired and no refresh token available. "
            "please re-run the tool to re-authenticate."
        )

    # A transient failure (SJAuthError from the client) propagates with its
    # cause: re-running later will refresh fine, which "re-authenticate"
    # would not say. None means the refresh token itself was rejected.
    new_token = client.refresh_token(token_data["refresh_token"])
    if new_token:
        token_manager.save(new_token)
        logger.info("token refreshed mid-run")
        return new_token["access_token"]

    raise SJAuthError(
        "access token expired and the refresh token was rejected. "
        "please re-run the tool to re-authenticate."
    )
