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

真实 origin／真实时点元数据未知拒绝；不得运行真实缓存审计、真实选参或输出样本外／上线结论。仅代码历史不含原始快照／真实结果／受控审计目录。合成黄金／统计参考文件是代码测试期望，原始数据隔离另行核验。受控 bundle 不公开推送。旧范围偏差和应用并发未决 P1 保留，不因本轮全绿而关闭。
