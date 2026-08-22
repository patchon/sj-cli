"""--logout orchestration (SPEC §5.6): best-effort server-side end-session,
local caches always cleared, server failure raises SJAuthError (exit 1).
Output: trail steps (ending session, removing caches), then a bare verdict
card — the header box is printed by the cli before handle_logout runs."""

import pytest

from sj_api_client.auth import handle_logout
from sj_api_client.errors import SJAPIError, SJAuthError
from sj_api_client.tokens import TokenManager


class FakeAuthClient:
    """Records import_cookies/b2c_logout calls; optionally fails the logout."""

    def __init__(self, *, logout_fails=False):
        self.calls = []
        self._logout_fails = logout_fails

    def import_cookies(self, cookies):
        self.calls.append(("import_cookies", len(cookies)))

    def b2c_logout(self):
        self.calls.append(("b2c_logout",))
        if self._logout_fails:
            raise SJAPIError("boom")


def _tm(tmp_path, *, token=False, cookies=False):
    tm = TokenManager(tmp_path / "token.json")
    if token:
        tm.path.write_text('{"access_token": "a"}')
    if cookies:
        tm.cookie_path.write_text(
            '[{"name": "x-ms-cpim-sso", "value": "v", "domain": "id.sj.se", "path": "/"}]'
        )
    return tm


def test_logout_with_cookies_ends_session_and_clears(tmp_path, capsys):
    tm = _tm(tmp_path, token=True, cookies=True)
    fc = FakeAuthClient()
    handle_logout(fc, tm)
    assert fc.calls[0] == ("import_cookies", 1)
    assert ("b2c_logout",) in fc.calls
    assert not tm.path.exists()
    assert not tm.cookie_path.exists()
    out = capsys.readouterr().out
    assert "✓ ending sj.se session" in out
    assert "✓ removing cached token and cookies" in out
    assert "● logged out" in out
    assert "removed   " not in out  # the fact moved into the trail


def test_logout_without_cookies_skips_server_call(tmp_path, capsys):
    tm = _tm(tmp_path, token=True)
    fc = FakeAuthClient()
    handle_logout(fc, tm)
    assert fc.calls == []
    assert not tm.path.exists()
    out = capsys.readouterr().out
    assert "ending sj.se session" not in out
    assert "✓ removing cached token and cookies" in out
    assert "● logged out" in out


def test_logout_nothing_cached_is_a_noop(tmp_path, capsys):
    tm = _tm(tmp_path)
    fc = FakeAuthClient()
    handle_logout(fc, tm)
    assert fc.calls == []
    out = capsys.readouterr().out
    assert "removing" not in out
    assert "● already logged out" in out


def test_logout_server_failure_still_clears_and_raises(tmp_path, capsys):
    tm = _tm(tmp_path, token=True, cookies=True)
    fc = FakeAuthClient(logout_fails=True)
    with pytest.raises(SJAuthError, match="server-side logout failed"):
        handle_logout(fc, tm)
    assert not tm.path.exists()
    assert not tm.cookie_path.exists()
    out = capsys.readouterr().out
    assert "✗ ending sj.se session" in out
    assert "✓ removing cached token and cookies" in out
    assert "● logged out" not in out  # the cli renders the failure card instead
