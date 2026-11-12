import base64
import hashlib
import hmac
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from .config import BARS, Settings


class TradingError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = str(code) if code is not None and re.fullmatch(r"[0-9]{1,10}", str(code)) else None


class ExchangeRejected(TradingError):
    """Explicit rejection; unlike transport failures, submission outcome is known."""


def number(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError
        return result
    except Exception as exc:
        raise TradingError("交易所返回了无效数值") from exc


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0


def fixture(symbol="BTC-USDT-SWAP", bar="1H", limit=300, end=None):
    """Deterministic synthetic OHLC; never presented as exchange data."""
    step = BARS[bar]
    end = (int(time.time() * 1000) if end is None else end) // step * step
    scale = 600 if symbol.startswith("BTC") else 30

    def price(ts):
        # Every timeframe samples the same underlying synthetic market.
        x = ts / BARS["1H"]
        return scale * (100 + 6 * math.sin(x / 18) + 2 * math.sin(x / 4) + math.sin(x / 71))

    candles = []
    for ts in range(end - limit * step, end, step):
        o, c = price(ts), price(ts + step)
        prices = [price(t) for t in range(ts, ts + step + 1, BARS["1H"])]
        candles.append(Candle(ts, o, max(prices) * 1.002, min(prices) * 0.998, c, 100 * step / BARS["1H"]))
    return candles


class OKX:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.base_url, timeout=8, transport=transport, follow_redirects=False
        )

    def close(self):
        self.http.close()

    def request(self, method, path, params=None, body=None, private=False):
        query = "?" + urlencode(params) if params else ""
        request_path = path + query
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json"}
        if self.settings.mode == "demo":
            headers["x-simulated-trading"] = "1"
        if private:
            if not self.settings.credentials_ready:
                raise TradingError("请在服务端 .env 配置 OKX API Key、Secret 和 Passphrase")
            stamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            signature = base64.b64encode(
                hmac.new(
                    self.settings.secret.encode(),
                    (stamp + method + request_path + payload).encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.settings.api_key,
                    "OK-ACCESS-SIGN": signature,
                    "OK-ACCESS-TIMESTAMP": stamp,
                    "OK-ACCESS-PASSPHRASE": self.settings.passphrase,
                }
            )
        try:
            response = self.http.request(method, request_path, content=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never expose headers, request bodies or raw upstream errors.
            raise TradingError("OKX 请求失败或结果不确定，请检查网络并查询订单状态") from exc
        if str(data.get("code")) != "0":
            code = str(data.get("code", "unknown"))
            if method == "POST" and code == "1":
                detail = data.get("data")
                subcode = detail[0].get("sCode") if isinstance(detail, list) and detail else code
                raise ExchangeRejected("OKX 明确拒绝订单，请检查账户和交易参数", subcode)
            raise TradingError("OKX API 返回错误，请在 OKX 查看账户状态", code)
        rows = data.get("data")
        if not isinstance(rows, list):
            raise TradingError("OKX 响应格式无效")
        return rows

    def candles(self, symbol, bar="1H", limit=300):
        rows = self.request("GET", "/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": limit})
        candles = []
        for row in rows:
            if len(row) >= 9 and row[8] == "1":
                values = [float(number(v)) for v in row[1:6]]
                candles.append(Candle(int(row[0]), *values))
        candles = sorted({c.ts: c for c in candles}.values(), key=lambda c: c.ts)
        if not candles:
            raise TradingError("OKX 未返回已完成的 K 线")
        return candles

    def instrument(self, symbol):
        rows = self.request("GET", "/api/v5/public/instruments", {"instType": "SWAP", "instId": symbol})
        if not rows:
            raise TradingError("合约不存在")
        info = rows[0]
        if (
            info.get("ctType") != "linear"
            or info.get("settleCcy") != "USDT"
            or info.get("ctValCcy") != symbol.split("-")[0]
            or info.get("state") != "live"
        ):
            raise TradingError("仅支持交易中的 USDT 本位线性永续合约")
        return info

    def ticker(self, symbol):
        rows = self.request("GET", "/api/v5/market/ticker", {"instId": symbol})
        if not rows or abs(int(time.time() * 1000) - int(rows[0].get("ts", 0))) > 60_000:
            raise TradingError("行情已过期")
        return rows[0]

    def account(self):
        configs = self.request("GET", "/api/v5/account/config", private=True)
        if not configs or configs[0].get("posMode") != "net_mode":
            raise TradingError("请先在 OKX 将账户设为单向持仓（net_mode）")
        balances = self.request("GET", "/api/v5/account/balance", {"ccy": "USDT"}, private=True)
        positions = self.request("GET", "/api/v5/account/positions", {"instType": "SWAP"}, private=True)
        pending = self.request("GET", "/api/v5/trade/orders-pending", {"instType": "SWAP"}, private=True)
        if not balances:
            raise TradingError("账户余额不可用")
        detail = next((d for d in balances[0].get("details", []) if d.get("ccy") == "USDT"), {})
        return {
            "equity": str(number(detail.get("eq", "0"))),
            "available": str(number(detail.get("availBal") or "0")),
            "unrealized_pnl": str(number(detail.get("upl") or "0")),
            "positions": positions,
            "pending": pending,
        }

    def place(self, body):
        rows = self.request("POST", "/api/v5/trade/order", body=body, private=True)
        if not rows:
            raise TradingError("订单响应为空，成交状态不确定")
        if "sCode" not in rows[0]:
            raise TradingError("订单响应缺少状态，成交状态不确定")
        if str(rows[0]["sCode"]) != "0":
            raise ExchangeRejected("OKX 明确拒绝订单，请检查账户余额、杠杆和最小张数", rows[0]["sCode"])
        if not rows[0].get("ordId"):
            raise TradingError("订单响应缺少订单号，成交状态不确定")
        return rows[0]

    def order(self, symbol, client_id):
        rows = self.request(
            "GET", "/api/v5/trade/order", {"instId": symbol, "clOrdId": client_id}, private=True
        )
        if not rows:
            raise TradingError("无法确认订单，保持停单")
        return rows[0]

    def cancel(self, symbol, client_id):
        return self.request(
            "POST", "/api/v5/trade/cancel-order", body={"instId": symbol, "clOrdId": client_id}, private=True
        )
