import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="intervals.icu 训练分析")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("report-template", help="输出中文 Markdown 报告模板")
    commands.add_parser("run", help="运行训练分析流程（预留）")
    args = parser.parse_args()

    if args.command == "report-template":
        template = Path(__file__).with_name("report_template.md")
        sys.stdout.buffer.write(template.read_bytes())
        return 0

    print("训练分析流程尚未接入。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
