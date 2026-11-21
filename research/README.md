# ZLIN-11 M3B 仅代码统计／合成复现

统计依赖与工作台隔离锁定，先在研究虚拟环境安装：

```bash
python3 -m pip install --require-hashes -r research/statistics/requirements.lock
```

统计复现：

```bash
python3 -m research.statistics verify
```

旧合成账本复现 `python3 -m research.m3 verify` 仍只需标准库。完整 mock 回归（另安装项目开发依赖）为 `python3 -m research.synthetic_guard pytest -q`。CI 用相同锁单安装统计库，然后执行两类 CLI 与回归；配置不等于远端 CI 已通过。

方法／原假设与预注册见 M3B_SPEC.md；DNS／进程边界、有限样本假阳性及许可绑定见 M3B_INTEGRATION.md。版本化统一冻结以最终交付清单为准；所有旧冻结保留。Holm 为主许可筛查，BY FDR 仅对照；当前固定负例存在已披露假阳性，不代表真实关系或收益有效。

P2 增量勘误以 M3B_REPORT_R2.md／M3B_INTEGRATION_R2.md 为准，两份文档从机器结果生成原始／Holm／BY 数值。每个筛后价格／收益模型携带 synthetic、screen-only 来源、用途边界和完整族绑定；显式真实用途拒绝。旧文档和冻结保留，新统一冻结为 M3B_SOURCE_FREEZE_V3.json，新参考为 M3B_EXPECTED_R2.json；statistical qualified 和生成器假阳性记录不变。

普通会员费率合成重算：`python3 -m research.fee_scenarios verify`（标准库，无需统计依赖）。首次保存逐笔明细及中文报告可用 `python3 -m research.fee_scenarios write`，拒绝覆盖已有目录。全 Taker 5 bps／Maker 2 bps 及加倍手续费压力；固定原人工成交路径，不输出真实收益、年化或夏普。来源、范围及必要测试见 FEE_SCENARIOS_PLAN.md、FEE_SOURCES.json、FEE_VALIDATION_V1.md，新源冻结 FEE_SOURCE_FREEZE_V1.json 保留旧冻结。

已授权 WS 附件的离线数据接入／质量回放：`python3 -m research.ws_replay ./OKX_LIVE_PUBLIC_WS_120S_20261114.zip --output ./new_ws_replay`，只需标准库。外部 ZIP／raw 锚和 peer 只读入口先校验，再保留可得时点生成诊断；不接账户、不推导资金费现金流或策略收益。数据不随仅代码 Git 历史交付，须由有权限的接收方另行提供同一附件。完整说明见 WS_REPLAY_GUIDE_V1.md；旧入口的真实 origin 拒绝指模型／收益许可，这个 WS 入口只允许冻结附件的数据质量回放。

真实 origin／真实时点元数据未知拒绝；不得运行真实缓存审计、真实选参或输出样本外／上线结论。仅代码历史不含原始快照／真实结果／受控审计目录。合成黄金／统计参考文件是代码测试期望，原始数据隔离另行核验。受控 bundle 不公开推送。旧范围偏差和应用并发未决 P1 保留，不因本轮全绿而关闭。

### R3 合成验收计算

安装已锁定统计依赖后，在仓库根目录使用新输出目录运行：

```bash
python3 -m research.experiments acceptance-synthetic --output ./acceptance_mock
```

`acceptance.json` 含模拟现货持有及历史时点等权基准、逐成交关系交易计数、基础与加倍成本下的 28 日配对块重采样和联合门槛。所有结果均带 `synthetic_test` 来源。真实验收状态为 `unavailable`。口径和拒绝条件见 [R3 计算说明](experiments/DESIGN_R3.md)。

### 冻结账本的中文报告

现货基准、六配置成本敏感性、Sortino、完整月胜率、换手、相关性与回撤归因可用一条离线命令生成。输入及命令见[报告复现说明](reporting/DESIGN.md)。

### 回测前离线证据校验

独立校验入口输出九类材料的缺项、哈希、时间、跨日期与结算配对诊断。规范及合成复现见[数据证据核验说明](evidence/DESIGN.md)。
