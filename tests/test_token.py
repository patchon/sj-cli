import json
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
