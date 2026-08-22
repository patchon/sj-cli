import time

import httpx
import pytest

from sj_api_client.client import STATION_MAP, RetryTransport, SJClient, parse_json_response
from sj_api_client.errors import SJAPIError


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
    monkeypatch.setattr(time, "sleep", lambda *_: None)


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
    resp, attempts = send(
        "POST", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), 201]
    )
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
        parse_json_response(
            httpx.Response(200, json={"status": "400", "message": "bad"}, request=req), "u"
        )
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
    r302 = httpx.Response(
        302, headers={"location": "https://www.sj.se/cb?code=ABC&state=s"}, request=req
    )
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


def test_get_retries_connection_reset_and_remote_protocol_error():
    resp, attempts = send(
        "GET", [httpx.ReadError("reset"), httpx.RemoteProtocolError("closed"), 200]
    )
    assert resp.status_code == 200
    assert attempts == 3


def test_post_is_not_retried_on_connection_reset():
    # the request may have reached the server: a retry could double-book
    with pytest.raises(httpx.ReadError):
        send("POST", [httpx.ReadError("reset"), 200])


# --- _raise_for_failure: the API's own error beats the status code ---------


def _resp(status, body=b"", content_type="application/json"):
    req = httpx.Request("POST", "https://example.test/x")
    return httpx.Response(status, request=req, content=body, headers={"content-type": content_type})


def test_raise_for_failure_accepts_empty_and_html_success_bodies():
    from sj_api_client.client import _raise_for_failure

    _raise_for_failure(_resp(204))
    _raise_for_failure(_resp(200, b"<html>ok</html>", "text/html"))
    _raise_for_failure(_resp(200, b'{"status":"200"}'))


def test_raise_for_failure_surfaces_json_errors_even_with_http_200():
    from sj_api_client.client import _raise_for_failure

    with pytest.raises(SJAPIError, match="AADB2C90077 · stale"):
        _raise_for_failure(
            _resp(200, b'{"status":"400","errorCode":"AADB2C90077","message":"stale"}')
        )
    with pytest.raises(SJAPIError, match="31100"):
        _raise_for_failure(_resp(400, b'{"errorCode":"31100","message":"trace disabled"}'))
    with pytest.raises(httpx.HTTPStatusError):
        _raise_for_failure(_resp(502, b"bad gateway", "text/plain"))


def test_cancel_methods_raise_instead_of_returning_false():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/revert"):
            return httpx.Response(204, request=request)
        return httpx.Response(
            400, request=request, json={"errorCode": "E1", "message": "cannot cancel"}
        )

    sc = SJClient()
    sc.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SJAPIError, match="E1 · cannot cancel"):
        sc.cancel_booking_with_patch("tok", "B1", [])
    with pytest.raises(SJAPIError, match="E1 · cannot cancel"):
        sc.cancel_provisional_booking("tok", "B1")
    assert sc.revert_booking("tok", "B1") is None  # 204, no body: fine
    sc.close()


# --- live SSO session answers the login page request with a code -----------


def test_login_sequence_short_circuits_when_authorize_redirects_with_a_code():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.host, request.url.path))
        if request.url.host == "id.sj.se" and request.url.path.endswith("/authorize"):
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://www.sj.se/logga-in/hantera?code=SSO123&state=s"},
            )
        if request.url.host == "www.sj.se":
            return httpx.Response(200, request=request, text="<html>callback, no SETTINGS</html>")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    sc = SJClient()
    sc.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert sc.initiate_login("e@x.se", "pw") is False  # no SMS step
    assert sc._sso_auth_code == "SSO123"
    assert sc.finalize_login() == "SSO123"
    assert [m for m, *_ in seen] == ["GET", "GET"]  # no credential/fingerprint POSTs
    sc.reset()
    assert sc._sso_auth_code == ""
    sc.close()
