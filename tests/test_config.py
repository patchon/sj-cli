import pytest

from sj_api_client.config import SERVICE_TYPE_NAMES, CfgManager
from sj_api_client.errors import SJConfigError
from tests.fakes import base_cfg, future_cfg


def verify(cfg):
    CfgManager().verify_cfg(cfg)


def errors_of(cfg) -> str:
    with pytest.raises(SJConfigError) as exc:
        verify(cfg)
    return str(exc.value)


def test_valid_config_passes():
    verify(future_cfg())
    verify(future_cfg(roundtrip=False, time_return=""))
    verify(future_cfg(service_types=["ALL"], book_partial=True, skip_weekends=False))


def test_past_dates_are_fine_but_a_passed_selection_is_not():
    from datetime import date, timedelta

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    in_a_month = (date.today() + timedelta(days=30)).isoformat()
    verify(future_cfg(dates=f"{yesterday}..{in_a_month}"))  # a standing selection keeps working
    msg = errors_of(base_cfg(dates="2020-01-01..2020-03-20"))
    assert "dates: all selected dates have passed" in msg
    # operations that never touch the dates (--cancel-date) skip the date rules
    CfgManager().verify_cfg(base_cfg(dates="2020-01-01..2019-01-01"), require_dates=False)
    with pytest.raises(SJConfigError, match="station_to 'Nowhere'"):
        CfgManager().verify_cfg(base_cfg(station_to="Nowhere"), require_dates=False)


def test_dates_required_typed_and_old_keys_rejected():
    import datetime

    cfg = base_cfg()
    del cfg["search_parameters"]["dates"]
    assert "dates is required" in errors_of(cfg)
    cfg = base_cfg()
    cfg["search_parameters"]["dates"] = datetime.date(2099, 9, 16)  # native TOML date
    assert 'dates must be a string like "2026-09-01..2026-10-30"' in errors_of(cfg)
    msg = errors_of(base_cfg(date_start="2099-09-16", date_end="2099-09-21"))
    assert 'date_start/date_end were replaced by dates = "START..END"' in msg
    assert "dates is required" not in msg  # the migration hint is enough


def test_dates_are_normalised_in_place():
    cfg = base_cfg(dates=" 2099-09-16 ,w45..46")
    verify(cfg)
    assert cfg["search_parameters"]["dates"] == "2099-09-16, W45..46"


def test_missing_sections():
    msg = errors_of({})
    assert "[auth] section is missing" in msg
    assert "[search_parameters] section is missing" in msg


def test_collects_all_errors():
    cfg = base_cfg(
        dates="2020-01-01..2019-01-01",
        time_leave="25:00",
        comfort_class="business",
        flexibility="FLEX",
        roundtrip="yes",
        station_to="Nowhere",
        service_types=["ALL", "SJ_IC"],
    )
    msg = errors_of(cfg)
    for frag in (
        "dates: range '2020-01-01..2019-01-01' must run forwards (start before end)",
        "time_leave '25:00' is not a real time of day",
        "comfort_class must be one of",
        "flexibility must be one of",
        "roundtrip must be a boolean",
        "station_to 'Nowhere' not found",
        "'ALL' cannot be combined",
    ):
        assert frag in msg, frag


def test_time_return_required_only_for_roundtrip():
    assert "time_return is required" in errors_of(base_cfg(time_return=""))
    verify(future_cfg(roundtrip=False, time_return=""))
    assert "time_return '9:00' must be a time formatted HH:MM" in errors_of(
        base_cfg(roundtrip=False, time_return="9:00")
    )


def test_station_lookup_is_case_insensitive():
    verify(future_cfg(station_from="linköping central", station_to="STOCKHOLM C"))


def test_service_type_names_cover_validation_set():
    assert set(SERVICE_TYPE_NAMES) == {
        "ALL",
        "SJ_HIGH",
        "SJ_IC",
        "SJ_REG",
        "SJ_NT",
        "X_TRAINOPS",
        "X_PTA",
        "X_EXPBUS",
    }
    assert "service_types contains invalid values: SJ_BUS" in errors_of(
        base_cfg(service_types=["SJ_BUS"])
    )


def test_config_error_carries_error_list():
    with pytest.raises(SJConfigError) as exc:
        verify(base_cfg(time_leave="25:00"))
    assert exc.value.errors == ["time_leave '25:00' is not a real time of day (HH:MM, 24-hour)"]


def test_date_errors_echo_the_value_and_distinguish_causes():
    msg = errors_of(base_cfg(dates="2026-09-31"))
    assert "dates: '2026-09-31' is not a real calendar date" in msg
    msg = errors_of(base_cfg(dates="2026/09/30"))
    assert "dates: '2026/09/30' is not a date (YYYY-MM-DD) or a week (W43, 2027-W02)" in msg
    msg = errors_of(base_cfg(dates="2027-W53"))
    assert "dates: 2027 has no week 53" in msg


def test_native_toml_time_is_accepted_and_normalised():
    import datetime

    cfg = future_cfg()
    cfg["search_parameters"]["time_leave"] = datetime.time(6, 59)
    cfg["search_parameters"]["time_return"] = datetime.time(17, 22)
    verify(cfg)  # unquoted TOML times (with seconds) must not crash or error
    assert cfg["search_parameters"]["time_leave"] == "06:59"
    assert cfg["search_parameters"]["time_return"] == "17:22"


