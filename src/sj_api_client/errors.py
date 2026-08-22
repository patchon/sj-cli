"""Custom exceptions for the SJ API client."""

from collections.abc import Iterable
from typing import Any

import httpx

from sj_api_client.logger import redact_url


class SJError(Exception):
    """Base class for every error raised by the SJ API client."""


class SJAPIError(SJError):
    """
    Exception raised when the SJ API returns an error in the JSON body.

    Accepts the raw error payload dict (or a plain string). For a dict, the
    error code (errorCode/error, or the SJ envelope's numeric ``code``) and
    human message (message/error_description) are exposed as attributes and
    str() renders "code · message" — followed by the envelope's
    ``validationErrors`` as "field: reason" — instead of the raw dict; the
    full dict stays available as .payload.
    """

    def __init__(self, payload: dict[str, Any] | str) -> None:
        self.payload: dict[str, Any] | None = payload if isinstance(payload, dict) else None
        self.code: str | None = None
        self.message: str | None = None
        details: list[str] = []
        if self.payload:
            code = (
                self.payload.get("errorCode")
                or self.payload.get("error")
                or self.payload.get("code")
            )
            self.code = str(code) if code else None
            msg = self.payload.get("message") or self.payload.get("error_description")
            self.message = str(msg) if msg else None
            for item in self.payload.get("validationErrors") or []:
                if isinstance(item, dict) and item.get("message"):
                    field = item.get("propertyPath")
                    details.append(f"{field}: {item['message']}" if field else str(item["message"]))
        text = " · ".join(p for p in (self.code, self.message, *details) if p)
        super().__init__(text or str(payload))


class SJAuthError(SJError):
    """Exception raised when authentication fails."""


def error_text(e: BaseException) -> str:
    """
    One-line, user-facing text for an exception.

    httpx's HTTPStatusError appends a "For more information" line that
    breaks card output; URLs may carry a one-time auth code; some errors
    (PoolTimeout) stringify to nothing. Every place that prints an
    exception goes through this.
    """
    text = str(e).strip()
    if isinstance(e, httpx.HTTPStatusError):
        text = text.split("\n", 1)[0]
    return redact_url(text) if text else type(e).__name__


class SJConfigError(SJError):
    """
    Exception raised when configuration validation fails.

    ``errors`` holds the individual validation messages (empty for
    file-level failures like a missing or unparsable config).
    """

    def __init__(self, message: str, errors: Iterable[str] | None = None) -> None:
        self.errors = list(errors) if errors else []
        super().__init__(message)
