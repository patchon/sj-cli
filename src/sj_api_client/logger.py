"""Logging setup: TRACE level, colour formatter, httpx filtering and secret redaction."""

import inspect
import json
import logging
import os
import re
import sys
import time
from typing import Any, ClassVar, override

import httpx

_this_file = os.path.normcase(__file__)
_logging_file = os.path.normcase(logging.__file__ or "")

# Add TRACE log level
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self: logging.Logger, message: object, *args: object, **kws: Any) -> None:
    """Log 'message % args' with severity 'TRACE'."""
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        # Yes, logger takes its '*args' as 'args'.
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace  # type: ignore[attr-defined]


# Keys whose values must never reach the logs (matched case-insensitively).
_SECRET_KEYS = frozenset(
    {"password", "access_token", "refresh_token", "id_token", "code_verifier", "authorization"}
)
_REDACTED = "***redacted***"

# httpx/httpcore trace lines carry raw header tuples (Set-Cookie with the SSO
# cookie, Cookie, Authorization) and URLs with the one-time auth code; they
# never pass through redact(), so they are scrubbed by pattern.
_HTTP_HEADER_RE = re.compile(
    r"(?i)(b?['\"](?:set-cookie|cookie|authorization|x-csrf-token)['\"],\s*b?['\"])([^'\"]*)"
)
_URL_CODE_RE = re.compile(r"(?i)([?&]code=)[^&'\"\s]+")


def redact_url(url: object) -> str:
    """Return the URL as text with an OAuth ``code=`` query value hidden."""
    return _URL_CODE_RE.sub(r"\1***", str(url))


def redact_http_trace(message: str) -> str:
    """Scrub secret header values and auth codes from an httpx/httpcore log line."""
    return redact_url(_HTTP_HEADER_RE.sub(rf"\1{_REDACTED}", message))


def redact(data: object) -> object:
    """Return a copy of data with values of secret keys replaced (recursively)."""
    if isinstance(data, dict):
        return {
            k: (_REDACTED if str(k).lower() in _SECRET_KEYS else redact(v)) for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact(v) for v in data]
    return data


def log_json(data: object) -> str:
    """
    Dumps given json object to a pretty string, with secrets redacted.

    Returns:
        str: An indented json representation of given object.

    """
    try:
        return json.dumps(redact(data), indent=2, default=str)
    except Exception as e:
        return f"failed to serialize json: {e}"


def log_request(request: httpx.Request) -> None:
    """Log an HTTP request."""
    logging.getLogger(__name__).debug(f" -> {request.method} {redact_url(request.url)}")


def log_response(response: httpx.Response) -> None:
    """Log an HTTP response."""
    logger = logging.getLogger(__name__)
    try:
        response.read()  # Load content so we can print it
        logger.debug(f" <- {response.status_code} {redact_url(response.url)}")

        if response.content:
            try:
                try:
                    body_json = response.json()
                    logger.debug(f" body [json]: {log_json(body_json)}")
                except Exception:
                    logger.debug(
                        f" body [raw]: {response.text[:1200]} <truncated after 1200 chars> ..."
                    )
            except Exception:
                logger.debug(f" body [binary]: {len(response.content)} bytes")

    except Exception as e:
        logger.warning(f"{response.status_code} < {response.url} (failed to read body: {e})")


class HttpxTraceLevelFilter(logging.Filter):
    """A filter to downgrade httpx/httpcore DEBUG logs to TRACE and scrub secrets from them."""

    def __init__(self, is_trace_level: bool, name: str = "") -> None:
        """Initializes the filter. is_trace_level: whether the global level is TRACE."""
        super().__init__(name)
        self.is_trace_level = is_trace_level

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """
        Filter the record.

        If the global log level is TRACE, this filter checks for logs
        from httpx or httpcore and changes their level to TRACE.

        Args:
            record: The log record.

        Returns:
            True to always pass the record on.

        """
        if not self.is_trace_level:
            return True

        if record.name.startswith(("httpx", "httpcore")):
            record.levelname = "TRACE"
            record.levelno = TRACE_LEVEL_NUM
            message = record.getMessage()
            scrubbed = redact_http_trace(message)
            if scrubbed != message:
                record.msg = scrubbed
                record.args = ()

        return True


