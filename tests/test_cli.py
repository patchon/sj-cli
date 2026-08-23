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
    assert "'2026/09/16' is not a date (YYYY-MM-DD) or a week (W43, 2027-W02)" in errors
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


def _logged_in_with_config(tmp_path, monkeypatch, **overrides):
    from sj_api_client import cli
    from sj_api_client.config import CfgManager
    from sj_api_client.tokens import TokenManager
    from tests.fakes import future_cfg

    params = future_cfg(**overrides)["search_parameters"]
    lines = ["[auth]", 'email = "a@b.se"', 'password = "x"', "", "[search_parameters]"]
    for k, v in params.items():
        lines.append(f"{k} = {str(v).lower() if isinstance(v, bool) else repr(v)}")
    (tmp_path / "config.toml").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(cli, "CfgManager", lambda: CfgManager(tmp_path / "config.toml"))
    monkeypatch.setattr(cli, "TokenManager", lambda: TokenManager(tmp_path / "token.json"))
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


# --- no hidden flag prefixes ------------------------------------------------


def test_flag_prefixes_are_not_aliases(capsys):
    # argparse would otherwise accept any unique prefix: `--logo` (a typo of
    # --login) must not log the user out
    for argv in (["--logo"], ["--list-t"], ["--login-s"], ["--dry", "--book"]):
        with pytest.raises(SystemExit) as exc_info:
            parse_args(argv)
        assert exc_info.value.code == 1
        assert "unrecognized arguments" in capsys.readouterr().err


def test_empty_cancel_values_are_reported_as_invalid_not_as_no_operation(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--cancel-date", ""])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "● invalid --cancel-date" in out and "empty entry in the date list" in out
    with pytest.raises(SystemExit):
        parse_args(["--cancel-booking", ""])
    assert "● invalid --cancel-booking" in capsys.readouterr().out


def test_cancel_date_range_guard_is_one_year_inclusive():
    from sj_api_client.cli import parse_cancel_dates

    assert parse_cancel_dates("2026-01-01..2026-12-31")[1] == []
    assert parse_cancel_dates("2028-01-01..2028-12-31")[1] == []  # leap year: 366 days
    assert "spans more than a year" in parse_cancel_dates("2026-01-01..2027-01-01")[1][0]


def test_parse_cancel_dates_accepts_week_terms():
    from datetime import date, timedelta

    from sj_api_client.cli import parse_cancel_dates

    monday = date(2026, 10, 19)  # 2026-W43
    week = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    assert parse_cancel_dates("2026-W43, 2026-10-28..2026-10-29") == (
        [*week, "2026-10-28", "2026-10-29"],
        [],
    )
    dates, errors = parse_cancel_dates("W43")  # bare week: this ISO year, 7 days
    assert errors == [] and len(dates) == 7
    assert parse_cancel_dates("W43..2026-10-25") == (
        [],
        ["range 'W43..2026-10-25' mixes a date and a week"],
    )


# --- travel pass selection --------------------------------------------------


def _pass(name, start, end):
    return {
        "name": name,
        "travelPassId": name,
        "startTravelValidityDateTime": f"{start}T00:00:00+01:00",
        "endTravelValidityDateTime": f"{end}T00:00:00+01:00",
    }


def test_pass_covering_the_booking_window_is_picked_without_a_prompt(monkeypatch):
    from datetime import date

    from sj_api_client.cli import resolve_travel_pass

    monkeypatch.setattr("builtins.input", lambda *_a: pytest.fail("prompted"))
    a = _pass("A", "2026-01-01", "2027-01-01")
    b = _pass("B", "2027-01-01", "2028-01-01")  # a renewal bought ahead
    window = (date(2026, 9, 1), date(2026, 10, 30))
    assert resolve_travel_pass([a, b], for_dates=window) is a
    assert resolve_travel_pass([a, b], for_dates=(date(2027, 3, 1), date(2027, 3, 5))) is b


def test_pass_prompt_needs_a_terminal_and_stops_on_eof(monkeypatch, capsys):
    from sj_api_client import cli

    a, b = _pass("A", "2026-01-01", "2027-01-01"), _pass("B", "2026-06-01", "2027-06-01")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.resolve_travel_pass([a, b])
    assert exc.value.code == 1
    assert "● 2 valid travel passes · run in a terminal to choose one" in capsys.readouterr().out

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "ask", lambda _t: "")  # EOF
    with pytest.raises(SystemExit) as exc:
        cli.resolve_travel_pass([a, b])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "● no travel pass selected" in out
    assert "1. A (2026-01-01 → 2026-12-31)" in out  # the last valid day, not the exclusive end

    answers = iter(["x", "2"])
    monkeypatch.setattr(cli, "ask", lambda _t: next(answers))
    assert cli.resolve_travel_pass([a, b]) is b
    assert "invalid selection, try again" in capsys.readouterr().out


def test_no_valid_pass_closes_with_a_status_line(capsys):
    from sj_api_client.cli import resolve_travel_pass

    with pytest.raises(SystemExit):
        resolve_travel_pass([_pass("Old", "2020-01-01", "2021-01-01")])
    assert "● no valid travel pass found" in capsys.readouterr().out


