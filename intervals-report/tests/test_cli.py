import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(PROJECT_DIR / "cli.py"), *args],
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

    def test_run_exits_with_an_explanation(self):
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("训练分析流程尚未接入", result.stderr)

    def test_missing_or_unknown_subcommand_is_rejected(self):
        for args in [(), ("unknown",)]:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage:", result.stderr)
