"""--login-status must agree with ensure_authenticated's non-interactive ladder:
logged in = valid access token OR usable refresh token (judged offline, SPEC §5.8).
Output is a status card: dot + verdict, then dim-labelled facts (account,
session horizon with relative time, token expiry — kept even when expired)."""

import base64
import json
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sj_cli.cli import handle_login_status
from sj_cli.tokens import TokenManager


def _profile(email):
    blob = base64.urlsafe_b64encode(json.dumps({"preferred_username": email}).encode()).decode()
    return blob.rstrip("=")


def _tm(tmp_path, token=None):
    tm = TokenManager(tmp_path / "token.json")
    if token:
        tm.save(token)
        tm.token = None  # handler must load from disk itself
    return tm


def _fmt(ts):
    dt = datetime.fromtimestamp(ts, tz=UTC).astimezone(ZoneInfo("Europe/Stockholm"))
    return dt.strftime("%a %d %b %H:%M").lower()


def test_valid_access_token_card(tmp_path, capsys):
    now = int(time.time())
    tm = _tm(
        tmp_path,
        {
            "access_token": "a",
            "expires_on": now + 3600,
            "profile_info": _profile("user@example.com"),
        },
    )
    with pytest.raises(SystemExit) as e:
        handle_login_status(tm)
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "● logged in" in out
    # the account lives in the header box (printed by the cli), not the card
    assert "account" not in out
    assert f"  session   valid until {_fmt(now + 3600)} (expires in " in out
    assert f"  token     valid until {_fmt(now + 3600)}" in out
    assert "expired" not in out


def test_refresh_horizon_and_relative_time(tmp_path, capsys):
    now = int(time.time())
    tm = _tm(
        tmp_path,
        {
            "access_token": "a",
            "expires_on": now + 3600,
            "refresh_token": "r",
            "refresh_token_expires_on": now + 86400,
        },
    )
    with pytest.raises(SystemExit) as e:
        handle_login_status(tm)
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert f"  session   valid until {_fmt(now + 86400)} (expires in 23h)" in out
    assert f"  token     valid until {_fmt(now + 3600)}" in out


def test_expired_access_with_valid_refresh_keeps_expiry_time(tmp_path, capsys):
    now = int(time.time())
    tm = _tm(
        tmp_path,
        {
            "access_token": "a",
            "expires_on": now - 1000,
            "refresh_token": "r",
            "refresh_token_expires_on": now + 86400,
            "profile_info": _profile("user@example.com"),
        },
    )
    with pytest.raises(SystemExit) as e:
        handle_login_status(tm)
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "● logged in" in out
    assert "account" not in out
    assert f"  session   valid until {_fmt(now + 86400)} (expires in 23h)" in out
    assert f"  token     expired {_fmt(now - 1000)} · renews automatically on next run" in out


def test_cached_email_reads_profile_offline(tmp_path):
    from sj_cli.cli import _cached_email

    tm = _tm(tmp_path, {"access_token": "a", "profile_info": _profile("user@example.com")})
    assert _cached_email(tm) == "user@example.com"
    assert _cached_email(_tm(tmp_path / "empty")) is None
    corrupt = TokenManager(tmp_path / "c" / "token.json")
    corrupt.path.parent.mkdir()
    corrupt.path.write_text("{not json")
    assert _cached_email(corrupt) is None


def test_expired_access_and_refresh_is_not_logged_in(tmp_path, capsys):
    now = int(time.time())
    tm = _tm(
        tmp_path,
        {
            "access_token": "a",
            "expires_on": now - 1000,
            "refresh_token": "r",
            "refresh_token_expires_on": now - 500,
        },
    )
    with pytest.raises(SystemExit) as e:
        handle_login_status(tm)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "● not logged in" in out
    assert "  session   expired · log in with --login" in out


def test_no_cache_is_not_logged_in(tmp_path, capsys):
    tm = _tm(tmp_path)
    with pytest.raises(SystemExit) as e:
        handle_login_status(tm)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "● not logged in" in out
    assert "  session   no cached login found · log in with --login" in out


def test_login_failed_card_from_api_error(capsys):
    from sj_cli.cli import print_login_failed
    from sj_cli.errors import SJAPIError, SJAuthError

    err = SJAuthError("login failed: AADB2C90054 · Fel uppgifter.")
    err.__cause__ = SJAPIError(
        {
            "status": "400",
            "errorCode": "AADB2C90054",
            "message": "Fel uppgifter.",
        }
    )
    print_login_failed(err)
    out = capsys.readouterr().out
    assert "● login failed" in out
    assert "  reason    Fel uppgifter." in out
    assert "  code      AADB2C90054" in out


def test_login_failed_card_without_api_cause(capsys):
    from sj_cli.cli import print_login_failed
    from sj_cli.errors import SJAuthError

    print_login_failed(SJAuthError("sms code not provided or timed out after 2 minutes"))
    out = capsys.readouterr().out
    assert "● login failed" in out
    assert "  reason    sms code not provided or timed out after 2 minutes" in out


def test_verdict_override_for_already_logged_in(tmp_path, capsys):
    now = int(time.time())
    tm = _tm(tmp_path, {"access_token": "a", "expires_on": now + 3600})
    with pytest.raises(SystemExit) as e:
        handle_login_status(tm, verdict="already logged in")
    assert e.value.code == 0
    assert "● already logged in" in capsys.readouterr().out
