# M3 R2 接口／范围说明

先冻结规格 a33d6c0，然后实现本轮整改。仅合成／mock；旧 M3 报告、首版／V2 冻结和越界记录保留，新规则／源／结果使用独立 R2 文件，不追溯修改旧验收结论。

## P1 公开接口

`CausalEngine.run_causal(decide, observations)` 的数据源由驱动器持有；`Portfolio(relations, params, stress)` 不再接收、保存 observations／provider／engine，且无历史源闭包。每个 callback 仅 `decide(DecisionSnapshot)`，含当前价、当前权益、当前仓位、已发且未成交意图摘要、已发生成交、按 `ts<=now && available_at<=now` 放行的观测。待单摘要不含未来成交价、成交时间或未来市场日历。数据全部复制为独立 mappingproxy、tuple 和 frozen slots 类型，包括持仓 leg key；保留快照不会随驱动推进增长，也不回指源 dict 或引擎。策略返回 `Decision(signals, cancel_books)`，不能通过引擎对象修改待单。旧三参数 callback 和继承的非因果 run 入口拒绝。

负向测试涵盖未来键、完整引擎／market／provider 属性、枚举、嵌套映射和属性修改、延迟可得和捕获快照不增长。反射、全局变量、策略自行导入其他模块或自行保存外部数据不属于这一接口保证；它**不是 Python 同进程安全沙箱**。原未来扰动与 R2 同单完整账本一致测试仍保留。

## P2 与风险预算

`allocate` 跳过零名义的上限除法，空／全零分配返回 `{}`,0；混合空 book 不生成无意义目标，零 beta 腿保持显式零。目标 .80／.24，增加风险的实际成交仍由 R2 检查 1／.25，数值不变。

`risk_exposure.csv`／JSON 对每个原始账本事件记录 equity、gross、net、每币绝对金额、动态硬限、超额和是否减仓。`breach_episodes` 记录开始与恢复的 ts／seq、峰值、持续 ms、超限事件数和右删失；减仓仍超限的行继续标为 `hard_limit_breached=true`，不被文案豁免。减仓只能减少原腿，不能翻向／开仓。顺序平仓时其他腿仍可能超限，费用也会降低权益；并非声称每个 risk_fill 后已恢复合规。冻结 synthetic hard_drift 中从收盘至下一开盘为 1ms 的抽象网格，不能推断真实响应延迟；另有延迟风险腿的日级持续超限测试。

Holm alpha 明确写入 `M3_PROTOCOL_R2.json` 为 .05，并与函数默认常量测试对齐。它只计算外部完整族的 FWER 调整，没有实现 FDR、正式 p 值生成或调整到资格的接线。summary 补列协整、ADF／单位根／平稳性、稳定性、相对收益残差、时点候选资格、检验族／资格接线和 FDR 待接项；这些不因合成测试通过而授予真实资格。

## 合成范围与可复现入口

先审读所有 research 测试、fixture、共用 conftest 及应用 mock：行情测试构造临时 `.body`／`.request.json`；`data_audit.verify(tmp_path)` 只校验测试临时数据；`verify_m2` 的导入仅供 gzip 清单辅助函数使用，没有调用其 `main/verify_controlled`；资金费真实入口测试只断言立即拒绝。应用测试使用内存 fixture／httpx.MockTransport／FakeOKX 和临时 SQLite，无市场请求和真实凭据。

`python3 -m research.synthetic_guard pytest -q` 为适用全回归入口，拦截当前 Python 进程及线程对原 `research/snapshots` 的 open／listdir／scandir 和网络 connect，非零意外拦截次数使验证退出非零；独立负向 guard 测试只使用临时 mock 路径。防护不是外部证明、系统级沙箱或子进程审计；它加上已审读的 mock／子进程路径说明本次覆盖。CLI `python3 -m research.m3 verify` 也自动启用防护，只重算合成黄金结果。真实动作继续拒绝，不执行 verify_m2。所有 pytest 版本／命令／结果与跨版本 CLI 单独记录，不混称覆盖。

## CI、旧证据与隔离口径

受控及仅代码的 CI 都仅运行 guarded pytest 和 M3 verify，不再执行真实缓存审计。仅代码 README 首条复现命令为 M3。workflow 是待发布代码，本轮没有推送，也没有 GitHub CI 成功结果。

黄金期望 JSON 仅包含固定合成输出的文件长度与 SHA，可与受控目录内**合成** manifest 共享 Git blob；共享该 blob 不等于泄露原始市场数据。隔离审计分别列出：继承自 d8ff081 的原始快照／真实结果／审计 blob 交集必须为 0；合成黄金共享 blob 独立白名单列出确切路径与哈希，不将其混作原始数据。仍检查仅代码完整历史的禁止路径及最终 bundle 全对象库。受控 bundle 仅限原受控审计范围，不公开发布。

旧越界记录保持原样：上一轮额外执行过一次旧 verify_m2，不能改称历史“零读取”；本轮禁止重演。真实数据、正式统计检验、训练／锁定样本外、容量敏感性、PR／CI 与数据共享条件仍待满足。

首次受保护完整回归为 230 passed／1 failed（既有 test_deduplication_survives_new_engine_and_concurrent_calls，mock submissions 为 0），读取／网络防护计数均 0。保留原 JUnit，不改交易模块或该并发测试；后续独立重跑结果另列，不能抹去首次失败。审查源可见并发 reconcile 与提交状态之间可能存在时序竞争，此处不声称已经修复或确诊。

首版捕获的 audit/m3/validation_runner.py 不符合当前 Ruff 格式；为保留其原字节，Ruff 排除 research/audit 捕获目录，继续检查所有实际源／测试与新代码。不是格式化旧证据或跳过新研究源；代码版没有该受控目录，采用相同配置。
