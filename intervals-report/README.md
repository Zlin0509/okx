# intervals.icu 离线训练报告

本目录是独立的 Python 命令行项目。需要 Python 3.11 或更新版本，运行只需标准库。

在仓库根目录执行：

```bash
python3 intervals-report/cli.py --help
python3 intervals-report/cli.py report-template
python3 -m unittest discover -s intervals-report/tests -v
```

`report-template` 将含占位符的中文 Markdown 模板原样输出到标准输出。`run` 使用调用者提供的本地 JSON，导入 SQLite 后将 UTF-8 中文报告写到标准输出。

```bash
mkdir -p data
python3 intervals-report/cli.py run \
  --input data/intervals-export.json --db data/intervals-report.sqlite3 \
  --as-of YYYY-MM-DD --sport cycling
```

请替换输入文件和截止日期。`--sport` 必填，可重复指定 `cycling`、`running` 或 `swimming`。可通过 `--baseline-ftp-w`、`--baseline-weight-kg` 和 `--target-ftp-w` 提供比较基线及 FTP 目标。

## 本地 JSON 格式

请提供 UTF-8 JSON 对象。下表的三个数组可省略或为空，数组元素只接受表中字段。日期使用 `YYYY-MM-DD`，导入前由调用者转换为 Asia/Shanghai 的日历日期。原始 API 响应需由调用者先转换为此格式。

| 数组 | 必填字段 | 可选字段 |
| --- | --- | --- |
| `activities` | `id`（非空字符串，文件内唯一）、`date`、`sport`（上述 CLI 选项之一） | `duration_seconds`（非负整数秒）、`tss`（非负数）、`power_seconds`（仅骑行，键为非负整瓦的十进制字符串，值为非负整数秒） |
| `wellness` | `date`（数组内唯一） | `weight_kg`（正数）、`ctl` 和 `atl`（非负数）、`tsb`（可为负数） |
| `ftp_history` | `date`（数组内唯一）、`ftp_w`（正数） | 无 |

可选指标可写 `null`。`power_seconds` 的合计在提供活动时长时须小于等于该时长。

## 存储与报告口径

- 每个输入文件独立成一份快照。SQLite 保存原始字节、SHA-256 及规范化记录。同一份文件重复导入时复用已有快照，报告仅使用本次输入对应的快照。请在一个输入文件中提供本次报告所需的完整范围。
- 请在 POSIX 系统（如 Linux、macOS）上使用由调用者控制的专用 SQLite 路径。新库权限为 `0600`。已有库必须由当前用户拥有，且为不授予组或其他用户任何权限的常规文件。数据库路径不能是符号链接。建议将含个人数据的输入、SQLite 和报告放在由调用者控制、Git 忽略的 `data/` 目录中。
- 周统计从周一开始，月统计从当月一日开始，两者均包含截止日。时长和 TSS 汇总按所选运动筛选，只累加有值的记录，并标注覆盖条数。CTL、ATL 和 TSB 使用个人记录。FTP 和体重分别取截止日及之前最后一条有效记录，并标注日期。
- 骑行强度以截止日可用的最新 FTP 划分 Z1–Z5，超过 105% FTP 的时长归入 Z5。占比以本周已提供的功率统计秒数为分母。
- 缺失指标或目标会标明。当前报告给出负荷和目标差距，风险评估和训练建议规则待制定。
- 参数错误退出 `2`。文件、数据、模板或 SQLite 错误退出 `1`，错误信息写到标准错误。输入校验先于入库，SQLite 写入与报告计算在同一事务完成。
