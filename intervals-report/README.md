# intervals.icu 训练分析骨架

本目录是独立的 Python 命令行项目。需要 Python 3.11 或更新版本，运行只需标准库。

在仓库根目录执行：

```bash
python3 intervals-report/cli.py --help
python3 intervals-report/cli.py report-template
python3 -m unittest discover -s intervals-report/tests -v
```

`report-template` 将 UTF-8 中文 Markdown 模板输出到标准输出。模板中的 `{{...}}` 标记留给后续报告生成逻辑填写。

`python3 intervals-report/cli.py run` 是预留的分析入口。当前会向标准错误说明流程尚未接入，并以状态码 `1` 退出。
