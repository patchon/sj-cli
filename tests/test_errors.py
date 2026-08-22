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
