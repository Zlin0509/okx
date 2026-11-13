import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS strategies (
                    id INTEGER PRIMARY KEY, spec TEXT NOT NULL, report TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, strategy_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL,
                    price TEXT NOT NULL, state TEXT NOT NULL, exchange_id TEXT,
                    error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    filled TEXT NOT NULL DEFAULT '0'
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, level TEXT NOT NULL, message TEXT NOT NULL,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper (
                    scope TEXT PRIMARY KEY, cash REAL NOT NULL, quantity REAL NOT NULL DEFAULT 0,
                    entry REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0,
                    realized REAL NOT NULL DEFAULT 0, symbol TEXT
                );
                CREATE TABLE IF NOT EXISTS backtests (
                    id INTEGER PRIMARY KEY, spec TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_audit (
                    id INTEGER PRIMARY KEY, order_id TEXT NOT NULL, from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
                    evidence TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scope_migrations (
                    old_scope TEXT PRIMARY KEY, scope TEXT NOT NULL, cutoff REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL);
                INSERT OR IGNORE INTO control VALUES(1,0);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(orders)")}
            for name, declaration in {
                "intent_version": "INTEGER NOT NULL DEFAULT 0",
                "submit_started": "REAL",
                "not_found_count": "INTEGER NOT NULL DEFAULT 0",
                "last_error_code": "TEXT",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE orders ADD COLUMN {name} {declaration}")
            if "last_signal" not in {r[1] for r in db.execute("PRAGMA table_info(strategies)")}:
                db.execute("ALTER TABLE strategies ADD COLUMN last_signal TEXT")
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def locked(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def event(self, message, level="info"):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(level,message,created) VALUES(?,?,?)", (level, message, time.time())
            )

    def add_strategy(self, report):
        with self.connect() as db:
            result = db.execute(
                "INSERT INTO strategies(spec,report,created) VALUES(?,?,?)",
                (json.dumps(report["spec"]), json.dumps(report), time.time()),
            )
            return result.lastrowid

    def strategies(self):
        with self.connect() as db:
            return [
                {**dict(r), "spec": json.loads(r["spec"]), "report": json.loads(r["report"])}
                for r in db.execute("SELECT * FROM strategies ORDER BY id DESC")
            ]

    def stop(self):
        with self.locked() as db:
            db.execute("UPDATE strategies SET active=0")
            db.execute("UPDATE control SET epoch=epoch+1 WHERE id=1")

    def control_epoch(self):
        with self.connect() as db:
            return db.execute("SELECT epoch FROM control WHERE id=1").fetchone()[0]

    def begin_start(self, expected=None):
        from .market import TradingError

        with self.locked() as db:
            epoch = db.execute("SELECT epoch FROM control WHERE id=1").fetchone()[0]
            if expected is not None and expected != epoch:
                raise TradingError("启动请求已被暂停或另一操作作废，请刷新后重新确认")
            db.execute("UPDATE control SET epoch=epoch+1 WHERE id=1")
            db.execute("UPDATE strategies SET active=0")
            return epoch + 1

    def orders(self, scope):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM orders WHERE scope=? ORDER BY created DESC LIMIT 100", (scope,)
                )
            ]

    def events(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 30")]

    @staticmethod
    def audit(db, order_id, before, after, reason, actor="system", evidence=None):
        db.execute(
            "INSERT INTO order_audit(order_id,from_state,to_state,actor,reason,evidence,created) VALUES(?,?,?,?,?,?,?)",
            (
                order_id,
                before,
                after,
                actor,
                reason,
                json.dumps(evidence or {}, ensure_ascii=False),
                time.time(),
            ),
        )

    def audit_history(self, order_id):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM order_audit WHERE order_id=? ORDER BY id", (order_id,))
            ]

    def unresolved(self, scope):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM orders WHERE scope=? AND state IN ('reserved','submitting','unknown','accepted','live','partially_filled') ORDER BY created",
                    (scope,),
                )
            ]

    def migrate_scope(self, old_scope, scope, verify_order):
        from .market import TradingError

        with self.locked() as db:
            prefix = old_scope.rsplit(":", 1)[0] + ":"
            if (
                db.execute(
                    "SELECT 1 FROM orders WHERE scope LIKE ? AND scope<>? LIMIT 1",
                    (prefix + "uid:%", scope),
                ).fetchone()
                or db.execute("SELECT 1 FROM scope_migrations WHERE scope<>? LIMIT 1", (scope,)).fetchone()
            ):
                raise TradingError("数据库已有另一账户 UID 记录，拒绝迁移")
            legacy = {old_scope}
            legacy.update(
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT scope FROM orders WHERE scope LIKE ? AND scope NOT LIKE ?",
                    (prefix + "%", prefix + "uid:%"),
                )
            )
            # Verification and every update share one transaction. A revoked key
            # needs no secret: look up each old client ID with the new authenticated
            # account and compare immutable order identity. Missing is never proof.
            for source in sorted(legacy):
                rows = db.execute("SELECT * FROM orders WHERE scope=?", (source,)).fetchall()
                evidence = {}
                for r in rows:
                    evidence[r["id"]] = (
                        {"ownership": "authenticated_original_key", "account_scope": scope}
                        if source == old_scope
                        else verify_order(dict(r), scope)
                    )
                if rows:
                    cutoff = max(r["created"] for r in rows)
                    db.execute(
                        "INSERT OR IGNORE INTO scope_migrations VALUES(?,?,?)", (source, scope, cutoff)
                    )
                for r in rows:
                    self.audit(
                        db,
                        r["id"],
                        r["state"],
                        r["state"],
                        "已核实账户归属并迁移旧版意图；终态去重和旧信号截止保留",
                        evidence=evidence[r["id"]],
                    )
                db.execute("UPDATE orders SET scope=? WHERE scope=?", (scope, source))
                for r in db.execute("SELECT id,report FROM strategies").fetchall():
                    report = json.loads(r["report"])
                    if report.get("scope") == source and (rows or source == old_scope):
                        report["scope"] = scope
                        db.execute("UPDATE strategies SET report=? WHERE id=?", (json.dumps(report), r["id"]))
            # Undo the unsafe release offered by the previous build. Its audit is
            # retained, but a possibly submitted order must rejoin reconciliation.
            for r in db.execute(
                "SELECT * FROM orders WHERE scope=? AND state='abandoned'", (scope,)
            ).fetchall():
                db.execute(
                    "UPDATE orders SET state='unknown',error=?,updated=? WHERE id=?",
                    ("撤销旧版人工解除；持续对账至交易所确认终态", time.time(), r["id"]),
                )
                self.audit(db, r["id"], "abandoned", "unknown", "旧版解除不能证明订单不会成交，恢复阻断")

    def legacy_cutoff(self, scope):
        with self.connect() as db:
            return db.execute(
                "SELECT COALESCE(MAX(cutoff),0) FROM scope_migrations WHERE scope=?", (scope,)
            ).fetchone()[0]

    def save_backtest(self, spec, result):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO backtests(spec,result,created) VALUES(?,?,?)",
                (json.dumps(spec), json.dumps(result), time.time()),
            )
            return cursor.lastrowid