def test_listing_modes_never_prompt_for_a_pass(tmp_path, monkeypatch, capsys):
    class TwoPasses(_StubClient):
        def get_travel_passes(self, _token):
            return [_pass("A", "2026-01-01", "2027-01-01"), _pass("B", "2026-06-01", "2027-06-01")]

    cli = _logged_in_with_config(tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "handle_list_bookings", lambda *_a, **_k: None)
    cli._run(parse_args(["--list-bookings"]), TwoPasses())
    assert "travelpass   B" in capsys.readouterr().out  # the longest-lived pass sets the range
    cli._run(parse_args(["--list-travelpasses"]), TwoPasses())
    out = capsys.readouterr().out
    assert "● 2 travel pass(es)" in out and "select pass" not in out


def test_list_travelpasses_shows_expired_passes_instead_of_failing(tmp_path, monkeypatch, capsys):
    class Expired(_StubClient):
        def get_travel_passes(self, _token):
            return [_pass("Old", "2020-01-01", "2021-01-01")]

    cli = _logged_in_with_config(tmp_path, monkeypatch)
    cli._run(parse_args(["--list-travelpasses"]), Expired())
    out = capsys.readouterr().out
    assert "2020-01-01 – 2020-12-31 (expired)" in out and "● 1 travel pass(es)" in out


def test_receipt_failure_does_not_hide_the_pass_cards(capsys):
    from sj_api_client.cli import handle_list_travelpasses
    from sj_api_client.errors import SJAPIError

    class NoReceipts:
        def get_receipt_search(self, _booking_id):
            raise SJAPIError({"errorCode": "31100", "message": "trace disabled"})

    tp = {**_pass("A", "2026-01-01", "2027-01-01"), "travelPassCreationBookingId": "B1"}
    handle_list_travelpasses(NoReceipts(), [tp])
    out = capsys.readouterr().out
    assert "! could not fetch the receipt for A: 31100 · trace disabled" in out
    assert "● 1 travel pass(es)" in out


# --- dates: past date_start, --cancel-date needs only the route -------------


def test_cancel_date_ignores_the_config_dates(tmp_path, monkeypatch):
    cli = _logged_in_with_config(
        tmp_path, monkeypatch, date_start="2020-01-01", date_end="2020-02-01"
    )
    monkeypatch.setattr(cli, "handle_cancel_mode", lambda *_a, **_k: True)
    monkeypatch.setattr(
        cli, "validate_dates_against_pass", lambda *_a, **_k: pytest.fail("dates checked")
    )
    cli._run(parse_args(["--cancel-date", "2026-09-15"]), _StubClient())  # exit 0


def test_pass_validation_uses_the_effective_start(capsys):
    from datetime import date

    from sj_api_client.cli import validate_dates_against_pass
    from tests.fakes import base_cfg

    tp = _pass("A", "2026-06-01", "2027-01-01")
    cfg = base_cfg(date_start="2026-01-01", date_end="2026-10-30")  # started before the pass
    validate_dates_against_pass(cfg, tp, today=date(2026, 8, 22))  # today is inside: fine
    with pytest.raises(SystemExit):
        validate_dates_against_pass(cfg, tp, today=date(2026, 3, 1))
    assert "● search dates (2026-03-01 – 2026-10-30) are outside travel pass validity" in (
        capsys.readouterr().out
    )


# --- --login verdict and failure lines --------------------------------------


def test_login_verdict_comes_from_the_session_just_established(tmp_path, monkeypatch, capsys):
    import time

    cli = _logged_in_with_config(tmp_path, monkeypatch)

    def login(_client, tm, _email, _password):
        now = int(time.time())
        tm.token = {"access_token": "fresh", "expires_on": now + 900, "refresh_token": "r"}
        tm.save_error = "disk full"  # the cache could not be written
        return "fresh", "full"

    monkeypatch.setattr(cli, "ensure_authenticated", login)
    with pytest.raises(SystemExit) as exc:
        cli._run(parse_args(["--login"]), _StubClient())
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "● logged in" in out  # not "no cached login found" from re-reading the disk
    assert "! token cache not saved: disk full · the next run will need to log in again" in out


def test_failures_close_with_a_status_line(tmp_path, monkeypatch, capsys):
    from sj_api_client.errors import SJAuthError

    cli = _logged_in_with_config(tmp_path, monkeypatch)

    def down(*_a):
        raise SJAuthError("token refresh failed: timed out")

    monkeypatch.setattr(cli, "ensure_authenticated", down)
    with pytest.raises(SystemExit) as exc:
        cli._run(parse_args(["--book", "--dry-run"]), _StubClient())
    assert exc.value.code == 1
    assert "● token refresh failed: timed out" in capsys.readouterr().out

    class NoMembership(_StubClient):
        def get_membership(self, _token):
            raise RuntimeError("503 Service Unavailable")

    cli = _logged_in_with_config(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        cli._run(parse_args(["--book", "--dry-run"]), NoMembership())
    assert "● initialization failed: 503 Service Unavailable" in capsys.readouterr().out

    cli = _logged_in_with_config(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "handle_list_bookings", lambda *_a, **_k: 1 / 0)
    with pytest.raises(SystemExit):
        cli._run(parse_args(["--list-bookings"]), _StubClient())
    assert "● error: division by zero" in capsys.readouterr().out
