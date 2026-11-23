import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from offline_store import SPORTS, import_source, iso_day, number, open_database, read_source
from report import render_report

TEMPLATE = Path(__file__).with_name("report_template.md")


def _date_arg(value):
    try:
        return iso_day(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _positive_arg(value):
    try:
        return number(float(value), positive=True)
    except ValueError:
        raise argparse.ArgumentTypeError("须为有限的正数") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="intervals.icu 训练分析")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("report-template", help="输出中文 Markdown 报告模板")
    run = commands.add_parser("run", help="导入本地 JSON 到 SQLite 并输出中文报告")
    run.add_argument("--input", type=Path, required=True, help="输入 JSON 文件")
    run.add_argument("--db", type=Path, required=True, help="训练报告专用 SQLite 文件")
    run.add_argument("--as-of", type=_date_arg, required=True, help="报告截止日期（含当日，YYYY-MM-DD）")
    run.add_argument("--sport", action="append", choices=SPORTS, required=True, help="运动范围，可重复指定")
    run.add_argument("--baseline-ftp-w", type=_positive_arg, help="比较基线 FTP（瓦）")
    run.add_argument("--baseline-weight-kg", type=_positive_arg, help="比较基线体重（千克）")
    run.add_argument("--target-ftp-w", type=_positive_arg, help="FTP 目标（瓦）")
    args = parser.parse_args()

    try:
        template = TEMPLATE.read_bytes()
        if args.command == "report-template":
            output = template
        else:
            source = read_source(args.input)
            with closing(open_database(args.db)) as db, db:
                sha256 = import_source(db, source)
                output = render_report(db, sha256, args, template.decode("utf-8")).encode("utf-8")
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except ArithmeticError:
        print("错误：报告指标超出可计算范围", file=sys.stderr)
        return 1

    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