def test_load_missing_file_says_so_not_parse_error(tmp_path):
    with pytest.raises(SJConfigError, match="no config file found at"):
        CfgManager(tmp_path / "config.toml").load()


def test_create_interactive_writes_template_with_credentials(tmp_path, monkeypatch, capsys):
    from sj_api_client import config as m

    answers = iter(["y", "not-an-email", "user@example.com"])
    monkeypatch.setattr(m, "ask", lambda _t: next(answers))
    passwords = iter(["", 's3cr3t"quote'])
    monkeypatch.setattr(m.getpass, "getpass", lambda _p="": next(passwords))
    cm = CfgManager(tmp_path / "config.toml")
    assert cm.create_interactive() is True
    text = cm.path.read_text()
    assert 'email = "user@example.com"' in text
    assert 'password = "s3cr3t\\"quote"' in text
    assert "[search_parameters]" in text  # the documented template came along
    assert (cm.path.stat().st_mode & 0o777) == 0o600  # holds the password
    out = capsys.readouterr().out
    assert "! 'not-an-email' is not an email address" in out
    assert "! password must not be empty" in out
    assert "● config created" in out
    cm.load()  # the created file is valid TOML


def test_create_interactive_declined_leaves_nothing(tmp_path, monkeypatch):
    from sj_api_client import config as m

    monkeypatch.setattr(m, "ask", lambda _t: "n")
    cm = CfgManager(tmp_path / "config.toml")
    assert cm.create_interactive() is False
    assert not cm.path.exists()


@pytest.mark.parametrize("secret", ["p\\ass", "x\\ny", "back\\\\slash", 'q"uo\\te', "tab\\there"])
def test_create_interactive_round_trips_backslashes_and_quotes(tmp_path, monkeypatch, secret):
    # re.sub with a string replacement would re-process the escaped backslashes
    from sj_api_client import config as m

    monkeypatch.setattr(m, "ask", lambda _t: "y" if "create" in _t else "user@example.com")
    monkeypatch.setattr(m.getpass, "getpass", lambda _p="": secret)
    cm = CfgManager(tmp_path / "config.toml")
    assert cm.create_interactive() is True
    assert cm.load()["auth"]["password"] == secret


def test_create_interactive_ctrl_d_on_password_reasks(tmp_path, monkeypatch, capsys):
    from sj_api_client import config as m

    monkeypatch.setattr(m, "ask", lambda _t: "y" if "create" in _t else "user@example.com")
    attempts = iter([EOFError(), "pw"])

    def getpass(_p=""):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(m.getpass, "getpass", getpass)
    cm = CfgManager(tmp_path / "config.toml")
    assert cm.create_interactive() is True
    assert "! password must not be empty" in capsys.readouterr().out
    assert cm.load()["auth"]["password"] == "pw"


def test_create_interactive_write_failure_is_a_config_error(tmp_path, monkeypatch, capsys):
    from sj_api_client import config as m

    monkeypatch.setattr(m, "ask", lambda _t: "y" if "create" in _t else "user@example.com")
    monkeypatch.setattr(m.getpass, "getpass", lambda _p="": "pw")
    (tmp_path / "blocker").write_text("")  # a file where the config directory should be
    cm = CfgManager(tmp_path / "blocker" / "config.toml")
    with pytest.raises(SJConfigError, match="could not write config at"):
        cm.create_interactive()
    assert "✗ writing config" in capsys.readouterr().out


def test_load_unreadable_file_is_not_reported_as_a_parse_error(tmp_path):
    cfg_dir = tmp_path / "config.toml"
    cfg_dir.mkdir()  # exists, but is a directory
    with pytest.raises(SJConfigError, match="cannot read config at"):
        CfgManager(cfg_dir).load()
    bad = tmp_path / "bad.toml"
    bad.write_text("[auth\n")
    with pytest.raises(SJConfigError, match="failed to parse toml config at"):
        CfgManager(bad).load()


def test_verify_cfg_search_params_scoped_to_operations_that_use_them():
    # a fresh wizard config has valid [auth] but stale template search dates:
    # login/list/cancel-booking must still work
    cfg = base_cfg(dates="2020-01-01..2020-03-20")
    with pytest.raises(SJConfigError):
        CfgManager().verify_cfg(cfg)  # booking modes: full validation
    CfgManager().verify_cfg(cfg, require_search=False)  # non-booking: [auth] only

    # a missing [search_parameters] section is fine when not required
    CfgManager().verify_cfg({"auth": cfg["auth"]}, require_search=False)

    # [auth] is enforced regardless of the operation
    bad = base_cfg()
    bad["auth"]["email"] = "not-an-email"
    with pytest.raises(SJConfigError, match="email must be a valid email"):
        CfgManager().verify_cfg(bad, require_search=False)


def test_wrong_typed_values_are_collected_not_crashed():
    cfg = base_cfg(flexibility=["FULLFLEX"], station_from=740000009, comfort_class=2)
    cfg["auth"] = {"email": 123, "password": ["x"]}
    with pytest.raises(SJConfigError) as exc:
        CfgManager().verify_cfg(cfg)
    msgs = "\n".join(exc.value.errors)
    assert "email must be a valid email address" in msgs
    assert "password must be a non-empty string" in msgs
    assert "station_from '740000009' not found in station map" in msgs
    assert "comfort_class must be one of" in msgs and "flexibility must be one of" in msgs
    with pytest.raises(SJConfigError, match=r"\[auth\] section is missing"):
        CfgManager().verify_cfg({"auth": "x", "search_parameters": "y"})
