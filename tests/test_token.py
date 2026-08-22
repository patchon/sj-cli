import json
import stat
import time

import pytest

from sj_api_client.errors import SJAuthError
from sj_api_client.tokens import TokenManager


def test_load_missing_returns_none(tmp_path):
    assert TokenManager(tmp_path / "token.json").load() is None


def test_load_corrupt_raises_sjautherror(tmp_path):
    p = tmp_path / "token.json"
    p.write_text("{not json")
    with pytest.raises(SJAuthError, match="failed to parse token cache"):
        TokenManager(p).load()


def test_save_computes_refresh_expiry_and_roundtrips(tmp_path):
    tm = TokenManager(tmp_path / "token.json")
    now = int(time.time())
    tm.save(
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_on": now + 3600,
            "refresh_token_expires_in": 86400,
        }
    )
    data = json.loads((tmp_path / "token.json").read_text())
    assert data["refresh_token_expires_on"] >= now + 86400 - 2
    assert tm.is_valid()
    assert tm.has_refresh_token()
    assert not tm.refresh_token_needs_renewal()


def test_validity_buffer_and_expiry(tmp_path):
    tm = TokenManager(tmp_path / "token.json")
    now = int(time.time())
    tm.token = {"access_token": "a", "expires_on": now + 299}  # inside 5-min buffer
    assert not tm.is_valid()
    tm.token = {"access_token": "a", "expires_on": now + 301}
    assert tm.is_valid()
    tm.token = {"access_token": "a", "expires_on": "soon"}
    assert not tm.is_valid()
    tm.token = {"refresh_token": "r", "refresh_token_expires_on": now - 1}
    assert not tm.has_refresh_token()
    tm.token = {"refresh_token": "r", "refresh_token_expires_on": now + 600}
    assert tm.has_refresh_token()
    assert tm.refresh_token_needs_renewal(threshold_seconds=3600)


def test_clear_removes_caches_and_is_idempotent(tmp_path):
    tm = TokenManager(tmp_path / "token.json")
    tm.save({"access_token": "a", "expires_on": 1})
    tm.save_cookies([{"name": "n", "value": "v", "domain": "", "path": "/"}])
    assert tm.clear() == ["token", "cookies"]
    assert not (tmp_path / "token.json").exists()
    assert not (tmp_path / "cookies.json").exists()
    assert tm.token is None
    assert tm.clear() == []


def test_profile_email_from_profile_info(tmp_path):
    import base64

    tm = TokenManager(tmp_path / "token.json")
    blob = (
        base64.urlsafe_b64encode(
            json.dumps({"preferred_username": "user@example.com", "ver": "1.0"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    tm.token = {"profile_info": blob}
    assert tm.profile_email() == "user@example.com"
    tm.token = {"profile_info": "%%%not-base64%%%"}
    assert tm.profile_email() is None
    tm.token = {}
    assert tm.profile_email() is None
    tm.token = None
    assert tm.profile_email() is None


def test_caches_are_written_owner_readable_only(tmp_path):
    tm = TokenManager(tmp_path / "cache" / "token.json")
    tm.save({"access_token": "a", "expires_on": 1})
    tm.save_cookies([{"name": "n", "value": "v", "domain": "", "path": "/"}])
    assert stat.S_IMODE(tm.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tm.cookie_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tm.path.parent.stat().st_mode) == 0o700


def test_save_tightens_the_mode_of_an_existing_cache(tmp_path):
    p = tmp_path / "token.json"
    p.write_text("{}")
    p.chmod(0o644)
    TokenManager(p).save({"access_token": "a", "expires_on": 1})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text())["access_token"] == "a"


def test_failed_write_keeps_the_previous_cache_and_the_in_memory_token(tmp_path, monkeypatch):
    import json as json_mod

    from sj_api_client import tokens as m

    tm = TokenManager(tmp_path / "token.json")
    tm.save({"access_token": "old", "expires_on": 1})

    def explode(data, f):
        f.write('{"access_token": "new", "expires_on": ')
        raise OSError("disk full")

    monkeypatch.setattr(m.json, "dump", explode)
    tm.save({"access_token": "new", "expires_on": 2})
    # disk: the old, complete file; memory: the new token (a fresh login must
    # not be treated as expired because the cache could not be written)
    assert json_mod.loads(tm.path.read_text()) == {"access_token": "old", "expires_on": 1}
    assert tm.token == {"access_token": "new", "expires_on": 2}
    assert not list(tmp_path.glob(".*.tmp"))


def test_clear_raises_when_a_cache_cannot_be_deleted(tmp_path):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    cache = tmp_path / "cache"
    tm = TokenManager(cache / "token.json")
    tm.save({"access_token": "a", "expires_on": 1})
    cache.chmod(0o500)
    try:
        with pytest.raises(SJAuthError, match="could not remove the token cache"):
            tm.clear()
    finally:
        cache.chmod(0o700)
    assert tm.path.exists()
