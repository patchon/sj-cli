"""CLI argument parsing: an explicit mode flag is required (SPEC §5.6).

Bare invocation prints help and exits 1; --dry-run is an explicit flag,
not an implicit default.
"""

import pytest

from sj_tool import parse_args


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


def test_dry_run_flag_parses():
    args = parse_args(["--dry-run"])
    assert args.dry_run is True
    assert args.book is False


def test_book_flag_parses():
    args = parse_args(["--book"])
    assert args.book is True
    assert args.dry_run is False


def test_dry_run_and_book_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--dry-run", "--book"])
    assert exc_info.value.code == 1
    assert "not allowed with" in capsys.readouterr().err


def test_removed_flag_spellings_are_rejected(capsys):
    # only documented flags are accepted — no hidden aliases (his rule)
    for argv in (["--cancel-bookings", "3HT2NEIL"], ["--login-only"],
                 ["--test-if-already-logged-in"]):
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
    from sj_tool import parse_cancel_dates

    assert parse_cancel_dates("2026-09-16") == (["2026-09-16"], [])
    # comma list: deduped and sorted
    dates, errors = parse_cancel_dates("2026-09-18,2026-09-16,2026-09-16")
    assert (dates, errors) == (["2026-09-16", "2026-09-18"], [])
    # inclusive range
    assert parse_cancel_dates("2026-09-16..2026-09-18") == (
        ["2026-09-16", "2026-09-17", "2026-09-18"], [])
    # single-day range and mixing commas with ranges
    dates, errors = parse_cancel_dates("2026-09-21,2026-09-16..2026-09-17,2026-09-21..2026-09-21")
    assert (dates, errors) == (["2026-09-16", "2026-09-17", "2026-09-21"], [])


def test_parse_cancel_dates_collects_all_errors_before_anything_runs():
    from sj_tool import parse_cancel_dates

    dates, errors = parse_cancel_dates("2026-09-31,2026/09/16,2026-09-20..2026-09-18,,2026-09-16..2026-09-17..2026-09-18")
    assert dates == []
    assert "'2026-09-31' is not a real calendar date" in errors
    assert "'2026/09/16' must be a date formatted YYYY-MM-DD" in errors
    assert "range '2026-09-20..2026-09-18' must run forwards (start before end)" in errors
    assert "empty entry in the date list" in errors
    assert "'2026-09-16..2026-09-17..2026-09-18' must be a single range start..end" in errors


def test_parse_cancel_dates_refuses_ranges_over_a_year():
    from sj_tool import parse_cancel_dates

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
