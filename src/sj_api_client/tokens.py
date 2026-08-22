"""Token management for the SJ API client."""

import base64
import contextlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sj_api_client.errors import SJAuthError
from sj_api_client.logger import log_json

logger = logging.getLogger(__name__)


def _write_private_json(path: Path, data: object) -> None:
    """
    Write JSON to a file only its owner can read: it holds tokens or cookies.

    Atomic: the data goes to a sibling temp file (created 0600, with a name
    unique to this writer — two processes saving at once must not share one)
    that replaces the real one only once complete, so a failure half-way
    (disk full, Ctrl-C) leaves the previous valid cache in place instead of
    a fragment that would lock every later run out with "failed to parse
    token cache".
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)  # the new file carries the temp file's 0600
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


class TokenManager:
    """
    Manages the loading, storage, and validation of authentication tokens.

    Attributes:
        DEFAULT_PATH (Path): The default system path for the token cache file.
        path (Path): The path to the token cache file.
        token (dict): The dictionary containing the loaded token data.

    """

    DEFAULT_PATH = Path.home() / ".cache" / "sj-api-client" / "token.json"

    def __init__(self, cache_path: str | Path | None = None) -> None:
        """
        Initializes the TokenManager.

        Args:
            cache_path (str or Path, optional): Custom path to the token cache
                file. Defaults to None, which results in using DEFAULT_PATH.

        """
        self.path = Path(cache_path) if cache_path else self.DEFAULT_PATH
        self.cookie_path = self.path.parent / "cookies.json"
        self.token: dict[str, Any] | None = None
        # Why the last save() could not write the cache (None when it could):
        # the token still works in memory, but the next run will have to log
        # in again, which the caller may want to say.
        self.save_error: str | None = None
        logger.debug(f"initialized token manager with path {self.path}")

    def load(self) -> dict[str, Any] | None:
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
                data = json.load(f)
        except Exception as e:
            raise SJAuthError(
                f"failed to parse token cache at {self.path}: {e} "
                f"(delete the file to force a fresh login)"
            ) from e
        if not isinstance(data, dict):
            raise SJAuthError(
                f"token cache at {self.path} is not a json object "
                f"(delete the file to force a fresh login)"
            )
        self.token = data
        logger.debug(f"loading token data {log_json(self.token)} from {self.path}")
        return self.token

    def save(self, token_data: dict[str, Any]) -> None:
        """
        Saves the provided token data to the cache file.

        Creates the directory structure if it doesn't exist; the file is
        written owner-readable only (0600), like the config that holds the
        password. Computes and stores the absolute refresh token expiry
        timestamp.

        Args:
            token_data (dict): The dict containing token information to save.

        """
        # Compute absolute refresh token expiry if we have the relative value
        rt_expires_in = token_data.get("refresh_token_expires_in")
        if rt_expires_in is not None and "refresh_token_expires_on" not in token_data:
            token_data["refresh_token_expires_on"] = int(time.time()) + int(rt_expires_in)

        # The in-memory token is the source of truth for this run even when
        # the cache cannot be written: a fresh login must not be treated as
        # expired just because the disk is read-only or full.
        self.token = token_data
        self.save_error = None
        try:
            _write_private_json(self.path, token_data)
            logger.debug(f"saved token data {log_json(token_data)} to {self.path}")
        except Exception as e:
            logger.error(f"failed to save token data to {self.path}: {e}")
            self.save_error = str(e)

    def is_valid(self) -> bool:
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
        now = time.time()
        if now < (exp - 300):
            logger.debug(f"token is valid until {datetime.fromtimestamp(exp)}")
            return True

        # Token is expired
        logger.debug(f"token is expired at {datetime.fromtimestamp(exp)}")
        return False

    def has_refresh_token(self) -> bool:
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
            now = time.time()
            if now >= rt_expires_on:
                logger.debug(f"refresh token expired at {datetime.fromtimestamp(rt_expires_on)}")
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

        remaining = rt_expires_on - time.time()
        if 0 < remaining < threshold_seconds:
            logger.info(
                f"refresh token expires in {int(remaining)}s, proactive renewal recommended"
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

        Raises:
            SJAuthError: If a cache file exists but could not be deleted —
                the session is then still usable, so "logged out" would lie.

        """
        removed = []
        failed = []
        for label, path in (("token", self.path), ("cookies", self.cookie_path)):
            if not path.exists():
                continue
            try:
                path.unlink()
                removed.append(label)
                logger.debug(f"removed {label} cache at {path}")
            except OSError as e:
                logger.warning(f"failed to remove {path}: {e}")
                failed.append(f"{label} cache at {path}: {e}")
        self.token = None
        if failed:
            raise SJAuthError("could not remove the " + "; ".join(failed))
        return removed

    def save_cookies(self, cookies: list[dict[str, str | None]]) -> None:
        """Save cookies to the cookie cache file."""
        try:
            _write_private_json(self.cookie_path, cookies)
            logger.debug(f"saved {len(cookies)} cookies to {self.cookie_path}")
        except Exception as e:
            logger.warning(f"failed to save cookies: {e}")

    def load_cookies(self) -> list[dict[str, str | None]] | None:
        """
        Load cookies from the cookie cache file.

        A cache that is not a list of name/value cookies is ignored (with a
        warning) rather than handed to the client: a corrupt file must not
        make every login fail until someone deletes it.
        """
        if not self.cookie_path.exists():
            logger.debug("no cookie cache found")
            return None
        try:
            with self.cookie_path.open() as f:
                cookies = json.load(f)
        except Exception as e:
            logger.warning(f"failed to load cookies: {e}")
            return None
        if not isinstance(cookies, list) or not all(
            isinstance(c, dict)
            and isinstance(c.get("name"), str)
            and isinstance(c.get("value"), str)
            for c in cookies
        ):
            logger.warning(f"ignoring malformed cookie cache at {self.cookie_path}")
            return None
        logger.debug(f"loaded {len(cookies)} cookies from {self.cookie_path}")
        return cookies
