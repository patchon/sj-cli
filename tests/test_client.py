import time

import httpx
import pytest

from sj_cli.client import STATION_MAP, RetryTransport, SJClient, parse_json_response
from sj_cli.errors import SJAPIError


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
        if isinstance(outcome, httpx.Response):
            return outcome
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
    from sj_cli.client import _raise_for_failure

    _raise_for_failure(_resp(204))
    _raise_for_failure(_resp(200, b"<html>ok</html>", "text/html"))
    _raise_for_failure(_resp(200, b'{"status":"200"}'))


def test_raise_for_failure_surfaces_json_errors_even_with_http_200():
    from sj_cli.client import _raise_for_failure

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


# --- transport: timeouts, body reads, connection release -------------------


class _FailingStream(httpx.SyncByteStream):
    """A response body whose read times out (headers arrived, body did not)."""

    def __iter__(self):
        raise httpx.ReadTimeout("body stalled")
        yield b""  # pragma: no cover


class _TrackedStream(httpx.SyncByteStream):
    def __init__(self, body=b""):
        self.body = body
        self.closed = False

    def __iter__(self):
        yield self.body

    def close(self):
        self.closed = True


def test_client_sets_explicit_timeouts_with_room_for_slow_booking_calls():
    c = SJClient()
    t = c.client.timeout
    assert t.connect == 10.0
    assert t.read == 30.0 and t.write == 30.0 and t.pool == 30.0
    c.close()


def test_get_retries_when_the_body_read_fails():
    req = httpx.Request("GET", "https://example.test/x")
    stalled = httpx.Response(200, request=req, stream=_FailingStream())
    inner = ScriptedTransport([stalled, 200])
    client = httpx.Client(transport=RetryTransport(inner))
    assert client.get("https://example.test/x").status_code == 200
    assert inner.attempts == 2


def test_post_body_read_failure_surfaces_as_the_real_error():
    req = httpx.Request("POST", "https://example.test/x")
    stalled = httpx.Response(200, request=req, stream=_FailingStream())
    inner = ScriptedTransport([stalled, 200])
    client = httpx.Client(transport=RetryTransport(inner))
    with pytest.raises(httpx.ReadTimeout):  # not StreamConsumed from a later read
        client.post("https://example.test/x")
    assert inner.attempts == 1


def test_retried_responses_release_their_connection():
    req = httpx.Request("GET", "https://example.test/x")
    streams = [_TrackedStream(), _TrackedStream()]
    outcomes = [httpx.Response(503, request=req, stream=s) for s in streams] + [200]
    inner = ScriptedTransport(outcomes)
    client = httpx.Client(transport=RetryTransport(inner))
    assert client.get("https://example.test/x").status_code == 200
    assert all(s.closed for s in streams)


def test_retry_transport_composes_with_a_real_client_stack():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = httpx.Client(transport=RetryTransport(httpx.MockTransport(handler)))
    assert client.get("https://example.test/j").json() == {"ok": True}
    assert client.post("https://example.test/j", json={}).json() == {"ok": True}
    assert calls == ["/j", "/j"]


def test_retry_warnings_do_not_log_auth_codes(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="sj_cli.client"):
        inner = ScriptedTransport([503, 200])
        client = httpx.Client(transport=RetryTransport(inner))
        client.get("https://example.test/cb?code=SECRET1&state=s")
    assert "SECRET1" not in caplog.text and "code=***" in caplog.text


def test_opted_in_post_is_retried_like_a_get():
    inner = ScriptedTransport([503, httpx.ReadTimeout("slow"), 200])
    client = httpx.Client(transport=RetryTransport(inner))
    resp = client.post("https://example.test/token", extensions={"sj_retry": True})
    assert resp.status_code == 200 and inner.attempts == 3


# --- every JSON endpoint raises on failure, never returns an error body -----


def _api(handler):
    sc = SJClient()
    sc.client = httpx.Client(transport=httpx.MockTransport(handler))
    return sc


