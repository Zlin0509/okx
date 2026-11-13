# ZLIN-11 仅代码交付

本分支只包含离线研究代码、明确标注的合成算例和测试；不含 OKX 原始快照、真实窗口结果或包含这些数据的父提交。它从功能基线 `d67cfd2` 重新构建，并检查完整可达历史。

Python 3.11+ 无依赖、无网络算术核验：

```bash
python3 -m research.m2 unit
```

R2 计算规则见 `M2_PROTOCOL_R2.json`，测试规格见 `M2_R2_TEST_SPEC.md`；controlled_base 和 code_base 分开记录。真实计算入口本轮禁用。

这不是策略收益或完整研究验收。缺受控快照运行 `python3 -m research.verify_m2` 或 `python3 -m research.m2 verify` 会退出 2，明确报 `controlled_snapshot_required`。不自动下载数据，不把缺数据视为通过。

M2 CLI 在被 Git 忽略的 `research/runtime/` 追加 UTC／HEAD／argv／源与结果哈希日志；本地日志不是外部认证或全局不可重跑保证。仅代码 CI 只验证应用及合成算例，真实覆盖验收另行保留。
