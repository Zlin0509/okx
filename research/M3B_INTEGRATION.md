# M3B 与 Guard P2 整合／范围索引

统计规格先提交 1c5cc11，独立统计层 c4833f4；随后核验日常开发交付的补丁 07a9d53b51208df09bbeaefba29f14adbc0433d6 并整合为 7074352。本阶段未改交易模块、原并发测试、信号／组合预算或真实研究入口。

## 来源核验与冻结

下载的 bundle、patch、报告、evidence、pytest XML、CLI JSON 均与 SHA256SUMS 中对应条目一致。清单另列的 final-guarded-pytest.xml 和 final-guarded-guard.json 并未作为附件提供；不能声称它们已独立核验，不将它们作为最终证据。最终合并源重新生成统一 M3B_SOURCE_FREEZE.json，M3 与 M3B CLI 均验证该冻结，保留 guard-only／M3B-only 及原 M3 所有旧冻结。最终 CLI／guard／跨版本证据全部重新采集，旧 stdout 不冒充新版本。

## DNS／读取防护的新口径

本版 synthetic_guard 拦截对应 Python audit 事件：原 snapshots 路径的 open／listdir／scandir，socket.connect，以及 getaddrinfo、gethostbyname、gethostbyname_ex、gethostbyaddr、getnameinfo 解析入口。连接与解析分开计数。M3 和 M3B CLI 成功输出本次实际计数及 GUARD_SCOPE；拒绝时退出 3 并输出实际计数和 guard_error。新增两条整合测试在 mock audit 解析入口被拒绝，resolver body 未运行，两类 CLI 解析计数实际为 1；没有真实 DNS 请求或 socket 连接。

仅覆盖当前 Python 进程及线程、且会发出相应 audit 事件的路径。subprocess、native C、直接系统调用及 OS 级隔离不在保证内。统计 CLI 在导入数值库前安装防护；测试在 guarded pytest 下运行。旧 M3_R2_IMPLEMENTATION.md 的 connect-only 描述属于历史版本，当前范围以上述新口径为准，不改写旧冻结文档。

## 统计判定、假阳性与许可

完整 6 候选、44 个实际检验，主 EG 族／差分 ADF 族／收益稳定性族分别 Holm；BY 是 EG 对照，不作为许可选择。已知 pair／basket 合成路径通过；独立随机游走、训练中点结构突变、平稳水平不符合 I(1) 兼容筛查的路径拒绝。固定随机游走残差负例 **rw_residual** 的 EG Holm 调整 p≈0.03865，出现假阳性；保留实际 qualified 筛查状态和 generator_truth=false 并明确标注。没有换种子、调整门槛或事后用真值替换统计输出。FWER 控制不是在每个有限样本上零错误，亦不为真实数据前提不满足提供保证。

收益回归稳定性 CUSUM／固定中点 HAC Wald 是有假设和功效限制的筛查，不证明价格对冲关系永远稳定；不拒绝单位根不证明 I(1)，不拒绝变化不证明稳定。价格 Model 与相对收益残差分开，收益平稳不代替价格协整。完整族缺失、无结果、元数据资格 unknown、非合成 origin、训练数据任一族成员摘要变化，均不得获得模型。许可仅绑定相同 snapshot 时间和完整训练族，不适用于真实币种、实盘或收益结论。

## 复现入口与未决项

安装独立统计锁单（包含传递依赖及下载内容 SHA）：

```bash
python3 -m pip install --require-hashes -r research/statistics/requirements.lock
```

安装后单命令统计验证：`python3 -m research.statistics verify`；旧合成账本：`python3 -m research.m3 verify`。统计数值参考用 rel=1e-7、abs=1e-8 比较，族摘要中的浮点内容哈希单独记录而不要求跨平台相同；离散判定、类型、输入／族计划摘要必须一致。源／规格冻结与 golden 文件 SHA 仍按字节精确校验。核心统计依赖版本精确匹配锁单。

旧缓存读取偏差与并发失败保留；后续全绿不关闭应用并发未决 P1。正式方法的合成实现不等于真实资格、真实训练／锁定样本外或收益／容量结论。数据、发布权限／共享范围、PR／CI 验收继续阻断。本轮不创建或修改唤醒。

旧 M3 合成黄金保留当时的待接字段（例如 summary 的 qualification_tests_pending），本版新增统计实现以 M3B 结果和方法说明为准，不改旧黄金或用其旧字段概括最终能力。原始行情／真实结果隔离、合成黄金、统计参考数值三类证据分别核对。