def test_json_methods_surface_the_api_error_envelope():
    envelope = {
        "status": 400,
        "code": 106,
        "message": "Validation errors",
        "validationErrors": [{"propertyPath": "outboundOfferId", "message": "MUST_BE_PROVIDED"}],
    }

    def handler(request):
        return httpx.Response(400, request=request, json=envelope)

    sc = _api(handler)
    with pytest.raises(
        SJAPIError, match="106 · Validation errors · outboundOfferId: MUST_BE_PROVIDED"
    ):
        sc.create_provisional_booking("tok", "OFF", "PT")
    with pytest.raises(SJAPIError, match="106"):
        sc.get_bookings("tok", "2026-09-01", "2026-09-02")
    with pytest.raises(SJAPIError, match="106"):
        sc.search_journey("tok", "Linköping Central", "Stockholm Central", "2026-09-01")
    with pytest.raises(SJAPIError, match="106"):
        sc.get_receipt_search("B1")  # no more silent []
    sc.close()


def test_json_methods_reject_error_bodies_and_empty_bodies_behind_http_200():
    bodies = iter(
        [
            httpx.Response(200, json={"errorCode": "E9", "message": "nope"}),
            httpx.Response(200, content=b""),
            httpx.Response(200, json={"bookings": [], "nextPage": None}),
            httpx.Response(200, json=[{"travelPassId": "TP"}]),
        ]
    )

    def handler(request):
        r = next(bodies)
        r.request = request
        return r

    sc = _api(handler)
    with pytest.raises(SJAPIError, match="E9 · nope"):
        sc.checkout_booking("tok", "B1")
    with pytest.raises(SJAPIError, match="empty response"):
        sc.checkout_booking("tok", "B1")
    assert sc.get_bookings("tok", "2026-09-01", "2026-09-02") == {"bookings": [], "nextPage": None}
    assert sc.get_travel_passes("tok") == [{"travelPassId": "TP"}]  # list bodies are fine
    sc.close()


def test_parse_json_response_accepts_a_list_body():
    req = httpx.Request("GET", "https://x")
    parse_json_response(httpx.Response(200, json=[1, 2], request=req), "u")  # no AttributeError


# --- refresh_token: rejected vs transient ----------------------------------


def test_refresh_token_distinguishes_rejection_from_transient_failure():
    from sj_cli.errors import SJAuthError

    outcomes = iter(
        [
            httpx.Response(200, json={"access_token": "new", "refresh_token": "r2"}),
            httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "AADB2C90080: expired"}
            ),
            httpx.Response(503, text="unavailable"),
            httpx.Response(503, text="unavailable"),
            httpx.Response(503, text="unavailable"),
            httpx.Response(503, text="unavailable"),
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json={"access_token": "new2", "refresh_token": "r3"}),
        ]
    )

    def handler(request):
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        o.request = request
        return o

    sc = SJClient()
    sc.client = httpx.Client(transport=RetryTransport(httpx.MockTransport(handler)))
    assert sc.refresh_token("r")["access_token"] == "new"
    assert sc.refresh_token("r") is None  # definitively rejected → full login
    with pytest.raises(SJAuthError, match="token refresh failed"):  # transient → try later
        sc.refresh_token("r")
    assert sc.refresh_token("r")["access_token"] == "new2"  # a timeout is retried
    sc.close()


# --- login page: take the SSO code before judging the callback's status ----


def test_login_page_takes_the_sso_code_even_when_the_callback_answers_403():
    def handler(request):
        if request.url.host == "id.sj.se":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "https://www.sj.se/logga-in/hantera?code=SSO123&state=s"},
            )
        return httpx.Response(403, request=request, text="blocked")

    sc = SJClient()
    sc.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert sc.initiate_login("e@x.se", "pw") is False
    assert sc._sso_auth_code == "SSO123"
    sc.close()


def test_login_page_failure_is_a_typed_error_without_the_url_secrets():
    def handler(request):
        return httpx.Response(503, request=request, text="maintenance")

    sc = SJClient()
    sc.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with pytest.raises(SJAPIError, match="login page request failed: 503"):
        sc.initiate_login("e@x.se", "pw")
    sc.close()


def test_retry_transport_keeps_compressed_bodies_decodable():
    import gzip

    req = httpx.Request("GET", "https://example.test/x")
    raw = gzip.compress(b'{"ok": true}')
    compressed = httpx.Response(
        200,
        request=req,
        headers={"content-encoding": "gzip", "content-type": "application/json"},
        stream=_TrackedStream(raw),
    )
    inner = ScriptedTransport([503, compressed])
    client = httpx.Client(transport=RetryTransport(inner))
    resp = client.get("https://example.test/x")
    assert resp.json() == {"ok": True}
    assert resp.num_bytes_downloaded == len(raw)
    assert resp.elapsed.total_seconds() >= 0  # httpx's own bookkeeping still works
