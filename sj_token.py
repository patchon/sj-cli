"""Token management for the SJ API client."""

import base64
import json
import logging
import time as _time
from datetime import datetime
from pathlib import Path

from sj_errors import SJAuthError
from sj_logger import log_json

logger = logging.getLogger(__name__)


class TokenManager:
    """
    Manages the loading, storage, and validation of authentication tokens.

    Attributes:
        DEFAULT_PATH (Path): The default system path for the token cache file.
        path (Path): The path to the token cache file.
        token (dict): The dictionary containing the loaded token data.

    """

    DEFAULT_PATH = Path.home() / ".cache" / "sj-api-client" / "token.json"

    def __init__(self, cache_path=None):
        """
        Initializes the TokenManager.

        Args:
            cache_path (str or Path, optional): Custom path to the token cache
                file. Defaults to None, which results in using DEFAULT_PATH.

        """
        self.path = Path(cache_path) if cache_path else self.DEFAULT_PATH
        self.cookie_path = self.path.parent / "cookies.json"
        self.token = None
        logger.debug(f"initialized token manager with path {self.path}")

    def load(self):
        """
        Loads and parses the token cache file.

        Returns:
            dict or None: The parsed token data, or None if the file doesn't
                exist.

        Raises:
            SJAuthError: If there is an error reading or parsing the json file.

        """
        if not self.path.exists():
            logger.debug(f"token cache not found at {self.path}")
            return None
        try:
            with self.path.open() as f:
                self.token = json.load(f)
                logger.debug(f"loading token data {log_json(self.token)} from {self.path}")
            return self.token
        except Exception as e:
            raise SJAuthError(
                f"failed to parse token cache at {self.path}: {e} "
                f"(delete the file to force a fresh login)"
            ) from e

    def save(self, token_data):
        """
        Saves the provided token data to the cache file.

        Creates the directory structure if it doesn't exist.
        Computes and stores the absolute refresh token expiry timestamp.

        Args:
            token_data (dict): The dict containing token information to save.

        """
        # Compute absolute refresh token expiry if we have the relative value
        rt_expires_in = token_data.get("refresh_token_expires_in")
        if rt_expires_in is not None and "refresh_token_expires_on" not in token_data:
            token_data["refresh_token_expires_on"] = int(_time.time()) + int(rt_expires_in)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w") as f:
                json.dump(token_data, f)
                logger.debug(f"saved token data {log_json(token_data)} to {self.path}")
            self.token = token_data
        except Exception as e:
            logger.error(f"failed to save token data to {self.path}: {e}")

    def is_valid(self):
        """
        Checks if the access token is present and not expired.

        Checks the 'expires_on' timestamp in the token data and adds a 5-minute
        buffer to ensure validity.

        Returns:
            bool: True if the token is valid and not expired, False otherwise.

        """
        if not self.token:
            logger.debug("no token data present")
            return False

        if "access_token" not in self.token:
            logger.debug(f"no valid access token found in token data {log_json(self.token)}")
            return False

        # Check expiry
        exp = self.token.get("expires_on")
        if not exp:
            logger.debug("token expiration not found")
            return False

        # 'expires_on' is usually a timestamp (int)
        if not isinstance(exp, int):
            logger.warning(f"token expiration is not an integer: {exp}")
            return False

        # Add a 5 minute buffer
        logger.debug("token expiration found, checking validity")
        now = datetime.now().timestamp()
        if now < (exp - 300):
            logger.debug(f"token is valid until {datetime.fromtimestamp(exp)}")
            return True

        # Token is expired
        logger.debug(f"token is expired at {datetime.fromtimestamp(exp)}")
        return False

    def has_refresh_token(self):
        """
        Checks if a refresh token is available and not expired.

        Returns:
            bool: True if a refresh token exists and is still valid,
                False otherwise.

        """
        if not self.token or "refresh_token" not in self.token:
            return False

        rt_expires_on = self.token.get("refresh_token_expires_on")
        if rt_expires_on and isinstance(rt_expires_on, (int, float)):
            now = _time.time()
            if now >= rt_expires_on:
                logger.debug(
                    f"refresh token expired at "
                    f"{datetime.fromtimestamp(rt_expires_on)}"
                )
                return False
            remaining = int(rt_expires_on - now)
            logger.debug(
                f"refresh token valid until "
                f"{datetime.fromtimestamp(rt_expires_on)} "
                f"({remaining}s remaining)"
            )

        return True

    def refresh_token_needs_renewal(self, threshold_seconds: int = 3600) -> bool:
        """
        Check if the refresh token is approaching expiry.

        Determines whether the refresh token should be proactively renewed.

        Args:
            threshold_seconds: Renew if fewer than this many seconds remain.
                Defaults to 1 hour.

        Returns:
            True if the refresh token exists but expires within the threshold.

        """
        if not self.token or "refresh_token" not in self.token:
            return False

        rt_expires_on = self.token.get("refresh_token_expires_on")
        if not rt_expires_on or not isinstance(rt_expires_on, (int, float)):
            return False

        remaining = rt_expires_on - _time.time()
        if 0 < remaining < threshold_seconds:
            logger.info(
                f"refresh token expires in {int(remaining)}s, "
                f"proactive renewal recommended"
            )
            return True

        return False

    def profile_email(self) -> str | None:
        """
        Email of the logged-in user, from the token's profile_info blob.

        profile_info is base64url-encoded JSON from B2C; the email is its
        preferred_username claim.

        Returns:
            The email string, or None if absent or undecodable.

        """
        if not self.token:
            return None
        blob = self.token.get("profile_info")
        if not isinstance(blob, str) or not blob:
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))
            email = data.get("preferred_username")
            return email if isinstance(email, str) and email else None
        except Exception as e:
            logger.debug(f"could not decode profile_info: {e}")
            return None

    def clear(self) -> list[str]:
        """
        Delete the cached token and cookie files (logout).

        Returns:
            Labels of the caches actually removed: "token" and/or "cookies".
                Empty list when nothing was cached.

        """
        removed = []
        for label, path in (("token", self.path), ("cookies", self.cookie_path)):
            if not path.exists():
                continue
            try:
                path.unlink()
                removed.append(label)
                logger.debug(f"removed {label} cache at {path}")
            except OSError as e:
                logger.warning(f"failed to remove {path}: {e}")
        self.token = None
        return removed

    def save_cookies(self, cookies: list[dict[str, str | None]]) -> None:
        """Save cookies to the cookie cache file."""
        try:
            self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cookie_path.open("w") as f:
                json.dump(cookies, f)
            logger.debug(f"saved {len(cookies)} cookies to {self.cookie_path}")
        except Exception as e:
            logger.warning(f"failed to save cookies: {e}")

    def load_cookies(self) -> list[dict[str, str | None]] | None:
        """Load cookies from the cookie cache file."""
        if not self.cookie_path.exists():
            logger.debug("no cookie cache found")
            return None
        try:
            with self.cookie_path.open() as f:
                cookies = json.load(f)
            logger.debug(f"loaded {len(cookies)} cookies from {self.cookie_path}")
            return cookies
        except Exception as e:
            logger.warning(f"failed to load cookies: {e}")
            return None
