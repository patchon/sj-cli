"""Auth-flow output discipline: routine token-lifecycle transitions are
logger-only — stdout stays quiet unless user interaction is needed (SMS)
or a result card is printed (SPEC §10.2)."""

import time

from sj_api_client.auth import ensure_authenticated, ensure_valid_token
from sj_api_client.tokens import TokenManager


class FakeRefreshClient:
    def __init__(self, new_token):
        self._new_token = new_token
        self.calls = []

    def refresh_token(self, refresh_token):
        self.calls.append("refresh")
        return dict(self._new_token)


def _fresh_token(now):
    return {
        "access_token": "new",
        "expires_on": now + 3600,
        "refresh_token": "r2",
        "refresh_token_expires_on": now + 86400,
    }


def test_proactive_refresh_is_silent(tmp_path, capsys):
    now = int(time.time())
    tm = TokenManager(tmp_path / "token.json")
    tm.save(
        {
            "access_token": "a",
            "expires_on": now + 3600,
            "refresh_token": "r",
            "refresh_token_expires_on": now + 600,  # within renewal threshold
        }
    )
    fc = FakeRefreshClient(_fresh_token(now))
    token, method = ensure_authenticated(fc, tm, "e@x.se", "pw")
    assert token == "new"
    assert method == "refreshed"
    assert fc.calls == ["refresh"]
    assert capsys.readouterr().out == ""


def test_midrun_refresh_is_silent(tmp_path, capsys):
    now = int(time.time())
    tm = TokenManager(tmp_path / "token.json")
    tm.save(
        {
            "access_token": "a",
            "expires_on": now - 100,  # expired mid-run
            "refresh_token": "r",
            "refresh_token_expires_on": now + 86400,
        }
    )
    tm.load()
    fc = FakeRefreshClient(_fresh_token(now))
    token = ensure_valid_token(fc, tm, "a")
    assert token == "new"
    assert capsys.readouterr().out == ""


class FakeLoginClient:
    def __init__(self, mfa=True):
        self.mfa = mfa
        self.calls = []

    def initiate_login(self, email, password):
        self.calls.append("initiate")
        return self.mfa

    def sms_trigger(self):
        self.calls.append("trigger")

    def sms_verify(self, code):
        self.calls.append(("verify", code))

    def finalize_login(self):
        self.calls.append("finalize")
        return "authcode"

    def exchange_code(self, code):
        self.calls.append("exchange")
        return {"access_token": "t"}

    def export_cookies(self):
        return []


def test_full_login_trail_is_human(monkeypatch, capsys):
    from sj_api_client import auth

    monkeypatch.setattr(auth, "read_sms_code", lambda: "534734")
    fc = FakeLoginClient()
    token = auth.perform_full_login(fc, "e@x.se", "pw")
    assert token == {"access_token": "t"}
    assert ("verify", "534734") in fc.calls
    out = capsys.readouterr().out
    assert "✓ performing login" in out
    assert "✓ sending sms code" in out
    assert "✓ verifying sms code" in out
    assert "✓ completing login" in out
    assert "sms verification required" not in out
    assert "finalizing" not in out
    assert "exchanging" not in out
    # a blank line separates the trail from whatever follows (card, title)
    assert out.endswith("\n\n")


def test_sms_prompt_inline_with_marker(monkeypatch, capsys):
    import io
    import sys as _sys

    from sj_api_client import auth

    monkeypatch.setattr(auth.select, "select", lambda *_: ([_sys.stdin], [], []))
    monkeypatch.setattr(_sys, "stdin", io.StringIO("534734\n"))
    assert auth.read_sms_code(timeout_seconds=120) == "534734"
    # inline prompt with the ? marker; newline added because stdin isn't a tty
    assert capsys.readouterr().out == " ? enter sms code (timeout 2m): \n"


def test_sms_prompt_timeout_closes_line(monkeypatch, capsys):
    from sj_api_client import auth

    monkeypatch.setattr(auth.select, "select", lambda *_: ([], [], []))
    assert auth.read_sms_code(timeout_seconds=120) is None
    assert capsys.readouterr().out == " ? enter sms code (timeout 2m): \n"


def test_valid_cached_token_reports_cached(tmp_path, capsys):
    now = int(time.time())
    tm = TokenManager(tmp_path / "token.json")
    tm.save(
        {
            "access_token": "a",
            "expires_on": now + 3600,
            "refresh_token": "r",
            "refresh_token_expires_on": now + 86400,
        }
    )
    token, method = ensure_authenticated(object(), tm, "e@x.se", "pw")
    assert token == "a"
    assert method == "cached"
    assert capsys.readouterr().out == ""


def test_full_login_reports_full(monkeypatch, tmp_path):
    from sj_api_client import auth

    monkeypatch.setattr(auth, "read_sms_code", lambda: "534734")
    tm = TokenManager(tmp_path / "token.json")  # empty cache → full login
    token, method = auth.ensure_authenticated(FakeLoginClient(), tm, "e@x.se", "pw")
    assert token == "t"
    assert method == "full"


def test_wrong_sms_code_retries_then_succeeds(monkeypatch, capsys):
    from sj_api_client import auth
    from sj_api_client.errors import SJAPIError

    class RejectFirstClient(FakeLoginClient):
        def sms_verify(self, code):
            self.calls.append(("verify", code))
            if code == "111111":
                raise SJAPIError({"status": "449"})

    codes = iter(["111111", "222222"])
    monkeypatch.setattr(auth, "read_sms_code", lambda: next(codes))
    fc = RejectFirstClient()
    token = auth.perform_full_login(fc, "e@x.se", "pw")
    assert token == {"access_token": "t"}
    assert fc.calls.count("trigger") == 1  # same sms, never re-sent
    assert ("verify", "111111") in fc.calls
    assert ("verify", "222222") in fc.calls
    out = capsys.readouterr().out
    assert "! sms code rejected, 2 attempt(s) left" in out


def test_wrong_sms_code_exhausts_attempts(monkeypatch):
    import pytest

    from sj_api_client import auth
    from sj_api_client.errors import SJAPIError, SJAuthError

    class WrongCodeClient(FakeLoginClient):
        def sms_verify(self, code):
            self.calls.append(("verify", code))
            raise SJAPIError({"status": "449"})

    monkeypatch.setattr(auth, "read_sms_code", lambda: "910676")
    fc = WrongCodeClient()
    with pytest.raises(SJAuthError, match="sms code rejected 3 times"):
        auth.perform_full_login(fc, "e@x.se", "pw")
    assert fc.calls.count(("verify", "910676")) == 3
