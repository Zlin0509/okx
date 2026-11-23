import hashlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def run_cli(self, *args: str, project_dir=PROJECT_DIR) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(project_dir / "cli.py"), *args],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )

    def test_help_lists_subcommands(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("report-template", result.stdout)
        self.assertIn("run", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_template_outputs_markdown(self):
        result = self.run_cli("report-template")
        template = (PROJECT_DIR / "report_template.md").read_text(encoding="utf-8")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, template)
        self.assertIn("# 训练分析报告", result.stdout)
        self.assertIn("{{data_coverage}}", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_run_requires_inputs_and_explicit_sport(self):
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("--sport", result.stderr)
        self.assertIn("--input", result.stderr)

    def test_missing_template_is_reported_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for filename in ("cli.py", "offline_store.py", "report.py"):
                shutil.copyfile(PROJECT_DIR / filename, folder / filename)
            result = self.run_cli("report-template", project_dir=folder)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("report_template.md", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_or_unknown_subcommand_is_rejected(self):
        for args in [(), ("unknown",)]:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage:", result.stderr)

    def setUp(self):
        self.workdir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.workdir / "input.json"
        self.db = self.workdir / "report.sqlite3"

    def run_report(self, *args):
        return self.run_cli("run", "--input", str(self.source), "--db", str(self.db), *args)

    def test_empty_import_produces_report_and_is_idempotent(self):
        raw = b'{"activities": [], "wellness": [], "ftp_history": []}\n'
        self.source.write_bytes(raw)
        args = ("--as-of", "2026-11-01", "--sport", "cycling", "--sport", "running")
        first = self.run_report(*args)
        second = self.run_report(*args)
        for result in (first, second):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn("本周 2026-10-26 至 2026-11-01，本月 2026-11-01 至 2026-11-01", first.stdout)
        self.assertIn("运动范围：骑行、跑步", first.stdout)
        self.assertIn("本周 0 条活动，本月 0 条活动", first.stdout)
        self.assertIn("| TSS 合计 | 无数据 | 无数据 |", first.stdout)
        self.assertIn("未提供 FTP 目标", first.stdout)
        self.assertNotIn("{{", first.stdout)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(
                db.execute("SELECT * FROM imports").fetchall(), [(hashlib.sha256(raw).hexdigest(), raw)]
            )
            for table in ("activities", "wellness", "ftp_history"):
                self.assertEqual(db.execute(f"SELECT count(*) FROM {table}").fetchone(), (0,))
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)

    def test_target_with_no_current_ftp_and_non_cycling_scope(self):
        self.source.write_text("{}", encoding="utf-8")
        result = self.run_report("--as-of", "2026-11-23", "--sport", "swimming", "--target-ftp-w", "200")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("运动范围：游泳", result.stdout)
        self.assertIn("目标 200.0 W，缺少当前 FTP，无法计算差距", result.stdout)
        self.assertIn("所选运动范围未包含骑行", result.stdout)

    def test_bad_input_leaves_database_uncreated(self):
        for raw, message in (
            (b'{"activities":', "有效的 UTF-8 JSON"),
            (b"\xff", "有效的 UTF-8 JSON"),
            (b"[]", "顶层须为对象"),
            (b'{"activities": {}}', "activities 须为数组"),
            (b'{"activities": [null]}', "activities 第 1 条记录"),
            (b'{"wellness": [{}]}', "wellness 第 1 条记录"),
            (b'{"wellness": [], "wellness": []}', "重复键"),
            (b'{"wellness": NaN}', "非有限数值"),
        ):
            with self.subTest(raw=raw):
                self.source.write_bytes(raw)
                result = self.run_report("--as-of", "2026-11-23", "--sport", "cycling")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.db.exists())

    def test_missing_file_and_bad_options(self):
        result = self.run_report("--as-of", "2026-11-23", "--sport", "cycling")
        self.assertEqual(result.returncode, 1)
        self.source.write_text("{}", encoding="utf-8")
        for args in (
            ("--as-of", "2026-02-29", "--sport", "cycling"),
            ("--as-of", "20261123", "--sport", "cycling"),
            ("--as-of", "2026-11-23", "--sport", "all"),
            ("--as-of", "2026-11-23", "--sport", "cycling", "--target-ftp-w", "inf"),
        ):
            with self.subTest(args=args):
                result = self.run_report(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.db.exists())

    def test_existing_unrelated_database_is_preserved(self):
        self.source.write_text("{}", encoding="utf-8")
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE unrelated (id INT)")
        before = self.db.read_bytes()
        result = self.run_report("--as-of", "2026-11-23", "--sport", "running")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("专用 SQLite", result.stderr)
        self.assertEqual(self.db.read_bytes(), before)

    def test_render_failure_rolls_back_import(self):
        self.source.write_text("{}", encoding="utf-8")
        args = ("--as-of", "2026-11-23", "--sport", "cycling")
        first = self.run_report(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.source.write_text('{"activities": []}', encoding="utf-8")
        for filename in ("cli.py", "offline_store.py", "report.py"):
            shutil.copyfile(PROJECT_DIR / filename, self.workdir / filename)
        (self.workdir / "report_template.md").write_text("{{unknown_field}}", encoding="utf-8")
        result = self.run_cli(
            "run", "--input", str(self.source), "--db", str(self.db), *args, project_dir=self.workdir
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("报告模板字段与渲染器不匹配", result.stderr)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT source_json FROM imports").fetchall(), [(b"{}",)])
        retry = self.run_report(*args)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM imports").fetchone(), (2,))
