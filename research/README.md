# ZLIN-11 M3 R2 仅代码合成复现

在仓库根目录运行（Linux Python 3.11+ 标准库）：

```bash
python3 -m research.m3 verify
```

只有合成数据生成、只读时点快照、信号／组合和 R2 账本接口，没有真实市场历史。定义／逐项整改及限制见 `M3_R2_PLAN.md` 和 `M3_R2_IMPLEMENTATION.md`。`python3 -m research.m3 real` 无条件拒绝。

完整 mock 回归：`python3 -m research.synthetic_guard pytest -q`（先按项目安装开发依赖）。CI 使用这条命令及 M3 verify，不执行真实缓存审计。本轮本地测试与 GitHub CI 状态分开披露。

资格未知拒绝，正式协整／ADF／稳定性和真实训练／样本外未完成。合成黄金期望仅是合成输出的 SHA／字节数，可以与受控版的合成 manifest 共享 blob；它与原始数据隔离是两个审计口径。全部历史与最终 bundle 对象仍须通过私有数据 blob 交集为 0 的检查。

本分支从安全 1e9681d 继续，保留旧源与冻结，不继承受控快照／真实结果／审计目录。没有授权公开受控 bundle。本地 CLI 日志只证明其自身记录，不是安全沙箱或外部认证。
