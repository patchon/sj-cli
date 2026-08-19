import json

from sj_logger import log_json, redact


def test_redact_masks_secret_keys_recursively():
    data = {
        "auth": {"email": "a@b", "password": "hunter2"},
        "Authorization": "Bearer x",
        "ACCESS_TOKEN": "t",
        "items": [{"refresh_token": "r", "ok": 1}],
        "id_token": "i",
        "code_verifier": "v",
    }
    out = redact(data)
    assert out["auth"] == {"email": "a@b", "password": "***redacted***"}
    assert out["Authorization"] == out["ACCESS_TOKEN"] == out["id_token"] == out["code_verifier"] == "***redacted***"
    assert out["items"] == [{"refresh_token": "***redacted***", "ok": 1}]
    # original untouched
    assert data["auth"]["password"] == "hunter2"


def test_log_json_is_pretty_and_redacted():
    s = log_json({"password": "x", "n": 1})
    assert "hunter" not in s
    assert json.loads(s) == {"password": "***redacted***", "n": 1}


def test_log_json_survives_unserialisable():
    assert "failed to serialize" not in log_json({"when": object()})  # default=str handles it
