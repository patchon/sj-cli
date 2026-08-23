import json
import logging

from sj_cli.logger import HttpxTraceLevelFilter, log_json, redact, redact_http_trace


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
    assert (
        out["Authorization"]
        == out["ACCESS_TOKEN"]
        == out["id_token"]
        == out["code_verifier"]
        == "***redacted***"
    )
    assert out["items"] == [{"refresh_token": "***redacted***", "ok": 1}]
    # original untouched
    assert data["auth"]["password"] == "hunter2"


def test_log_json_is_pretty_and_redacted():
    s = log_json({"password": "x", "n": 1})
    assert "hunter" not in s
    assert json.loads(s) == {"password": "***redacted***", "n": 1}


def test_log_json_survives_unserialisable():
    assert "failed to serialize" not in log_json({"when": object()})  # default=str handles it


def test_http_trace_redaction_scrubs_cookies_auth_and_codes():
    line = (
        "receive_response_headers.complete return_value=(b'HTTP/1.1', 302, b'Found', "
        "[(b'Set-Cookie', b'x-ms-cpim-sso:abc=SECRET; path=/'), (b'Location', "
        "b'https://www.sj.se/logga-in/hantera?code=AUTHCODE&state=s'), (b'Authorization', b'Bearer T')])"
    )
    out = redact_http_trace(line)
    assert "SECRET" not in out and "AUTHCODE" not in out and "Bearer T" not in out
    assert "b'Set-Cookie', b'***redacted***'" in out and "code=***&state=s" in out
    assert "Location" in out and "302" in out  # everything else survives


def test_httpcore_records_are_scrubbed_by_the_filter():
    rec = logging.LogRecord(
        "httpcore.http11",
        logging.DEBUG,
        __file__,
        1,
        "send_request_headers.started request=[(b'Cookie', b'x-ms-cpim-sso:abc=SECRET')]",
        (),
        None,
    )
    assert HttpxTraceLevelFilter(True).filter(rec)
    assert "SECRET" not in rec.getMessage() and rec.levelname == "TRACE"


def test_redaction_covers_sms_codes_csrf_tokens_and_auth_codes():
    import json

    from sj_cli.logger import log_json

    data = {
        "verification_code": "123456",
        "X-CSRF-TOKEN": "csrf-abc",
        "csrf_token": "csrf-def",
        "code": "A" * 40,  # an OAuth authorization code
        "status": {"code": 106},  # the API's numeric error code stays readable
    }
    out = json.loads(log_json(data))
    assert out["verification_code"] == out["X-CSRF-TOKEN"] == out["csrf_token"] == out["code"]
    assert out["code"] == "***redacted***"
    assert out["status"]["code"] == 106


def test_invalid_log_level_is_reported_on_stderr(capsys):
    from sj_cli.logger import setup_logging

    setup_logging("bogus")
    assert "invalid log level 'bogus' specified, defaulting to CRITICAL" in capsys.readouterr().err
