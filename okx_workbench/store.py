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
            """)
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

    def save_backtest(self, spec, result):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO backtests(spec,result,created) VALUES(?,?,?)",
                (json.dumps(spec), json.dumps(result), time.time()),
            )
            return cursor.lastrowid
