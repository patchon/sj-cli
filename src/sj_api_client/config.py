"""Configuration loading and validation for the SJ API client."""

import getpass
import logging
import os
import re
import tomllib
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any

from sj_api_client.client import STATION_MAP
from sj_api_client.dates import sweden_now
from sj_api_client.errors import SJConfigError
from sj_api_client.logger import log_json
from sj_api_client.output import ask, blank, pinfo, print_status_card, prompt, pwarn, spinner

logger = logging.getLogger(__name__)

# Valid service_types values and their display names ("ALL" = no filter).
SERVICE_TYPE_NAMES = {
    "ALL": "All",
    "SJ_HIGH": "SJ High-speed train",
    "SJ_IC": "SJ InterCity",
    "SJ_REG": "SJ Regional",
    "SJ_NT": "SJ Night train",
    "X_TRAINOPS": "Other train operators",
    "X_PTA": "Public transport",
    "X_EXPBUS": "Express buses",
}


class CfgManager:
    """
    Manages the loading and validation of configuration for sj-api-client.

    Attributes:
        DEFAULT_PATH: The default system path for the configuration file.
        EXAMPLE_PATH: The documented template next to the code (first-run setup).
        path: The path to the configuration file to be loaded.
        cfg: The dictionary containing the loaded configuration data.

    """

    DEFAULT_PATH = (
        Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "sj-api-client"
        / "config.toml"
    )
    EXAMPLE_PATH = Path(__file__).parent / "config.example.toml"

    def __init__(self, cfg_path: str | Path | None = None) -> None:
        self.path = Path(cfg_path) if cfg_path else self.DEFAULT_PATH
        self.cfg: dict[str, Any] = {}
        logger.debug(f"initialized config manager with path {self.path}")

    def load(self) -> dict[str, Any]:
        """
        Loads and parses the configuration file.

        Returns:
            The parsed configuration settings.

        Raises:
            SJConfigError: If the file is missing, cannot be read, or is not
                valid TOML — each with its own message.

        """
        if not self.path.exists():
            raise SJConfigError(f"no config file found at {self.path}")
        try:
            with self.path.open("rb") as f:
                self.cfg = tomllib.load(f)
        except OSError as e:
            raise SJConfigError(f"cannot read config at {self.path}: {e}") from e
        except ValueError as e:  # TOMLDecodeError, or a non-UTF-8 file
            raise SJConfigError(f"failed to parse toml config at {self.path}: {e}") from e
        logger.debug(f"loading config {log_json(self.cfg)} from {self.path}")
        return self.cfg

    def create_interactive(self) -> bool:
        """
        First-run setup: offer to create the config file interactively.

        Asks for the SJ credentials (password never echoed), writes the
        documented template with them filled in, and chmods the file to
        0600 — it holds the password. The user still edits the search
        parameters by hand before the first run.

        Returns:
            True when the config was created, False when declined.

        Raises:
            SJConfigError: If the file cannot be written.

        """
        pinfo(f"no config found at {self.path}")
        if ask("create it now? [y/n]: ").strip().lower() not in ("y", "yes"):
            return False

        while True:
            email = ask("sj account email: ").strip()
            if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                break
            pwarn(f"'{email}' is not an email address")
        while True:
            prompt("sj account password: ")
            try:
                password = getpass.getpass("")
            except EOFError:  # Ctrl-D: treat like an empty answer, as ask() does
                print()
                password = ""
            if password:
                break
            pwarn("password must not be empty")

        def toml_str(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"')

        # Function replacements: a string replacement would have re.sub
        # re-process the backslashes toml_str just escaped.
        template = self.EXAMPLE_PATH.read_text()
        content = re.sub(r'(?m)^email = ".*"$', lambda _m: f'email = "{toml_str(email)}"', template)
        content = re.sub(
            r'(?m)^password = ".*"$', lambda _m: f'password = "{toml_str(password)}"', content
        )
        try:
            with spinner("writing config"):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.touch(mode=0o600)  # create owner-only before the password is written
                self.path.write_text(content)
                self.path.chmod(0o600)
        except OSError as e:
            raise SJConfigError(f"could not write config at {self.path}: {e}") from e

        blank()
        print_status_card(
            True,
            "config created",
            [
                ("file", str(self.path)),
                ("next", "edit [search_parameters] (route, dates, times) before booking"),
            ],
        )
        return True

    def verify_cfg(self, cfg: dict[str, Any], require_search: bool = True) -> None:
        """
        Validates configuration fields.

        [auth] is always validated; [search_parameters] only when
        require_search is true — operations that never touch the route or
        dates (login, listing, cancelling by number) work with a config
        holding only credentials. Collects all errors, reports them at once.

        Raises:
            SJConfigError: If any validation errors are found.

        """
        errors: list[str] = []

        # [auth] section (every value may be of the wrong TOML type: collect
        # an error rather than crash on it)
        auth = cfg.get("auth")
        if not auth or not isinstance(auth, dict):
            errors.append("[auth] section is missing")
        else:
            email = auth.get("email", "")
            if (
                not isinstance(email, str)
                or not email
                or "@" not in email
                or "." not in email.split("@")[-1]
            ):
                errors.append("email must be a valid email address (x@y.z)")
            password = auth.get("password")
            if not password or not isinstance(password, str):
                errors.append("password must be a non-empty string")

        # [search_parameters] section — only booking-shaped operations use it
        if require_search:
            params = cfg.get("search_parameters")
            if not params or not isinstance(params, dict):
                errors.append("[search_parameters] section is missing")
            else:
                self._validate_search_params(params, errors)

        if errors:
            raise SJConfigError(
                "configuration errors:\n  - " + "\n  - ".join(errors), errors=errors
            )

    def _validate_search_params(self, params: dict[str, Any], errors: list[str]) -> None:
        """Validate the [search_parameters] section."""
        # date_start
        date_start = self._validate_date(params.get("date_start", ""), "date_start", errors)

        # date_end
        date_end = self._validate_date(params.get("date_end", ""), "date_end", errors)

        # Normalise native TOML dates back to strings for downstream code
        if date_start:
            params["date_start"] = date_start.isoformat()
        if date_end:
            params["date_end"] = date_end.isoformat()

        # date_end >= date_start
        if date_start and date_end and date_end < date_start:
            errors.append("date_end must be >= date_start")

        # date_start must be today or in the future
        if date_start and date_start < sweden_now().date():
            errors.append("date_start must be today or in the future")

        # time_leave
        t = self._validate_time(params.get("time_leave", ""), "time_leave", errors)
        if t:
            params["time_leave"] = t

        # roundtrip
        roundtrip = params.get("roundtrip")
        if roundtrip is None or not isinstance(roundtrip, bool):
            errors.append("roundtrip must be a boolean (true/false)")
        elif roundtrip:
            t = self._validate_time(params.get("time_return", ""), "time_return", errors)
            if t:
                params["time_return"] = t
        else:
            tr = params.get("time_return", "")
            if tr:
                t = self._validate_time(tr, "time_return", errors)
                if t:
                    params["time_return"] = t

        # station_from / station_to
        station_names_lower = {k.lower() for k in STATION_MAP}
        for field in ["station_from", "station_to"]:
            val = params.get(field, "")
            if not val:
                errors.append(f"{field} is required")
            elif not isinstance(val, str) or val.strip().lower() not in station_names_lower:
                errors.append(f"{field} '{val}' not found in station map")

        # comfort_class
        valid_classes = {"1 class", "2 class", "2 class calm"}
        cc = params.get("comfort_class", "")
        if not isinstance(cc, str) or cc not in valid_classes:
            errors.append(f"comfort_class must be one of: {', '.join(sorted(valid_classes))}")

        # flexibility
        valid_flex = {"FULLFLEX", "SEMIFLEX", "NOFLEX"}
        flex = params.get("flexibility", "")
        if not isinstance(flex, str) or flex not in valid_flex:
            errors.append(f"flexibility must be one of: {', '.join(sorted(valid_flex))}")

        # select_closest_ticket_available
        sct = params.get("select_closest_ticket_available")
        if sct is None or not isinstance(sct, bool):
            errors.append("select_closest_ticket_available must be a boolean")

        # allow_class_fallback (optional, default true)
        acf = params.get("allow_class_fallback")
        if acf is not None and not isinstance(acf, bool):
            errors.append("allow_class_fallback must be a boolean if specified")

        # book_partial (optional, default false)
        bp = params.get("book_partial")
        if bp is not None and not isinstance(bp, bool):
            errors.append("book_partial must be a boolean if specified")

        # skip_weekends / skip_holidays (optional, default true)
        for key in ("skip_weekends", "skip_holidays"):
            val = params.get(key)
            if val is not None and not isinstance(val, bool):
                errors.append(f"{key} must be a boolean if specified")

        # service_types (optional, list of allowed service type strings)
        valid_service_types = set(SERVICE_TYPE_NAMES)
        st = params.get("service_types")
        if st is not None:
            if not isinstance(st, list) or not all(isinstance(s, str) for s in st):
                errors.append("service_types must be a list of strings")
            else:
                invalid = [s for s in st if s not in valid_service_types]
                if invalid:
                    errors.append(
                        f"service_types contains invalid values: {', '.join(invalid)}. "
                        f"Valid values: {', '.join(sorted(valid_service_types))}"
                    )
                if "ALL" in st and len(st) > 1:
                    errors.append("service_types: 'ALL' cannot be combined with other values")

    def _validate_date(self, value: str | date, field_name: str, errors: list[str]) -> date | None:
        """
        Validate a config date. Returns the parsed date or None.

        Accepts a quoted "YYYY-MM-DD" string or a native (unquoted) TOML
        date. Error messages echo the received value, so a subtly wrong one
        (wrong separator, trailing space, impossible day) is visible.
        """
        if isinstance(value, datetime):
            errors.append(f"{field_name} must be a date (YYYY-MM-DD), not a date-time")
            return None
        if isinstance(value, date):
            return value  # native TOML date (unquoted in the config)
        if not value:
            errors.append(f"{field_name} is required")
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                errors.append(f"{field_name} '{value}' is not a real calendar date")
            else:
                errors.append(f"{field_name} '{value}' must be a date formatted YYYY-MM-DD")
            return None

    def _validate_time(
        self, value: str | dt_time, field_name: str, errors: list[str]
    ) -> str | None:
        """
        Validate a config time. Returns the normalised "HH:MM" string or None.

        Accepts a quoted "HH:MM" string or a native (unquoted) TOML time
        (written with seconds, e.g. 06:59:00). Error messages echo the
        received value, so a subtly wrong one is visible.
        """
        if isinstance(value, dt_time):
            return value.strftime("%H:%M")  # native TOML time (unquoted)
        if not value:
            errors.append(f"{field_name} is required")
            return None
        if not isinstance(value, str):
            errors.append(f"{field_name} '{value}' must be a time formatted HH:MM (24-hour)")
            return None
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            if re.fullmatch(r"\d{2}:\d{2}", value):
                errors.append(f"{field_name} '{value}' is not a real time of day (HH:MM, 24-hour)")
            else:
                errors.append(f"{field_name} '{value}' must be a time formatted HH:MM (24-hour)")
            return None
        return value
