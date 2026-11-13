# M3B P2 整合索引

本文件补充旧整合文档，统一源冻结为 M3B_SOURCE_FREEZE_V3.json。M3 合成黄金和旧统计参考保留；新模型参数／标签另存 M3B_EXPECTED_R2.json。CLI 在核对新参考前，删除新增模型参数字段后与旧参考逐值比对，完整 batch 和旧标量不变。两类模型始终携带同一许可的追溯字段。

<!-- generated: research.statistics.disclosure; input: statistics JSON -->
| rw_residual 指标 | 机器结果 |
|---|---|
| EG 原始 p | `0.009662979064298902` |
| EG Holm 调整 p（主筛查） | `0.028988937192896705` |
| EG BY 调整 p（仅对照） | `0.03551144806129846` |
| 统计筛查 | `qualified` |
| 生成器关系真值（仅评估注释） | `false` |

该固定合成负例保留为假阳性；真值只在检验和模型许可之后用于评估，不参与筛查、阈值或参数选择。没有更换种子、阈值或 qualified 结果。

旧 M3B_REPORT.md、M3B_INTEGRATION.md 及旧评论的 Holm≈0.038652 为抄录错误；旧文件及冻结保留，本版本勘误。旧机器统计值和筛查判定无需更改。

价格和相对收益模型各自携带不可变 provenance：origin=synthetic_test、source=synthetic_statistical_screen_only、screen_only=true、allowed_use=synthetic_calculation，并绑定候选、决策时间及计划／完整族／训练摘要。单独 dataclasses.asdict／JSON／pickle 仍保留标签；显式请求真实 origin 或 trading／profit／deployment 用途被拒绝。生成器真值和 false_positive 不进入模型或许可逻辑。标签和接口拒绝不构成 Python 进程安全沙箱，也不能辨认调用者谎报的来源或标签被外部主动剥除的副本。

统计有限样本会出错，screen-only 模型不是真实可交易资格、收益／容量或上线证据。快照／完整族绑定检查及原统计规格不变；本版不运行真实训练、选参或样本外。应用并发 P1、数据、发布／共享及 PR／CI 阻断继续保留。

复现：按原独立哈希锁单安装依赖后运行 `python3 -m research.statistics verify` 及 `python3 -m research.m3 verify`。统计 CLI 同时检查两份生成文档与机器结果一致；使用新目录 research/results/m3b_r2，拒绝覆盖。Guard DNS／connect／snapshot 计数、当前进程及线程范围与旧整合版本相同，不覆盖 subprocess、native C、直接系统调用或 OS 隔离。
