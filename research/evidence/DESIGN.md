# ZLIN-11 数据证据离线核验

此入口读取带独立 SHA-256 锚的规范化证据清单，输出 JSON 缺项报告。命令只做字节校验和材料之间的一致性校验，不导入回测执行器。输出始终包含 `real_execution_allowed=false`、`real_returns=null`。R1／R2 的六配置、门槛、冻结日历和 R3 真实入口保持原版本。

## 合成复现

在代码工作树根目录运行，选择新的 dossier 目录。此路径只需 Python 3.11 或更新版本的标准库。

```bash
python -m research.evidence.fixture ./mock-evidence
python -m research.evidence --dossier ./mock-evidence --expected-sha256 "$(sha256sum ./mock-evidence/dossier.json | cut -d' ' -f1)"
```

上面的同机计算 SHA 仅用于合成演练。正式审查须从提交方归档之外取得审核方登记的 dossier SHA，不能以同一目录的自报哈希替代。CLI 将 JSON 写到 stdout。0 表示结构、绑定与已实现的覆盖检查全部通过，2 表示证据不齐。各问题记录 `artifact`、`code`、`count` 和至多六个不同示例。两种退出码都不改变真实执行许可。

## 证据结构

`dossier.json` 记录 schema、origin、UTC 数据截止时刻、编制者、政策锚和固定成员的 SHA-256。九个材料各用同名 JSON 文件。`sources.json` 索引 `sources/<id>.json` 的字节哈希、角色及身份。见证记录参考出处、获取时间与来源类别。所有成员在解析前按字节核验，拒绝重定向与重复字段。证据路径请与旧受控行情和审计缓存分开。

| 材料 | 核验内容 |
|---|---|
| historical_universe_manifest | 全球历史范围、完整现货及 USDT 永续清单、连续 listed／delisted 生命周期、公布／可见／生效三种时间、逐日开收盘覆盖。允许事先公告，也允许事后归档旧事件，但旧事件在历史可见前不得作为已知。标题与地区公告索引不充当有效事件。 |
| complete_candidate_configuration_family | 训练、验证及合格测试网格的 UTC 周一清单，纳入和排除覆盖完整币池。核对冻结六配置、BTC/ETH 锚定的币对及篮子、测试 ID 集合承诺和 Holm／BY／原始检验整数公式。入选至少验证完整上市日、180 日门槛及 512 日窗口加首尾所需收盘。 |
| exact_eligible_dates | 固定训练及验证边界、指定登记的原测试或备用测试区间、至少 365 个已完成且连续的 UTC 日。隔离日期拒绝；备用区间要求原窗口独立判定无效，保留登记边界。 |
| prior_exposure_audit | 审计覆盖、获取截止、已知暴露日期冲突与未知边界。历史证据模式还需提供 R3 原暴露清单的完整固定哈希文件 `known-exposure-r3.json`，核验其公告元数据日期是否逐日结转。它不授予任何历史有效资格。 |
| actual_funding_calendar_and_marks | 独立见证的分段结算 cadence 覆盖所有所需币种日。调度点由段的开始、排他结束、首个点和周期生成，实际费率和精确应用 mark 以币种和调度点匹配，并要求实际结算时间与事件 ID 相同。保留真实零与负费率；缺任一项、重复、非精确 mark、OHLC 或 WS 状态替代均不通过。 |
| spot_baseline_prices | BTC 与周度声明的六至八只现货的日期覆盖，核对可见及有效上市匹配。等权所需开收盘覆盖周二执行至后一周再平衡，首选和换出腿均考虑。 |
| venue_constraints_and_capacity | 检查有效合约规则日期、正 tick／lot、确认的滞后 `volCcyQuote` 证明覆盖，包含日度退出或成交所需的前 30 个完整日。该材料是参与度计算的输入，执行容量仍须另审。 |
| source_and_dependency_freeze | 精确固定 R1 登记、R2 决策、R3 的 110 项源冻结和依赖锁，按 SHA 核查全部源文件。 |
| independent_preregistration_approval | 检查编制者与审核者标识不同、受审成员字节绑定、审核时间晚于或等于数据截止且不晚于回执获取。核对一致性不验证账号身份或准入决定。 |

覆盖段以包含首尾的 UTC 日期表示，结算段按 UTC 半开时间表示。覆盖段是上游规范化证明的声明，不能从最长两端点推断中间完整。日期连接检查负例时会报告漏日，但需要独立审查原始证据的流来源、取得时间、历史市场完整性、公告版本及数值。当前工具不复算行情报价或实际成交，不证明真实全球历史币池的来源完备性，也不重新计算排序、流动性门槛或 venue 深度。需要整日证据的异常上市与退市时段须由审查方明确处理，不拼接跨缺口日历。独立审查仍要核对已归档的历史曝光记录和未解决边界。

本分支只验证合成清单。正式历史证据模式尚未对实际市场材料运行。原始真实结算 mark、早期资金费、历史币池、合格样本外日期和发布认证仍待补齐。
