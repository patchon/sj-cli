"""CLI argument parsing: an explicit mode flag is required (SPEC §5.6).

Bare invocation prints help and exits 1; --dry-run is an explicit flag,
not an implicit default.
"""

import sys

import pytest

from sj_api_client.cli import parse_args


def test_no_args_prints_help_and_exits_1(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_args([])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--dry-run" in err
    assert "--book" in err
    # full help closes with the red status line naming the problem, then a blank
    assert "● no operation given, choose one of the flags above" in err
    assert err.endswith("\n\n")


def test_unrecognized_argument_shows_help_and_status(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["help"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--dry-run" in err  # the full help, not just the usage line
    assert "● unrecognized arguments: help" in err
    assert err.endswith("\n\n")


def test_help_flag_ends_with_blank_line(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["-h"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--dry-run" in out
    assert out.endswith("\n\n")


def test_dry_run_is_a_modifier_not_an_operation(capsys):
    # bare --dry-run: no operation given
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--dry-run"])
    assert exc_info.value.code == 1
    assert "no operation given" in capsys.readouterr().err
    # composes with --book and the cancel flags
    args = parse_args(["--book", "--dry-run"])
    assert args.book is True and args.dry_run is True
    args = parse_args(["--cancel-date", "2026-09-16", "--dry-run"])
    assert args.cancel_dates == ["2026-09-16"] and args.dry_run is True
    args = parse_args(["--cancel-booking", "3HT2NEIL", "--dry-run"])
    assert args.cancel_booking_numbers == ["3HT2NEIL"] and args.dry_run is True


def test_dry_run_rejected_for_modes_with_nothing_to_preview(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--list-bookings", "--dry-run"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "● --dry-run only applies to --book, --cancel-date and --cancel-booking" in err


def test_book_flag_parses():
    args = parse_args(["--book"])
    assert args.book is True
    assert args.dry_run is False


def test_removed_flag_spellings_are_rejected(capsys):
    # only documented flags are accepted — no hidden aliases (his rule)
    for argv in (
        ["--cancel-bookings", "3HT2NEIL"],
        ["--login-only"],
        ["--test-if-already-logged-in"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            parse_args(argv)
        assert exc_info.value.code == 1
        capsys.readouterr()


def test_logout_flag_parses():
    assert parse_args(["--logout"]).logout is True


def test_other_modes_still_parse_alone():
    assert parse_args(["--list-bookings"]).list_bookings is True
    assert parse_args(["--list-travelpasses"]).list_travelpasses is True
    assert parse_args(["--login"]).login is True
    assert parse_args(["--login-status"]).login_status is True
    assert parse_args(["--cancel-date", "2026-01-20"]).cancel_date == "2026-01-20"
    assert parse_args(["--cancel-booking", "3HT2NEIL"]).cancel_booking == "3HT2NEIL"


def test_parse_cancel_dates_single_list_and_ranges():
    from sj_api_client.cli import parse_cancel_dates

    assert parse_cancel_dates("2026-09-16") == (["2026-09-16"], [])
    # comma list: deduped and sorted
    dates, errors = parse_cancel_dates("2026-09-18,2026-09-16,2026-09-16")
    assert (dates, errors) == (["2026-09-16", "2026-09-18"], [])
    # inclusive range
    assert parse_cancel_dates("2026-09-16..2026-09-18") == (
        ["2026-09-16", "2026-09-17", "2026-09-18"],
        [],
    )
    # single-day range and mixing commas with ranges
    dates, errors = parse_cancel_dates("2026-09-21,2026-09-16..2026-09-17,2026-09-21..2026-09-21")
    assert (dates, errors) == (["2026-09-16", "2026-09-17", "2026-09-21"], [])


def test_parse_cancel_dates_collects_all_errors_before_anything_runs():
    from sj_api_client.cli import parse_cancel_dates

    dates, errors = parse_cancel_dates(
        "2026-09-31,2026/09/16,2026-09-20..2026-09-18,,2026-09-16..2026-09-17..2026-09-18"
    )
    assert dates == []
    assert "'2026-09-31' is not a real calendar date" in errors
    assert "'2026/09/16' must be a date formatted YYYY-MM-DD" in errors
    assert "range '2026-09-20..2026-09-18' must run forwards (start before end)" in errors
    assert "empty entry in the date list" in errors
    assert "'2026-09-16..2026-09-17..2026-09-18' must be a single range start..end" in errors


def test_parse_cancel_dates_refuses_ranges_over_a_year():
    from sj_api_client.cli import parse_cancel_dates

    dates, errors = parse_cancel_dates("2026-01-01..2027-06-01")
    assert dates == []
    assert "range '2026-01-01..2027-06-01' spans more than a year" in errors


def test_cancel_date_flag_validates_before_running(capsys):
    args = parse_args(["--cancel-date", "2026-09-18,2026-09-16..2026-09-17"])
    assert args.cancel_dates == ["2026-09-16", "2026-09-17", "2026-09-18"]
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--cancel-date", "2026-09-31"])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "● invalid --cancel-date" in out
    assert "'2026-09-31' is not a real calendar date" in out


def test_cancel_booking_numbers_are_validated_and_uppercased():
    args = parse_args(["--cancel-booking", "3ht2neil,ABCD1234,3ht2neil"])
    assert args.cancel_booking_numbers == ["3HT2NEIL", "ABCD1234"]


def test_cancel_booking_rejects_dates_with_a_hint(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--cancel-booking", "2026-09-18,2026-09-18..2026-09-25"])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "● invalid --cancel-booking" in out
    assert "'2026-09-18' is not a booking number (letters and digits only, e.g. 3HT2NEIL)" in out
    assert "'2026-09-18..2026-09-25' is not a booking number" in out
    assert "these look like dates — did you mean --cancel-date?" in out


def test_cancel_booking_rejects_empty_entries(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--cancel-booking", "3HT2NEIL,,ABCD1234"])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "empty entry in the booking number list" in out
    assert "did you mean" not in out  # hint only when tokens look like dates


# --- first-run setup gate in _run (missing config) --------------------------


class _NoCallClient:
    def __getattr__(self, name):
        raise AssertionError(f"client.{name} must not be called before the config exists")


def _missing_config(tmp_path, monkeypatch, *, tty, create):
    """Point the cli at a missing config under tmp_path; `create` replaces the wizard."""
    from sj_api_client import cli
    from sj_api_client.config import CfgManager

    monkeypatch.setattr(cli, "CfgManager", lambda: CfgManager(tmp_path / "config.toml"))
    monkeypatch.setattr(CfgManager, "create_interactive", create)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
    return cli


def test_missing_config_other_operations_never_offer_setup(tmp_path, monkeypatch, capsys):
    cli = _missing_config(
        tmp_path, monkeypatch, tty=True, create=lambda _s: pytest.fail("setup offered by --book")
    )
    with pytest.raises(SystemExit) as exc:
        cli._run(parse_args(["--book", "--dry-run"]), _NoCallClient())
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "● no configuration" in out
    assert f"expected a config file at {tmp_path / 'config.toml'}" in out
    assert "run --login in a terminal to create it" in out


def test_missing_config_login_without_a_terminal_shows_the_card(tmp_path, monkeypatch, capsys):
    cli = _missing_config(
        tmp_path, monkeypatch, tty=False, create=lambda _s: pytest.fail("setup offered without tty")
    )
    with pytest.raises(SystemExit) as exc:
        cli._run(parse_args(["--login"]), _NoCallClient())
    assert exc.value.code == 1
    assert "expected a config file at" in capsys.readouterr().out


def test_missing_config_login_declined_hints_without_repeating_the_path(
    tmp_path, monkeypatch, capsys
):
    cli = _missing_config(tmp_path, monkeypatch, tty=True, create=lambda _s: False)
    with pytest.raises(SystemExit) as exc:
        cli._run(parse_args(["--login"]), _NoCallClient())
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "● no configuration" in out
    assert "re-run --login when you want to create it" in out
    assert "expected a config file" not in out  # the wizard already named the path


def test_missing_config_login_created_continues_into_the_login(tmp_path, monkeypatch, capsys):
    from sj_api_client.errors import SJAuthError

    def create(self):
        self.path.write_text('[auth]\nemail = "a@b.se"\npassword = "x"\n')
        return True

    cli = _missing_config(tmp_path, monkeypatch, tty=True, create=create)
    seen = {}

    def login(_client, _tm, email, password):
        seen["creds"] = (email, password)
        raise SJAuthError("stop here")

    monkeypatch.setattr(cli, "ensure_authenticated", login)
    with pytest.raises(SystemExit):
        cli._run(parse_args(["--login"]), _NoCallClient())
    assert seen["creds"] == ("a@b.se", "x")  # the fresh config was loaded and used
    assert "● login failed" in capsys.readouterr().out


def test_missing_config_write_failure_shows_its_own_card(tmp_path, monkeypatch, capsys):
    from sj_api_client.errors import SJConfigError

    def create(_self):
        raise SJConfigError("could not write config at /x: Permission denied")

    cli = _missing_config(tmp_path, monkeypatch, tty=True, create=create)
    with pytest.raises(SystemExit) as exc:
        cli._run(parse_args(["--login"]), _NoCallClient())
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "● config not created" in out
    assert "Permission denied" in out


# --- exit codes after the run (SPEC §5.7) -----------------------------------


class _StubClient:
    """Enough of SJClient for _run to reach the mode dispatch."""

    def get_membership(self, _token):
        return {"firstName": "A", "lastName": "B"}

    def get_travel_passes(self, _token):
        return [{"name": "Pass", "travelPassId": "TP"}]

    def resolve_station(self, name):
        return name


def _logged_in_with_config(tmp_path, monkeypatch):
    from sj_api_client import cli
    from sj_api_client.config import CfgManager
    from tests.fakes import base_cfg

    params = base_cfg()["search_parameters"]
    lines = ["[auth]", 'email = "a@b.se"', 'password = "x"', "", "[search_parameters]"]
    for k, v in params.items():
        lines.append(f"{k} = {str(v).lower() if isinstance(v, bool) else repr(v)}")
    (tmp_path / "config.toml").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(cli, "CfgManager", lambda: CfgManager(tmp_path / "config.toml"))
    monkeypatch.setattr(cli, "ensure_authenticated", lambda *_a: ("tok", "cached"))
    monkeypatch.setattr(cli, "fetch_all_bookings", lambda *_a, **_k: [])
    monkeypatch.setattr(cli, "cleanup_stale_provisionals", lambda *_a, **_k: [])
    return cli


@pytest.mark.parametrize(
    ("counts", "code"),
    [
        ({"days": 2, "booked": 2}, None),
        ({"days": 2, "booked": 1, "unavailable": 1}, None),  # no offer is a skip, not a failure
        ({"days": 2, "booked": 1, "failed": 1}, 1),
        ({"days": 1, "error": 1}, 1),
    ],
)
def test_book_exit_code_follows_the_day_counts(tmp_path, monkeypatch, capsys, counts, code):
    cli = _logged_in_with_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "process_date_range", lambda *_a, **_k: counts)
    if code is None:
        cli._run(parse_args(["--book"]), _StubClient())
    else:
        with pytest.raises(SystemExit) as exc:
            cli._run(parse_args(["--book"]), _StubClient())
        assert exc.value.code == code


@pytest.mark.parametrize(("ok", "code"), [(True, None), (False, 1)])
def test_cancel_booking_exit_code_follows_the_outcome(tmp_path, monkeypatch, ok, code):
    cli = _logged_in_with_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "handle_cancel_booking", lambda *_a, **_k: ok)
    if code is None:
        cli._run(parse_args(["--cancel-booking", "ABCD1234"]), _StubClient())
    else:
        with pytest.raises(SystemExit) as exc:
            cli._run(parse_args(["--cancel-booking", "ABCD1234"]), _StubClient())
        assert exc.value.code == code
