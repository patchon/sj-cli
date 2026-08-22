import pytest

from sj_config import SERVICE_TYPE_NAMES, CfgManager
from sj_errors import SJConfigError
from tests.conftest import base_cfg


def verify(cfg):
    CfgManager().verify_cfg(cfg)


def errors_of(cfg) -> str:
    with pytest.raises(SJConfigError) as exc:
        verify(cfg)
    return str(exc.value)


def test_valid_config_passes():
    verify(base_cfg())
    verify(base_cfg(roundtrip=False, time_return=""))
    verify(base_cfg(service_types=["ALL"], book_partial=True, skip_weekends=False))


def test_missing_sections():
    msg = errors_of({})
    assert "[auth] section is missing" in msg
    assert "[search_parameters] section is missing" in msg


def test_collects_all_errors():
    cfg = base_cfg(date_start="2020-01-01", date_end="2019-01-01", time_leave="25:00",
                   comfort_class="business", flexibility="FLEX", roundtrip="yes",
                   station_to="Nowhere", service_types=["ALL", "SJ_IC"])
    msg = errors_of(cfg)
    for frag in ("date_end must be >= date_start", "date_start must be today or in the future",
                 "time_leave '25:00' is not a real time of day", "comfort_class must be one of",
                 "flexibility must be one of", "roundtrip must be a boolean",
                 "station_to 'Nowhere' not found", "'ALL' cannot be combined"):
        assert frag in msg, frag


def test_time_return_required_only_for_roundtrip():
    assert "time_return is required" in errors_of(base_cfg(time_return=""))
    verify(base_cfg(roundtrip=False, time_return=""))
    assert "time_return '9:00' must be a time formatted HH:MM" in errors_of(base_cfg(roundtrip=False, time_return="9:00"))


def test_station_lookup_is_case_insensitive():
    verify(base_cfg(station_from="linköping central", station_to="STOCKHOLM C"))


def test_service_type_names_cover_validation_set():
    assert set(SERVICE_TYPE_NAMES) == {"ALL", "SJ_HIGH", "SJ_IC", "SJ_REG", "SJ_NT",
                                       "X_TRAINOPS", "X_PTA", "X_EXPBUS"}
    assert "service_types contains invalid values: SJ_BUS" in errors_of(base_cfg(service_types=["SJ_BUS"]))


def test_config_error_carries_error_list():
    with pytest.raises(SJConfigError) as exc:
        verify(base_cfg(time_leave="25:00"))
    assert exc.value.errors == ["time_leave '25:00' is not a real time of day (HH:MM, 24-hour)"]


def test_native_toml_date_is_accepted_and_normalised():
    import datetime

    cfg = base_cfg()
    cfg["search_parameters"]["date_start"] = datetime.date(2026, 9, 16)
    cfg["search_parameters"]["date_end"] = datetime.date(2026, 9, 21)
    verify(cfg)  # unquoted TOML dates must not crash or error
    assert cfg["search_parameters"]["date_start"] == "2026-09-16"
    assert cfg["search_parameters"]["date_end"] == "2026-09-21"


def test_date_errors_echo_the_value_and_distinguish_causes():
    import datetime

    msg = errors_of(base_cfg(date_start="2026-09-31"))
    assert "date_start '2026-09-31' is not a real calendar date" in msg
    msg = errors_of(base_cfg(date_start="2026/09/30"))
    assert "date_start '2026/09/30' must be a date formatted YYYY-MM-DD" in msg
    msg = errors_of(base_cfg(date_start="2026-09-16 "))
    assert "date_start '2026-09-16 ' must be a date formatted YYYY-MM-DD" in msg
    cfg = base_cfg()
    cfg["search_parameters"]["date_start"] = datetime.datetime(2026, 9, 16, 8, 0)
    with pytest.raises(SJConfigError, match=r"date_start must be a date .YYYY-MM-DD., not a date-time"):
        verify(cfg)


def test_native_toml_time_is_accepted_and_normalised():
    import datetime

    cfg = base_cfg()
    cfg["search_parameters"]["time_leave"] = datetime.time(6, 59)
    cfg["search_parameters"]["time_return"] = datetime.time(17, 22)
    verify(cfg)  # unquoted TOML times (with seconds) must not crash or error
    assert cfg["search_parameters"]["time_leave"] == "06:59"
    assert cfg["search_parameters"]["time_return"] == "17:22"
