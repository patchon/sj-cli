import httpx
import pytest

import sj_client
from sj_client import STATION_MAP, RetryTransport, SJClient, parse_json_response
from sj_errors import SJAPIError


class ScriptedTransport(httpx.BaseTransport):
    """Returns/raises the scripted outcomes in order; records attempts."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.attempts = 0

    def handle_request(self, request):
        self.attempts += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, request=request)

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sj_client.time, "sleep", lambda *_: None)


def send(method, outcomes):
    inner = ScriptedTransport(outcomes)
    client = httpx.Client(transport=RetryTransport(inner))
    resp = client.request(method, "https://example.test/x")
    return resp, inner.attempts


def test_get_retries_on_503_then_succeeds():
    resp, attempts = send("GET", [503, 503, 200])
    assert resp.status_code == 200
    assert attempts == 3


def test_get_gives_up_after_three_retries():
    resp, attempts = send("GET", [502, 502, 502, 502])
    assert resp.status_code == 502
    assert attempts == 4


def test_get_does_not_retry_4xx():
    resp, attempts = send("GET", [404, 200])
    assert resp.status_code == 404
    assert attempts == 1


def test_get_retries_connect_error():
    resp, attempts = send("GET", [httpx.ConnectError("boom"), 200])
    assert resp.status_code == 200
    assert attempts == 2


def test_connect_error_exhausted_reraises():
    with pytest.raises(httpx.ConnectError):
        send("GET", [httpx.ConnectError("boom")] * 4)


def test_get_retries_read_timeout():
    resp, attempts = send("GET", [httpx.ReadTimeout("slow"), 200])
    assert resp.status_code == 200
    assert attempts == 2


def test_post_is_not_retried_on_5xx():
    resp, attempts = send("POST", [503, 200])
    assert resp.status_code == 503
    assert attempts == 1


def test_post_is_not_retried_on_read_timeout():
    with pytest.raises(httpx.ReadTimeout):
        send("POST", [httpx.ReadTimeout("slow"), 200])


def test_post_is_retried_when_never_sent():
    resp, attempts = send("POST", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), 201])
    assert resp.status_code == 201
    assert attempts == 3


def test_patch_is_not_retried_on_502():
    resp, attempts = send("PATCH", [502, 200])
    assert resp.status_code == 502
    assert attempts == 1


def test_parse_json_response_paths():
    req = httpx.Request("GET", "https://x")
    parse_json_response(httpx.Response(200, json={"ok": 1}, request=req), "u")
    with pytest.raises(SJAPIError):
        parse_json_response(httpx.Response(200, json={"status": "400", "message": "bad"}, request=req), "u")
    with pytest.raises(SJAPIError):
        parse_json_response(httpx.Response(200, json={"error": "invalid_grant"}, request=req), "u")
    with pytest.raises(httpx.HTTPStatusError):
        parse_json_response(httpx.Response(500, text="<html>", request=req), "u")
    with pytest.raises(Exception, match="failed to decode"):
        parse_json_response(httpx.Response(200, text="<html>", request=req), "u")


def test_resolve_station_case_insensitive_and_passthrough():
    c = SJClient()
    assert c.resolve_station("stockholm c") == STATION_MAP["Stockholm Central"]
    assert c.resolve_station("  Linköping Central ") == "740000009"
    assert c.resolve_station("Nowhere") == "Nowhere"
    c.close()


def test_extract_auth_code_from_302_and_history():
    c = SJClient()
    req = httpx.Request("GET", "https://id.sj.se/auth")
    r302 = httpx.Response(302, headers={"location": "https://www.sj.se/cb?code=ABC&state=s"}, request=req)
    assert c._extract_auth_code(r302) == "ABC"
    followed = httpx.Response(200, request=httpx.Request("GET", "https://www.sj.se/cb"))
    followed.history = [r302]
    assert c._extract_auth_code(followed) == "ABC"
    assert c._extract_auth_code(httpx.Response(200, request=req)) is None
    c.close()


def test_reset_clears_flow_state():
    c = SJClient()
    c.trans_id, c.page_view_id, c.last_response = "t", "p", "r"
    old_verifier = c.code_verifier
    c.reset()
    assert (c.trans_id, c.page_view_id, c.last_response) == ("", "", None)
    assert c.code_verifier != old_verifier  # fresh PKCE pair
    c.close()
