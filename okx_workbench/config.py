import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

LIVE_ACK = "I_UNDERSTAND_LIVE_TRADING_USES_REAL_FUNDS"
SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
BARS = {"1H": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}


@dataclass(frozen=True)
class Settings:
    mode: str = "demo"
    source: str = "offline"
    api_key: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)
    passphrase: str = field(default="", repr=False)
    live_ack: str = field(default="", repr=False)
    base_url: str = "https://www.okx.com"
    max_order: Decimal = Decimal("100")
    max_position: Decimal = Decimal("500")
    equity_fraction: Decimal = Decimal("0.10")
    slippage_bps: Decimal = Decimal("20")
    database: str = "data/workbench.sqlite3"
    account_state_dir: str = field(default_factory=lambda: str(Path.home() / ".local/share/okx-workbench"))
    max_signal_age_seconds: int = 300

    def __post_init__(self):
        if self.mode not in {"demo", "live"} or self.source not in {"offline", "okx"}:
            raise ValueError("OKX_MODE 或 OKX_DATA_SOURCE 无效")
        if self.base_url not in {"https://www.okx.com", "https://us.okx.com", "https://eea.okx.com"}:
            raise ValueError("仅允许官方 OKX API 域名")
        if self.mode == "live" and (self.live_ack != LIVE_ACK or self.source != "okx"):
            raise ValueError("实盘必须显式设置 OKX_LIVE_ACK 并选择 okx 数据源")
        if self.mode == "live":
            raise ValueError("本版无法保证跨主机账户互斥，禁止实盘启动；仅支持模拟盘或离线演练")
        if not 30 <= self.max_signal_age_seconds <= 300:
            raise ValueError("信号有效期必须在 30 至 300 秒之间")
        for value in (self.max_order, self.max_position, self.equity_fraction, self.slippage_bps):
            if not value.is_finite() or value <= 0:
                raise ValueError("风控参数必须为有限正数")
        if self.equity_fraction > 1 or self.slippage_bps > 100:
            raise ValueError("仓位比例不能大于 1；滑点上限不能大于 100 bps")

    @property
    def credentials_ready(self):
        return bool(self.api_key and self.secret and self.passphrase)

    @classmethod
    def from_env(cls):
        load_dotenv(override=False)
        return cls(
            mode=os.getenv("OKX_MODE", "demo"),
            source=os.getenv("OKX_DATA_SOURCE", "offline"),
            api_key=os.getenv("OKX_API_KEY", ""),
            secret=os.getenv("OKX_SECRET_KEY", ""),
            passphrase=os.getenv("OKX_PASSPHRASE", ""),
            live_ack=os.getenv("OKX_LIVE_ACK", ""),
            base_url=os.getenv("OKX_BASE_URL", "https://www.okx.com"),
            max_order=Decimal(os.getenv("MAX_ORDER_USDT", "100")),
            max_position=Decimal(os.getenv("MAX_POSITION_USDT", "500")),
            equity_fraction=Decimal(os.getenv("MAX_EQUITY_FRACTION", "0.10")),
            slippage_bps=Decimal(os.getenv("MAX_SLIPPAGE_BPS", "20")),
            database=os.getenv("DATABASE_PATH", "data/workbench.sqlite3"),
            max_signal_age_seconds=int(os.getenv("MAX_SIGNAL_AGE_SECONDS", "300")),
        )
