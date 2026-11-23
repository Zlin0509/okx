import hashlib
import json
import math
import os
import sqlite3
from datetime import date
from pathlib import Path

SPORTS = {"cycling": "骑行", "running": "跑步", "swimming": "游泳"}
APPLICATION_ID = 0x49525054  # IRPT
INT64_MAX = 2**63 - 1

SCHEMA = (
    "CREATE TABLE imports (sha256 TEXT PRIMARY KEY, source_json BLOB NOT NULL)",
    """CREATE TABLE activities (
        sha256 TEXT NOT NULL REFERENCES imports, id TEXT NOT NULL, day TEXT NOT NULL,
        sport TEXT NOT NULL, duration_seconds INTEGER, tss REAL, power_seconds TEXT,
        PRIMARY KEY (sha256, id)
    )""",
    """CREATE TABLE wellness (
        sha256 TEXT NOT NULL REFERENCES imports, day TEXT NOT NULL,
        weight_kg REAL, ctl REAL, atl REAL, tsb REAL, PRIMARY KEY (sha256, day)
    )""",
    """CREATE TABLE ftp_history (
        sha256 TEXT NOT NULL REFERENCES imports, day TEXT NOT NULL,
        ftp_w REAL NOT NULL, PRIMARY KEY (sha256, day)
    )""",
)


def iso_day(value):
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise ValueError("日期须为 YYYY-MM-DD")


def number(value, *, minimum=None, positive=False):
    try:
        if type(value) in (int, float) and math.isfinite(value):
            if (minimum is None or value >= minimum) and (not positive or value > 0):
                return float(value)
    except OverflowError:
        pass
    raise ValueError("数值无效或超出范围")


def _optional_number(row, key, **bounds):
    try:
        return None if row.get(key) is None else number(row[key], **bounds)
    except ValueError as error:
        raise ValueError(f"{key} {error}") from None


def _seconds(value):
    if type(value) is int and 0 <= value <= INT64_MAX:
        return value
    raise ValueError("时长须为非负整数秒且可存入 SQLite")


def _power_histogram(value, duration):
    if not isinstance(value, dict):
        raise ValueError("power_seconds 须为整瓦到整数秒的对象")
    for power, seconds in value.items():
        if not power.isascii() or not power.isdecimal() or len(power) > 19:
            raise ValueError("power_seconds 的功率键须为非负整瓦")
        watts = int(power)
        if str(watts) != power or watts > INT64_MAX:
            raise ValueError("power_seconds 的功率键须为规范的非负整瓦")
        _seconds(seconds)
    if duration is not None and sum(value.values()) > duration:
        raise ValueError("power_seconds 合计超出活动时长")
    return json.dumps(value, separators=(",", ":"))


def _activity(row):
    activity_id = row.get("id")
    if not isinstance(activity_id, str) or not activity_id.strip():
        raise ValueError("id 须为非空字符串")
    try:
        activity_id.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("id 含无效的 Unicode 字符") from None
    sport = row.get("sport")
    if not isinstance(sport, str) or sport not in SPORTS:
        raise ValueError("sport 须为 cycling、running 或 swimming")
    duration = row.get("duration_seconds")
    if duration is not None:
        duration = _seconds(duration)
    power = row.get("power_seconds")
    if power is not None:
        if sport != "cycling":
            raise ValueError("power_seconds 只适用于骑行")
        power = _power_histogram(power, duration)
    return (
        activity_id,
        iso_day(row.get("date")).isoformat(),
        sport,
        duration,
        _optional_number(row, "tss", minimum=0),
        power,
    )


def _wellness(row):
    return (
        iso_day(row.get("date")).isoformat(),
        _optional_number(row, "weight_kg", positive=True),
        _optional_number(row, "ctl", minimum=0),
        _optional_number(row, "atl", minimum=0),
        _optional_number(row, "tsb"),
    )


def _ftp(row):
    watts = _optional_number(row, "ftp_w", positive=True)
    if watts is None:
        raise ValueError("须提供 ftp_w")
    return iso_day(row.get("date")).isoformat(), watts


def _rows(document, key, allowed, convert):
    entries = document.get(key, [])
    if not isinstance(entries, list):
        raise ValueError(f"{key} 须为数组")
    seen = set()
    rows = []
    for index, entry in enumerate(entries, 1):
        try:
            if not isinstance(entry, dict) or entry.keys() - allowed:
                raise ValueError("记录须为对象，且字段须符合导入格式")
            row = convert(entry)
            if row[0] in seen:
                raise ValueError("活动 id 或日期重复")
            seen.add(row[0])
            rows.append(row)
        except ValueError as error:
            raise ValueError(f"{key} 第 {index} 条记录：{error}") from None
    return rows


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("JSON 对象含重复键")
    return result


def _invalid_constant(_value):
    raise ValueError("JSON 含非有限数值")


def read_source(path):
    raw = path.read_bytes()
    try:
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_invalid_constant
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("输入须为有效的 UTF-8 JSON") from None
    if not isinstance(document, dict) or document.keys() - {"activities", "wellness", "ftp_history"}:
        raise ValueError("顶层须为对象，字段限 activities、wellness 和 ftp_history")
    activities = _rows(
        document, "activities", {"id", "date", "sport", "duration_seconds", "tss", "power_seconds"}, _activity
    )
    wellness = _rows(document, "wellness", {"date", "weight_kg", "ctl", "atl", "tsb"}, _wellness)
    ftp = _rows(document, "ftp_history", {"date", "ftp_w"}, _ftp)
    return raw, activities, wellness, ftp


def open_database(path: Path):
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        pass
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def import_source(db, source):
    raw, activities, wellness, ftp = source
    sha256 = hashlib.sha256(raw).hexdigest()
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("BEGIN IMMEDIATE")
    db_id = db.execute("PRAGMA application_id").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if (db_id, version) != (APPLICATION_ID, 1):
        if db_id or version or db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone() is not None:
            raise ValueError("请使用训练报告专用 SQLite 数据库（版本 1）")
        for statement in SCHEMA:
            db.execute(statement)
        db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        db.execute("PRAGMA user_version = 1")

    inserted = db.execute("INSERT INTO imports VALUES (?, ?) ON CONFLICT (sha256) DO NOTHING", (sha256, raw))
    if inserted.rowcount:
        db.executemany(
            "INSERT INTO activities VALUES (?, ?, ?, ?, ?, ?, ?)", ((sha256, *r) for r in activities)
        )
        db.executemany("INSERT INTO wellness VALUES (?, ?, ?, ?, ?, ?)", ((sha256, *r) for r in wellness))
        db.executemany("INSERT INTO ftp_history VALUES (?, ?, ?)", ((sha256, *r) for r in ftp))
    return sha256
