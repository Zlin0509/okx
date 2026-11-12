import base64
import hashlib
import hmac
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from okx_workbench.config import LIVE_ACK, Settings
from okx_workbench.market import OKX, ExchangeRejected, TradingError


def test_live_requires_explicit_ack_and_official_domain_only():
    with pytest.raises(ValueError):
        Settings(mode="live", source="okx")
    with pytest.raises(ValueError):
        Settings(mode="live", live_ack=LIVE_ACK)
    assert Settings(mode="live", source="okx", live_ack=LIVE_ACK).mode == "live"
    with pytest.raises(ValueError):
        Settings(base_url="https://attacker.example")
    with pytest.raises(ValueError):
        Settings(max_order=Decimal("NaN"))


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_exact_request_bytes_signed_and_demo_header(method):
    settings = replace(Settings(), source="okx", api_key="key", secret="secret", passphrase="pass")

    def handle(request):
        headers = request.headers
        message = (
            headers["OK-ACCESS-TIMESTAMP"] + method + request.url.raw_path.decode() + request.content.decode()
        )
        expected = base64.b64encode(hmac.new(b"secret", message.encode(), hashlib.sha256).digest()).decode()
        assert headers["OK-ACCESS-SIGN"] == expected
        assert headers["x-simulated-trading"] == "1"
        assert headers["OK-ACCESS-KEY"] == "key"
        return httpx.Response(200, json={"code": "0", "data": []})

    client = OKX(settings, httpx.MockTransport(handle))
    client.request(
        method,
        "/api/v5/trade/order",
        params={"instId": "BTC-USDT-SWAP"} if method == "GET" else None,
        body={"side": "buy", "sz": "1"} if method == "POST" else None,
        private=True,
    )
    client.close()


def test_incomplete_candles_removed_and_chronological_order():
    def handle(request):
        rows = [
            ["7200000", "102", "103", "101", "102", "1", "0", "0", "0"],
            ["3600000", "101", "102", "100", "101", "1", "0", "0", "1"],
            ["0", "100", "101", "99", "100", "1", "0", "0", "1"],
        ]
        return httpx.Response(200, json={"code": "0", "data": rows})

    client = OKX(Settings(), httpx.MockTransport(handle))
    assert [c.ts for c in client.candles("BTC-USDT-SWAP")] == [0, 3600000]
    client.close()


def test_exchange_error_and_credentials_are_sanitized():
    settings = replace(Settings(), api_key="private-key", secret="private-secret", passphrase="private-pass")
    client = OKX(
        settings,
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": "50113", "msg": "private-secret", "data": []})
        ),
    )
    with pytest.raises(TradingError) as error:
        client.account()
    assert "private" not in str(error.value)
    assert error.value.code == "50113"
    client.close()


def test_per_order_rejection_not_treated_as_fill():
    client = OKX(
        Settings(),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "0", "data": [{"sCode": "51008"}]})),
    )
    with pytest.raises(TradingError):  # Missing credentials: no network trade.
        client.place({})
    client.close()
    client = OKX(
        replace(Settings(), api_key="key", secret="secret", passphrase="pass"),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "0", "data": [{"sCode": "51008"}]})),
    )
    with pytest.raises(ExchangeRejected):
        client.place({})
    client.close()


def test_malformed_receipt_is_uncertain_not_rejected():
    client = OKX(
        replace(Settings(), api_key="key", secret="secret", passphrase="pass"),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "0", "data": [{}]})),
    )
    with pytest.raises(TradingError) as error:
        client.place({})
    assert not isinstance(error.value, ExchangeRejected)
    client.close()
