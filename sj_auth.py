"""Authentication orchestration for the SJ API client."""

import logging
import select
import sys

from sj_client import SJClient
from sj_errors import SJAuthError
from sj_output import pinfo, spinner
from sj_token import TokenManager

logger = logging.getLogger(__name__)

SMS_TIMEOUT_SECONDS = 120


def read_sms_code(timeout_seconds: int = SMS_TIMEOUT_SECONDS) -> str | None:
    """
    Read an SMS code from stdin with a timeout.

    Args:
        timeout_seconds: How long to wait for input (default 120s).

    Returns:
        The entered code string, or None if timeout or empty input.

    """
    pinfo(f"enter sms code (timeout {timeout_seconds}s): ")
    sys.stdout.flush()

    ready, _, _ = select.select([sys.stdin], [], [], timeout_seconds)
    if not ready:
        return None

    code = sys.stdin.readline().strip()
    return code or None


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
        # Try cookie-based silent login first (may skip MFA)
        if token_manager:
            cookies = token_manager.load_cookies()
            if cookies:
                logger.info("attempting cookie-based silent login ...")
                client.import_cookies(cookies)
                try:
                    with spinner("attempting session restore"):
                        mfa_required = client.initiate_login(email, password)
                    if not mfa_required:
                        with spinner("restoring session"):
                            auth_code = client.finalize_login()
                            if auth_code:
                                token_data = client.exchange_code(auth_code)
                                token_manager.save_cookies(client.export_cookies())
                                return token_data
                        logger.warning("cookie-based login: finalize returned no code")
                    else:
                        logger.info(
                            "cookie-based login still requires MFA, "
                            "continuing with SMS flow"
                        )
                        # MFA required — continue directly with SMS below
                        # (don't re-init, the login state is valid)
                        pinfo("sms verification required")
                        client.sms_trigger()

                        code = read_sms_code()
                        if not code:
                            raise SJAuthError(
                                "sms code not provided or timed out after 2 minutes"
                            )

                        with spinner("verifying sms code"):
                            client.sms_verify(code)
                            auth_code = client.finalize_login()
                        if not auth_code:
                            raise SJAuthError(
                                "could not retrieve authorization code "
                                "from final redirect"
                            )

                        with spinner("exchanging authorization code"):
                            token_data = client.exchange_code(auth_code)
                        token_manager.save_cookies(client.export_cookies())
                        return token_data
                except SJAuthError:
                    raise
                except Exception as e:
                    logger.warning(f"cookie-based login failed: {e}")
                    # Reset client state for fresh attempt
                    client.__init__()

        # Full login flow (no cookies or cookie attempt failed)
        with spinner("performing login"):
            mfa_required = client.initiate_login(email, password)

        if mfa_required:
            # Step 2: SMS
            pinfo("sms verification required")
            client.sms_trigger()

            code = read_sms_code()
            if not code:
                raise SJAuthError("sms code not provided or timed out after 2 minutes")

            # Step 3: Verify
            with spinner("verifying sms code"):
                client.sms_verify(code)
        else:
            pinfo("sms verification not required, skipping")

        # Step 4: Finalize (device registration + extract auth code)
        with spinner("finalizing login"):
            auth_code = client.finalize_login()
        if not auth_code:
            raise SJAuthError("could not retrieve authorization code from final redirect")

        # Step 5: Exchange
        with spinner("exchanging authorization code"):
            token_data = client.exchange_code(auth_code)

        # Save cookies for future silent logins
        if token_manager:
            token_manager.save_cookies(client.export_cookies())

        return token_data

    except SJAuthError:
        raise
    except Exception as e:
        raise SJAuthError(f"login failed: {e}") from e


def ensure_authenticated(
    client: SJClient, token_manager: TokenManager, email: str, password: str
) -> str:
    """
    Ensure a valid access token is available.

    Follows the token lifecycle: validate → refresh → full login.

    Args:
        client: The SJ HTTP client.
        token_manager: The token cache manager.
        email: User's email address (for full login fallback).
        password: User's password (for full login fallback).

    Returns:
        A valid access token string.

    Raises:
        SJAuthError: If all authentication methods fail.

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
                    if new_token:
                        token_manager.save(new_token)
                        pinfo("session extended via proactive token refresh")
                        return new_token["access_token"]
                except Exception as e:
                    logger.warning(f"proactive refresh failed: {e}")
                # Still valid, use current token
            logger.info("using cached access token")
            return token_data["access_token"]

        # Expired but has refresh token → try refresh
        if token_manager.has_refresh_token():
            logger.info("access token expired, refreshing ...")
            try:
                new_token = client.refresh_token(token_data["refresh_token"])
                if new_token:
                    token_manager.save(new_token)
                    logger.info("token refreshed successfully")
                    return new_token["access_token"]
            except Exception as e:
                logger.warning(f"token refresh failed: {e}")

            # Refresh failed, fall through to full login
            pinfo("token refresh failed, performing full login ...")
        else:
            pinfo("refresh token expired, performing full login ...")

    # No token or refresh failed → full login (try cookies first)
    pinfo("no valid token found, performing full login ...")
    token_data = perform_full_login(client, email, password, token_manager)
    token_manager.save(token_data)
    return token_data["access_token"]


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
                    if new_token:
                        token_manager.save(new_token)
                        pinfo("session extended via proactive token refresh")
                        return new_token["access_token"]
                except Exception as e:
                    logger.warning(f"proactive mid-run refresh failed: {e}")
        return current_access_token

    logger.info("access token expired during run, attempting refresh ...")

    token_data = token_manager.token
    if not token_data or not token_manager.has_refresh_token():
        raise SJAuthError(
            "access token expired and no refresh token available. "
            "please re-run the tool to re-authenticate."
        )

    try:
        new_token = client.refresh_token(token_data["refresh_token"])
        if new_token:
            token_manager.save(new_token)
            pinfo("token refreshed mid-run")
            return new_token["access_token"]
    except Exception as e:
        logger.warning(f"mid-run token refresh failed: {e}")

    raise SJAuthError(
        "access token expired and refresh failed. "
        "please re-run the tool to re-authenticate."
    )
