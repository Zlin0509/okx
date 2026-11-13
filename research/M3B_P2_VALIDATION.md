# M3B 两项 P2 整改与验证

规格提交 63eee39；实现统一冻结提交 0435e18；仅代码对应 747f51b／3c05d2b，入口索引 ff334b2。最终 bundle 提交及附件哈希在独立交付清单，不用报告自身构造循环哈希。

| 复审项 | 增量改动及证据 |
|---|---|
| rw_residual 数值披露错误 | disclosure.documents 从冻结机器 JSON 生成 M3B_REPORT_R2.md 和 M3B_INTEGRATION_R2.md；CLI 同时检查文档一致。原始 0.009662979064298902、Holm 0.028988937192896705、BY 0.03551144806129846；旧报告和评论的乘四错误明确勘误，不改写历史 |
| 脱离 batch 的模型失去 screen-only 标签 | qualified_models 的价格／收益模型各带不可变 ModelProvenance，含 synthetic origin、screen-only 来源、用途限制、候选、时间及计划／完整族／训练摘要；asdict→JSON、pickle 独立序列化保留标签。显式真实 origin／交易／收益／部署用途请求拒绝。标签不使用生成器真值，也不以清除假阳性为目的 |
| 原结果与资格绑定 | legacy_projection 仅删新增模型参数，其余全部与旧黄金核对；本机 JSON 正规化后的完整 batch 与旧版逐值完全一致。旧快照／检验族篡改拒绝、未来扰动不改变历史的测试继续通过 |
| 冻结与旧证据 | V3 新冻结含 49 项（含 V2 原文件与新增规格／文档／黄金／测试）。旧规格、生成器、报告、黄金及所有旧冻结未改；新结果仅写 research/results/m3b_r2。旧快照、应用、原并发测试、研究账本源 Git tree 比较未变，未打开快照内容 |

## 实际测试覆盖

- 受控与仅代码各运行一次最终完整 guarded pytest：Python 3.12.3 各 273 passed，0 failed/error/skipped，1 条既有 Starlette 警告；父进程 snapshot／connect／DNS guard 计数均 0。新增 P2 11 项，原统计 21 项、整合 CLI DNS 负向 2 项和 guard 7 项均覆盖。
- 定向最终 34 passed。新增模型标签／绑定／不可变／JSON／pickle 保留、真实用途拒绝、真值仅评估、旧结果一致、两文档自动生成及漂移拒绝。开发初次 4 failed（tuple/list 比较及生成中止导致缺文件）已在初始 targeted.xml 保留，修正 JSON 正规化后再生成；不代表统计方法或阈值变更。
- 两版两类 CLI 在 Python 3.11.14／3.12.3／3.14.0 各 verify=0、real=2，共 24 次；正常三个 guard 计数均 0。3.11／3.14 仅 CLI，未跑完整 pytest。使用原有哈希锁定研究环境，本轮未下载依赖或请求行情。
- 筛查判定 SHA 保持 7bd7980a1558df12af6d7f3f0cf4d65436391a93de823d13b1373514a4c3dbff；新增有标签参数后统计 JSON SHA 为 6139dfb26d2c089481061d3b8dc94ef81013e7695f75a5182c032858ecddac24。旧 08254c03…8b4aecd 原字节保留。当前三版机器结果字节一致不扩大为跨平台保证；原数值容差／离散判定严格规则不变。
- Ruff 检查／格式、JS 语法和 diff 检查通过。命令、UTC、HEAD、最终源冻结、退出码、stdout/stderr 哈希及 JUnit 在新证据目录；运行次数按新日志计算，不追溯证明旧运行次数。

## 范围与未决

Screen-only 标签不构成 Python 进程安全沙箱，不能辨别外部调用者谎报 origin 或主动剥除标签的参数副本。Guard 仅当前进程／线程 audit 入口，不保证 subprocess、native C、直接 syscall 或 OS 隔离。每个模型的适用边界随对象及序列化保留，不依赖旁边的评估真值注释。

种子、阈值、qualified、固定 rw_residual 假阳性和原生成器真值均保留；真值只经筛后 evaluation_notes 汇总，不参与检验、模型许可或参数调整。接口／统计合成阶段通过不代表真实市场资格或上线结论。

应用并发首次失败 P1 未修复、未关闭；后续 273 全绿不替代诊断。本轮未读 M1 缓存、运行 verify_m2、跑真实窗口、请求市场、读取交易凭据、下单、改服务、重试发布或变更唤醒。真实训练／选参／样本外、数据与发布／共享条件及 PR／CI 仍阻断，整体 in_progress，交付后待针对性独立复审。
