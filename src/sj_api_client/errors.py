"""Custom exceptions for the SJ API client."""


class SJAPIError(Exception):
    """
    Exception raised when the SJ API returns an error in the JSON body.

    Accepts the raw error payload dict (or a plain string). For a dict, the
    error code (errorCode/error) and human message (message/error_description)
    are exposed as attributes and str() renders "code · message" instead of
    the raw dict; the full dict stays available as .payload.
    """

    def __init__(self, payload):
        self.payload = payload if isinstance(payload, dict) else None
        self.code = None
        self.message = None
        if self.payload:
            code = self.payload.get("errorCode") or self.payload.get("error")
            self.code = str(code) if code else None
            msg = self.payload.get("message") or self.payload.get("error_description")
            self.message = str(msg) if msg else None
        text = " · ".join(p for p in (self.code, self.message) if p)
        super().__init__(text or str(payload))


class SJAuthError(Exception):
    """Exception raised when authentication fails."""


class SJConfigError(Exception):
    """
    Exception raised when configuration validation fails.

    ``errors`` holds the individual validation messages (empty for
    file-level failures like a missing or unparsable config).
    """

    def __init__(self, message, errors=None):
        self.errors = list(errors) if errors else []
        super().__init__(message)
