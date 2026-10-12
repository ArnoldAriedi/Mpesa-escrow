"""N6: STK Push Query. A failed or unreadable QUERY must never be read as
'the customer did not pay'."""
import json

import httpx
import pytest

import escrow_core as mpesa

CASES = [
    (200, {"ResponseCode": "0", "ResultCode": "0", "ResultDesc": "ok"}, "PAID"),
    (200, {"ResponseCode": "0", "ResultCode": 0}, "PAID"),
    (200, {"ResponseCode": "0", "ResultCode": "1032", "ResultDesc": "cancelled"}, "FAILED"),
    (200, {"ResponseCode": "0", "ResultCode": "2001"}, "FAILED"),
    (200, {"ResponseCode": "0", "ResultCode": "1037"}, "FAILED"),
    (200, {"ResponseCode": "0"}, "UNKNOWN"),
    (200, {"ResponseCode": "0", "ResultCode": "garbage"}, "UNKNOWN"),
    (200, {"ResponseCode": "1", "ResultCode": "0"}, "UNKNOWN"),
    (200, {}, "UNKNOWN"),
    (500, {"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"}, "UNKNOWN"),
    (401, {"errorMessage": "Invalid Access Token"}, "UNKNOWN"),
    (404, {}, "UNKNOWN"),
    (503, {}, "UNKNOWN"),
]


@pytest.fixture
def stub_auth(monkeypatch):
    async def fake_token(client):
        return "tok"

    monkeypatch.setattr(mpesa, "get_access_token", fake_token)
    monkeypatch.setattr(mpesa, "MPESA_SHORTCODE", "174379")
    monkeypatch.setattr(mpesa, "MPESA_PASSKEY", "test")


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", CASES)
async def test_query_outcomes(stub_auth, status, body, expected):
    async with _client(lambda request: httpx.Response(status, json=body)) as c:
        out = await mpesa.query_stk_status(c, "ws_CO_1")
    assert out["outcome"] == expected
    if expected != "UNKNOWN":
        assert out["result_code"] is not None


@pytest.mark.asyncio
async def test_a_non_json_answer_is_unknown(stub_auth):
    async with _client(lambda request: httpx.Response(200, text="<html>gateway</html>")) as c:
        out = await mpesa.query_stk_status(c, "ws_CO_1")
    assert out["outcome"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_request_shape(stub_auth):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ResponseCode": "0", "ResultCode": "0"})

    async with _client(handler) as c:
        await mpesa.query_stk_status(c, "ws_CO_42")
    assert seen["url"].endswith("/mpesa/stkpushquery/v1/query")
    assert seen["auth"] == "Bearer tok"
    assert set(seen["json"]) == {"BusinessShortCode", "Password", "Timestamp", "CheckoutRequestID"}
    assert seen["json"]["CheckoutRequestID"] == "ws_CO_42"
    assert seen["json"]["BusinessShortCode"] == "174379"


@pytest.mark.asyncio
async def test_missing_configuration_raises(stub_auth, monkeypatch):
    monkeypatch.setattr(mpesa, "MPESA_PASSKEY", None)
    with pytest.raises(RuntimeError):
        await mpesa.query_stk_status(None, "ws_CO_1")


@pytest.mark.asyncio
async def test_missing_checkout_id_raises(stub_auth):
    with pytest.raises(RuntimeError):
        await mpesa.query_stk_status(None, "")