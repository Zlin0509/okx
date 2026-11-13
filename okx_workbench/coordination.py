"""Single-host demo-account binding, NOT a distributed lock. Settings rejects live."""

import hashlib
import sqlite3
from pathlib import Path

from .market import TradingError


def account_scope(mode, uid):
    if not isinstance(uid, str) or not uid.isdigit():
        raise TradingError("无法验证账户 UID，拒绝执行")
    return f"{mode}:okx:uid:" + hashlib.sha256(uid.encode()).hexdigest()


def bind_account(directory, scope, database):
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "accounts.sqlite3"
    with sqlite3.connect(path, timeout=30) as db:
        db.execute("CREATE TABLE IF NOT EXISTS bindings(scope TEXT PRIMARY KEY, database TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        canonical = str(Path(database).resolve())
        row = db.execute("SELECT database FROM bindings WHERE scope=?", (scope,)).fetchone()
        if row and row[0] != canonical:
            raise TradingError("同一模拟账户已绑定另一数据库；拒绝第二实例，请使用原数据库")
        other = db.execute(
            "SELECT 1 FROM bindings WHERE database=? AND scope<>?", (canonical, scope)
        ).fetchone()
        if other:
            raise TradingError("账户已改变；该数据库已绑定另一账户，请使用独立数据库")
        db.execute("INSERT OR IGNORE INTO bindings VALUES(?,?)", (scope, canonical))
    path.chmod(0o600)