class CliLogFormatter(logging.Formatter):
    """
    Custom formatter for CLI logging output.

    This formatter adds color to log messages based on the log level and
    formats timestamps in ISO 8601 format with the local timezone.

    Attributes:
        COLORS: A dictionary mapping log levels to ANSI color codes.
        RESET: The ANSI escape code to reset color.

    """

    COLORS: ClassVar[dict[str, str]] = {
        "TRACE": "\033[38;5;208m",  # Orange
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[41m",  # Red background
    }
    RESET: ClassVar[str] = "\033[0m"

    @override
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """
        Format the time in ISO 8601 format with local timezone.

        Args:
            record: The log record containing the creation time.
            datefmt: The date format string (unused).

        Returns:
            The formatted time string.

        """
        ct = self.converter(record.created)
        s = time.strftime("%Y-%m-%dT%H:%M:%S", ct)

        # Insert the colon in the timezone offset, e.g., +0000 -> +00:00.
        tz = time.strftime("%z", ct)
        if tz:
            tz = f"{tz[:3]}:{tz[3:]}"
        return f"{s}.{int(record.msecs):03d}{tz}"

    @override
    def format(self, record: logging.LogRecord) -> str:
        """
        Format the specified record as text.

        Args:
            record: The log record to format.

        Returns:
            The formatted log message string.

        """
        record.className = ""
        try:
            frame = inspect.currentframe()
            while frame:
                fname = os.path.normcase(frame.f_code.co_filename)
                if fname in (_logging_file, _this_file):
                    frame = frame.f_back
                    continue
                if "self" in frame.f_locals:
                    record.className = frame.f_locals["self"].__class__.__name__
                break
            del frame
        except Exception:
            pass  # silent fail.

        levelname = record.levelname
        func_name = record.funcName
        filename = record.filename
        class_name = record.className  # type: ignore[attr-defined]

        # Pad the level name to ensure alignment
        record.levelname = f"{levelname:<8}"
        record.filename = f"{filename:<20}"
        record.funcName = f"{func_name:<20}"

        display_class_name = class_name or '""'
        record.className = f"{display_class_name:<20}"

        log_message = super().format(record)

        if record.levelname.strip() in self.COLORS:
            log_message = f"{self.COLORS[record.levelname.strip()]}{log_message}{self.RESET}"

        record.filename = filename
        record.funcName = func_name
        record.className = class_name
        record.levelname = levelname
        return log_message


def setup_logging(level: str) -> None:
    """
    Configure the logger.

    Sets up logging to stderr with a custom formatter. Allows for dynamic
    log level changes on subsequent calls.

    Args:
        level: The log level to set. Empty or invalid values fall back to CRITICAL.

    """
    levels = {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
        "TRACE": TRACE_LEVEL_NUM,
        "NOTSET": logging.NOTSET,
    }

    level_upper = level.upper()
    if not level_upper:
        level_upper = "CRITICAL"

    log_level = levels.get(level_upper)
    err_msg = ""
    if log_level is None:
        err_msg = f"invalid log level '{level}' specified, defaulting to CRITICAL"
        log_level = logging.CRITICAL

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    if level_upper == "TRACE":
        httpx_logger.setLevel(log_level)
        httpcore_logger.setLevel(log_level)
    else:
        httpx_logger.setLevel(logging.WARNING)
        httpcore_logger.setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)

    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.addFilter(HttpxTraceLevelFilter(level_upper == "TRACE"))
        handler.setFormatter(
            CliLogFormatter(
                "time=%(asctime)s level=%(levelname)s "
                "file=%(filename)s class=%(className)s "
                'function=%(funcName)s msg="%(message)s"'
            )
        )
        root_logger.addHandler(handler)

        if err_msg:
            logger.warning(err_msg)
        else:
            logger.debug(
                "log level set to '%s' from env variable LOG_LEVEL",
                logging.getLevelName(log_level),
            )
    elif err_msg:
        logger.warning(err_msg)

    logger.debug(
        "setting log level to %s",
        logging.getLevelName(log_level),
    )
