"""SJAPIError turns API error bodies into readable messages (code · message),
keeping the raw payload and parsed fields as attributes."""

from sj_api_client.errors import SJAPIError, SJAuthError, SJConfigError, SJError


def test_b2c_error_dict_is_humanized():
    e = SJAPIError(
        {
            "status": "400",
            "errorCode": "AADB2C90054",
            "message": "Inloggningsuppgifterna matchar inte.",
        }
    )
    assert str(e) == "AADB2C90054 · Inloggningsuppgifterna matchar inte."
    assert e.code == "AADB2C90054"
    assert e.message == "Inloggningsuppgifterna matchar inte."
    assert e.payload["status"] == "400"


def test_oauth_style_error_keys():
    e = SJAPIError({"error": "invalid_grant", "error_description": "bad refresh token"})
    assert str(e) == "invalid_grant · bad refresh token"
    assert e.code == "invalid_grant"
    assert e.message == "bad refresh token"


def test_dict_without_known_keys_falls_back_to_repr():
    e = SJAPIError({"status": "500"})
    assert str(e) == "{'status': '500'}"
    assert e.code is None
    assert e.message is None


def test_string_payload_unchanged():
    e = SJAPIError("cancel request for X was not accepted")
    assert str(e) == "cancel request for X was not accepted"
    assert e.payload is None
    assert e.code is None
    assert e.message is None


def test_every_error_shares_the_sjerror_base():
    for exc in (SJAPIError("x"), SJAuthError("x"), SJConfigError("x", ["a"])):
        assert isinstance(exc, SJError)
    assert SJConfigError("x", ["a"]).errors == ["a"]


def test_api_error_reads_the_sj_envelope_code_and_validation_details():
    from sj_api_client.errors import SJAPIError

    e = SJAPIError(
        {
            "status": 400,
            "code": 106,
            "message": "Validation errors",
            "validationErrors": [
                {"propertyPath": "outboundOfferId", "message": "OUTBOUND_OFFER_ID_MUST_BE_PROVIDED"}
            ],
        }
    )
    assert e.code == "106"
    assert str(e) == "106 · Validation errors · outboundOfferId: OUTBOUND_OFFER_ID_MUST_BE_PROVIDED"


def test_error_text_is_one_line_redacted_and_never_empty():
    import httpx

    from sj_api_client.errors import SJAPIError, error_text

    req = httpx.Request("GET", "https://www.sj.se/cb?code=SECRET1&state=s")
    resp = httpx.Response(403, request=req)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        text = error_text(e)
    assert "\n" not in text  # httpx appends a "For more information" line
    assert "SECRET1" not in text and "code=***" in text
    assert text.startswith("Client error '403 Forbidden'")
    assert error_text(httpx.PoolTimeout("")) == "PoolTimeout"  # str() is empty
    assert error_text(SJAPIError({"errorCode": "E1", "message": "m"})) == "E1 · m"
    assert error_text(RuntimeError("plain")) == "plain"
