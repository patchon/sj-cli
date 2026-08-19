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
                 "time_leave must be a valid time", "comfort_class must be one of",
                 "flexibility must be one of", "roundtrip must be a boolean",
                 "station_to 'Nowhere' not found", "'ALL' cannot be combined"):
        assert frag in msg, frag


def test_time_return_required_only_for_roundtrip():
    assert "time_return is required" in errors_of(base_cfg(time_return=""))
    verify(base_cfg(roundtrip=False, time_return=""))
    assert "time_return must be a valid time" in errors_of(base_cfg(roundtrip=False, time_return="9:00"))


def test_station_lookup_is_case_insensitive():
    verify(base_cfg(station_from="linköping central", station_to="STOCKHOLM C"))


def test_service_type_names_cover_validation_set():
    assert set(SERVICE_TYPE_NAMES) == {"ALL", "SJ_HIGH", "SJ_IC", "SJ_REG", "SJ_NT",
                                       "X_TRAINOPS", "X_PTA", "X_EXPBUS"}
    assert "service_types contains invalid values: SJ_BUS" in errors_of(base_cfg(service_types=["SJ_BUS"]))
