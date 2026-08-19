"""Configuration loading and validation for the SJ API client."""

import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

import tomllib

from sj_calendar import sweden_now
from sj_client import STATION_MAP
from sj_errors import SJConfigError
from sj_logger import log_json

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
        path: The path to the configuration file to be loaded.
        cfg: The dictionary containing the loaded configuration data.

    """

    DEFAULT_PATH = (
        Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "sj-api-client"
        / "config.toml"
    )

    def __init__(self, cfg_path=None):
        self.path = Path(cfg_path) if cfg_path else self.DEFAULT_PATH
        self.cfg = {}
        logger.debug(f"initialized config manager with path {self.path}")

    def load(self) -> dict:
        """
        Loads and parses the configuration file.

        Returns:
            The parsed configuration settings.

        Raises:
            SJConfigError: If there is an error reading or parsing the file.

        """
        try:
            with self.path.open("rb") as f:
                self.cfg = tomllib.load(f)
                logger.debug(f"loading config {log_json(self.cfg)} from {self.path}")
            return self.cfg
        except Exception as e:
            raise SJConfigError(f"failed to parse toml config at {self.path}: {e}") from e

    def verify_cfg(self, cfg: dict) -> None:
        """
        Validates all configuration fields.

        Collects all errors and reports them at once.

        Raises:
            SJConfigError: If any validation errors are found.

        """
        errors: list[str] = []

        # [auth] section
        auth = cfg.get("auth")
        if not auth:
            errors.append("[auth] section is missing")
        else:
            email = auth.get("email", "")
            if not email or "@" not in email or "." not in email.split("@")[-1]:
                errors.append("email must be a valid email address (x@y.z)")
            if not auth.get("password"):
                errors.append("password must be a non-empty string")

        # [search_parameters] section
        params = cfg.get("search_parameters")
        if not params:
            errors.append("[search_parameters] section is missing")
        else:
            self._validate_search_params(params, errors)

        if errors:
            raise SJConfigError(
                "configuration errors:\n  - " + "\n  - ".join(errors)
            )

    def _validate_search_params(self, params: dict, errors: list[str]) -> None:
        """Validate the [search_parameters] section."""
        # date_start
        date_start = self._validate_date(params.get("date_start", ""), "date_start", errors)

        # date_end
        date_end = self._validate_date(params.get("date_end", ""), "date_end", errors)

        # date_end >= date_start
        if date_start and date_end and date_end < date_start:
            errors.append("date_end must be >= date_start")

        # date_start must be today or in the future
        if date_start and date_start < sweden_now().date():
            errors.append("date_start must be today or in the future")

        # time_leave
        self._validate_time(params.get("time_leave", ""), "time_leave", errors)

        # roundtrip
        roundtrip = params.get("roundtrip")
        if roundtrip is None or not isinstance(roundtrip, bool):
            errors.append("roundtrip must be a boolean (true/false)")
        elif roundtrip:
            self._validate_time(params.get("time_return", ""), "time_return", errors)
        else:
            tr = params.get("time_return", "")
            if tr:
                self._validate_time(tr, "time_return", errors)

        # station_from / station_to
        station_names_lower = {k.lower() for k in STATION_MAP}
        for field in ["station_from", "station_to"]:
            val = params.get(field, "")
            if not val:
                errors.append(f"{field} is required")
            elif val.strip().lower() not in station_names_lower:
                errors.append(f"{field} '{val}' not found in station map")

        # comfort_class
        valid_classes = {"1 class", "2 class", "2 class calm"}
        cc = params.get("comfort_class", "")
        if cc not in valid_classes:
            errors.append(
                f"comfort_class must be one of: {', '.join(sorted(valid_classes))}"
            )

        # flexibility
        valid_flex = {"FULLFLEX", "SEMIFLEX", "NOFLEX"}
        flex = params.get("flexibility", "")
        if flex not in valid_flex:
            errors.append(
                f"flexibility must be one of: {', '.join(sorted(valid_flex))}"
            )

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
                    errors.append(
                        "service_types: 'ALL' cannot be combined with other values"
                    )

    def _validate_date(
        self, value: str, field_name: str, errors: list[str]
    ) -> date | None:
        """Validate a YYYY-MM-DD date string. Returns parsed date or None."""
        if not value:
            errors.append(f"{field_name} is required")
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            errors.append(f"{field_name} must be a valid date (YYYY-MM-DD)")
            return None

    def _validate_time(
        self, value: str, field_name: str, errors: list[str]
    ) -> None:
        """Validate an HH:MM time string (24-hour)."""
        if not value:
            errors.append(f"{field_name} is required")
            return
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
            errors.append(f"{field_name} must be a valid time (HH:MM, 24-hour)")
