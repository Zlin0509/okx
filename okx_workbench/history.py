"""Public candle polling and a bounded cache separate from the trading ledger."""

import fcntl
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import BARS, SYMBOLS, Settings
from .market import OKX, Candle, TradingError

CACHE_ID = 1330337875
SEED_BARS = 300
PAGE_LIMIT = 300
POLL_SECONDS = 10
HEALTH_SECONDS = 30
DAY_MS = 86_400_000


def now_ms():
    return int(time.time() * 1000)


class CacheCapacityError(TradingError):
    messages = {
        "budget": "行情缓存达到磁盘预算",
        "main_file_limit": "行情缓存主文件超过安全上限",
        "sqlite_full": "行情缓存写入空间不足",
    }

    def __init__(self, reason, history, main_bytes, disk_bytes):
        super().__init__(self.messages[reason])
        self.diagnostic = {
            "reason": reason,
            "detail": str(self),
            "main_bytes": main_bytes,
            "main_limit_bytes": history.budget // 3,
            "disk_bytes": disk_bytes,
            "budget_bytes": history.budget,
        }


class History:
    def __init__(self, path, max_rows=10000, days=365, budget=64 * 1024 * 1024, max_pairs=4):
        if not 301 <= max_rows <= 100000 or not 1 <= days <= 3650 or budget < 4 * 1024 * 1024:
            raise ValueError("行情保留须为每组301–100000根、1–3650天、磁盘至少4MiB")
        if not 1 <= max_pairs <= 8:
            raise ValueError("行情订阅上限须在1–8之间")
        self.path = Path(path).resolve()
        self.max_rows, self.days, self.budget, self.max_pairs = max_rows, days, budget, max_pairs
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        new_cache = not self.path.exists()
        with self.db(initialize=True) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA auto_vacuum=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT, bar TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, v REAL,
                    confirmed INTEGER, PRIMARY KEY(symbol,bar,ts)) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS subscriptions (
                    symbol TEXT, bar TEXT, PRIMARY KEY(symbol,bar)) WITHOUT ROWID;
            """)
            db.execute(f"PRAGMA application_id={CACHE_ID}")
            if new_cache:
                db.execute("INSERT OR IGNORE INTO subscriptions VALUES('BTC-USDT-SWAP','1H')")
            # Dropping a subscription does not drop its history.
            db.execute(
                """DELETE FROM subscriptions WHERE (symbol,bar) IN
                   (SELECT symbol,bar FROM subscriptions ORDER BY symbol,bar LIMIT -1 OFFSET ?)""",
                (self.max_pairs,),
            )
            self.prune(db, now_ms())
        self.path.chmod(0o600)

    @contextmanager
    def db(self, *, initialize=False):
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # Refuse to open an unrelated ledger, even if the path was aliased to it.
            existed = self.path.exists()
            self.check_capacity()
            db = sqlite3.connect(self.path, timeout=5)
            try:
                if (existed or not initialize) and db.execute("PRAGMA application_id").fetchone()[
                    0
                ] != CACHE_ID:
                    raise TradingError("拒绝将非行情数据库用作缓存")
                db.execute("PRAGMA temp_store=MEMORY")
                # The journal can contain an old copy of modified pages. Leave two thirds for it and headers.
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                db.execute(f"PRAGMA max_page_count={self.budget // (3 * page_size)}")
                yield db
                db.commit()
            except Exception as exc:
                db.rollback()
                if (
                    isinstance(exc, sqlite3.Error)
                    and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
                ):
                    raise self.capacity_error("sqlite_full") from exc
                raise
            finally:
                db.close()

    def subscribe(self, symbol, bar):
        if symbol not in SYMBOLS or bar not in BARS:
            raise TradingError("不支持的行情订阅")
        with self.db() as db:
            if (symbol, bar) in self.pairs(db):
                return
            if db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] >= self.max_pairs:
                raise TradingError("行情订阅已达上限，请先取消不用的订阅")
            db.execute("INSERT INTO subscriptions VALUES(?,?)", (symbol, bar))

    def unsubscribe(self, symbol, bar):
        with self.db() as db:
            db.execute("DELETE FROM subscriptions WHERE symbol=? AND bar=?", (symbol, bar))

    def pairs(self, db=None):
        if db is None:
            with self.db() as db:
                return self.pairs(db)
        return db.execute("SELECT symbol,bar FROM subscriptions ORDER BY symbol,bar").fetchall()

    def disk_usage(self):
        sizes = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                sizes.append(Path(str(self.path) + suffix).stat().st_size)
            except FileNotFoundError:
                sizes.append(0)
        return sizes[0], sum(sizes)

    def disk_bytes(self):
        return self.disk_usage()[1]

    def capacity_error(self, reason):
        return CacheCapacityError(reason, self, *self.disk_usage())

    def check_capacity(self):
        main_bytes, disk_bytes = self.disk_usage()
        if disk_bytes >= self.budget:
            raise CacheCapacityError("budget", self, main_bytes, disk_bytes)
        if main_bytes > self.budget // 3:
            raise CacheCapacityError("main_file_limit", self, main_bytes, disk_bytes)

    def ingest(self, symbol, bar, rows, *, before=None):
        now = now_ms()
        parsed = []
        for row in rows[:PAGE_LIMIT]:
            try:
                ts, o, high, low, c, volume = int(row[0]), *map(float, row[1:6])
                confirm = str(row[8])
            except (IndexError, TypeError, ValueError) as exc:
                raise TradingError("公共行情格式无效") from exc
            if (
                ts < 0
                or ts > now
                or confirm not in {"0", "1"}
                or not all(math.isfinite(x) for x in (o, high, low, c, volume))
                or min(o, high, low, c) <= 0
                or low > min(o, c)
                or high < max(o, c)
                or volume < 0
                or confirm == "1"
                and ts + BARS[bar] > now
            ):
                raise TradingError("公共行情数值或收盘时间无效")
            if before is not None and (ts >= before or confirm != "1"):
                continue
            if confirm == "0" and ts + BARS[bar] <= now:
                continue
            if ts >= now - self.days * DAY_MS:
                parsed.append((symbol, bar, ts, o, high, low, c, volume, int(confirm)))
        with self.db() as db:
            if (symbol, bar) not in self.pairs(db):
                return None  # A cancellation during the network request wins.
            db.executemany(
                """INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,bar,ts) DO UPDATE SET o=excluded.o,h=excluded.h,l=excluded.l,
                     c=excluded.c,v=excluded.v,confirmed=excluded.confirmed
                   WHERE candles.confirmed=0 AND (excluded.confirmed=1 OR excluded.v>=candles.v)""",
                parsed,
            )
            self.prune(db, now)
        return max((row[2] for row in parsed if row[-1]), default=None)

    def prune(self, db, now):
        db.execute(
            "DELETE FROM candles WHERE ts<? OR (confirmed=0 AND ts<?)",
            (now - self.days * DAY_MS, now - DAY_MS),
        )
        for symbol in SYMBOLS:
            for bar, step in BARS.items():
                db.execute(
                    "DELETE FROM candles WHERE symbol=? AND bar=? AND confirmed=0 AND ts<=?",
                    (symbol, bar, now - step),
                )
                db.execute(
                    """DELETE FROM candles WHERE symbol=? AND bar=? AND ts <
                       (SELECT ts FROM candles WHERE symbol=? AND bar=? ORDER BY ts DESC LIMIT 1 OFFSET ?)""",
                    (symbol, bar, symbol, bar, self.max_rows - 1),
                )
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        while True:
            used = (
                db.execute("PRAGMA page_count").fetchone()[0]
                - db.execute("PRAGMA freelist_count").fetchone()[0]
            )
            if used * page_size <= self.budget // 4:
                break
            # Delete surplus history only. If there is nothing left to reclaim, the hard page cap stops growth.
            deleted = db.execute(
                """DELETE FROM candles WHERE (symbol,bar,ts) IN
                   (SELECT symbol,bar,ts FROM (SELECT symbol,bar,ts,
                    ROW_NUMBER() OVER(PARTITION BY symbol,bar ORDER BY ts DESC) AS rank FROM candles)
                    WHERE rank>? ORDER BY ts,symbol,bar LIMIT ?)""",
                (SEED_BARS + 1, PAGE_LIMIT),
            ).rowcount
            if not deleted:
                break

    def rows(self, symbol, bar, *, confirmed=True, limit=10000):
        where = "AND confirmed=1" if confirmed else ""
        with self.db() as db:
            return db.execute(
                f"""SELECT ts,o,h,l,c,v,confirmed FROM candles
                    WHERE symbol=? AND bar=? AND ts>=? {where} ORDER BY ts DESC LIMIT ?""",
                (symbol, bar, now_ms() - self.days * DAY_MS, limit),
            ).fetchall()[::-1]

    def series(self, symbol, bar, *, limit=10000):
        rows = self.rows(symbol, bar, limit=limit)
        start = len(rows) - 1
        while start > 0 and rows[start][0] - rows[start - 1][0] == BARS[bar]:
            start -= 1
        return [Candle(*row[:-1]) for row in rows[max(0, start) :]]


class Collector:
    def __init__(self, history, public=None, *, base_url="https://www.okx.com"):
        self.history = history
        self.public = public if public is not None else OKX(Settings(base_url=base_url))
        self.state = {}
        self.retry = {}

    def sync(self, symbol, bar):
        pair = (symbol, bar)
        retry_at, failures = self.retry.get(pair, (0, 0))
        if time.monotonic() < retry_at:
            return
        params = {"instId": symbol, "bar": bar}
        try:
            rows = self.public.request("GET", "/api/v5/market/candles", {**params, "limit": 3})
            latest = self.history.ingest(symbol, bar, rows)
            # Target the newest retained gap first; a large gap progresses across bounded sweeps.
            for _ in range(4):
                timestamps = [row[0] for row in self.history.rows(symbol, bar, limit=self.history.max_rows)]
                gaps = [b for a, b in zip(timestamps, timestamps[1:], strict=False) if b - a != BARS[bar]]
                if not timestamps or not gaps and len(timestamps) >= SEED_BARS:
                    break
                cursor = gaps[-1] if gaps else timestamps[0]
                if cursor - BARS[bar] < now_ms() - self.history.days * DAY_MS:
                    break
                page = self.public.request(
                    "GET", "/api/v5/market/history-candles", {**params, "after": cursor, "limit": PAGE_LIMIT}
                )
                self.history.ingest(symbol, bar, page, before=cursor)
                if not page or timestamps == [
                    row[0] for row in self.history.rows(symbol, bar, limit=self.history.max_rows)
                ]:
                    break
            synced = now_ms()
            if latest is None or synced - (latest + BARS[bar]) >= BARS[bar]:
                raise TradingError("公共接口未返回最新已确认K线")
        except (TradingError, sqlite3.Error, OSError) as exc:
            self.retry[pair] = (time.monotonic() + min(120, 5 * 2 ** min(failures, 5)), failures + 1)
            if isinstance(exc, TradingError):
                detail = str(exc) if "查询订单" not in str(exc) else "公共行情请求失败"
            else:
                detail = "行情缓存无法写入，请检查容量和权限"
            previous = self.state.get(pair)
            page_full = isinstance(exc, CacheCapacityError) and exc.diagnostic["reason"] == "sqlite_full"
            phase = "capacity_blocked" if page_full else "disconnected"
            self.state[pair] = (phase, previous[1] if previous else None, None, detail)
            return
        self.retry.pop(pair, None)
        self.state[pair] = ("collecting", synced, latest, None)

    def poll(self):
        with self.history.db() as db:
            self.history.prune(db, now_ms())
            pairs = self.history.pairs(db)
        for pair in self.state.keys() - set(pairs):
            self.state.pop(pair)
            self.retry.pop(pair, None)
        for symbol, bar in pairs:
            self.sync(symbol, bar)

    def fresh(self, symbol, bar, ts):
        status, synced, latest, _ = self.state.get((symbol, bar), ("waiting", None, None, None))
        now = now_ms()
        return (
            (symbol, bar) in self.history.pairs()
            and status == "collecting"
            and synced is not None
            and 0 <= now - synced <= HEALTH_SECONDS * 1000
            and latest == ts
            and 0 <= now - ts - BARS[bar] < BARS[bar]
        )

    def candles(self, symbol, bar, *, minimum, signal=False):
        candles = self.history.series(symbol, bar, limit=SEED_BARS if signal else 10000)
        if len(candles) < minimum:
            raise TradingError(
                f"本地连续已确认K线不足（{len(candles)}/{minimum}根），请订阅并等待回补，或检查保留限制"
            )
        if signal and not self.fresh(symbol, bar, candles[-1].ts):
            raise TradingError("公共行情断连或已落后，等待新鲜已确认K线")
        return candles

    def check_capacity(self):
        self.history.check_capacity()
        if any(phase == "capacity_blocked" for phase, *_ in self.state.copy().values()):
            raise self.history.capacity_error("sqlite_full")

    def status(self):
        try:
            self.check_capacity()
            items = self.subscription_status()
        except CacheCapacityError as exc:
            items = None
            issue = exc.diagnostic
        else:
            issue = None
        return dict(
            subscriptions=items,
            capacity_issue=issue,
            disk_bytes=issue["disk_bytes"] if issue else self.history.disk_bytes(),
            budget_bytes=self.history.budget,
            max_rows=self.history.max_rows,
            retention_days=self.history.days,
            max_pairs=self.history.max_pairs,
        )

    def subscription_status(self):
        now = now_ms()
        items = []
        for symbol, bar in self.history.pairs():
            phase, synced, _, detail = self.state.get((symbol, bar), ("waiting", None, None, None))
            candles = self.history.series(symbol, bar, limit=SEED_BARS)
            latest = candles[-1].ts if candles else None
            close = latest + BARS[bar] if latest is not None else None
            connected = latest is not None and self.fresh(symbol, bar, latest)
            items.append(
                dict(
                    symbol=symbol,
                    bar=bar,
                    connected=connected,
                    state=("collecting" if len(candles) >= SEED_BARS else "backfilling")
                    if connected
                    else phase
                    if phase != "collecting"
                    else "stale",
                    updated_ms=synced,
                    latest_close_ms=close,
                    delay_seconds=(now - close) / 1000 if close is not None else None,
                    confirmed_count=len(candles),
                    detail=detail,
                )
            )
        return items
