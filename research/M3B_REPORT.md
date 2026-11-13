# M3B 统计层与 Guard P2 统一交付（待独立审查）

实现正式方法的合成验证与完整检验族到模型的接口，整合日常开发 07a9d53 补丁。整体保持 in_progress；不放行真实研究、收益／容量／上线结论或交易应用验收。旧缓存越界记录与应用并发未决 P1 保留，后续全绿不代表修复。

## 来源、依赖与冻结

- 预注册 1c5cc11；独立统计层 c4833f4；guard 整合 7074352；最终统一源冻结 ec2cd53（M3B_SOURCE_FREEZE_V2.json）。原统计层、guard-only、首次统一冻结和所有旧 M3 冻结保持原字节，可由对应历史提交恢复。
- 统计环境单独锁定 statsmodels 0.14.6、numpy 2.3.5、scipy 1.16.3、pandas 2.3.3，加全部传递依赖，共 9 个包，requirements.lock 含内容 SHA；无新增应用运行依赖。CI 显式安装此锁单，然后 --no-sync 运行两类 CLI 和回归；本轮未推送，没有远端 CI 通过结论。
- 使用对应版本官方文档而非滚动 stable，安装实现的函数源哈希见 M3B_METHOD_SOURCES.json；参数、原假设及测试规格见 M3B_SPEC.md／M3B_PROTOCOL.json。

## 统计与资格接口

| 方法 | 实现与许可口径 |
|---|---|
| Engle–Granger | 对 log 价格的带截距关系，固定 8 阶、autolag=None。调用 statsmodels coint 的 MacKinnon 协整分布；估计残差普通 ADF 只核对统计量，不拿普通 ADF p 值替代协整 p 值。[官方说明](https://www.statsmodels.org/v0.14.6/generated/statsmodels.tsa.stattools.coint.html) |
| 单位根／平稳性 | 各腿水平与差分 ADF，固定 8 阶。水平不拒绝、差分拒绝仅与 I(1) 相容，不证明 I(1)；普通收益平稳也不证明价格会收敛。[官方说明](https://www.statsmodels.org/v0.14.6/generated/statsmodels.tsa.stattools.adfuller.html) |
| 关系稳定性 | 收益回归残差 CUSUM，加预注册训练中点全部系数变化的 HAC Wald χ²。不是对 I(1) 价格回归滥用普通 CUSUM；有串行相关、渐近近似和功效限制，不拒绝不证明永远稳定。[CUSUM](https://www.statsmodels.org/v0.14.6/generated/statsmodels.stats.diagnostic.breaks_cusumolsresid.html)、[Wald](https://www.statsmodels.org/v0.14.6/generated/statsmodels.regression.linear_model.RegressionResults.wald_test.html) |
| 多重检验 | EG、差分 ADF、稳定性各为预注册完整族，Holm alpha=.05。EG 同时报告 BY FDR 调整，只作对照；不据结果切换许可方法。[官方说明](https://www.statsmodels.org/v0.14.6/generated/statsmodels.stats.multitest.multipletests.html) |
| 模型接线 | 价格残差 Model 与独立相对收益残差模型分开；许可绑定全部族成员的历史摘要、计划摘要、同一 snapshot 时间和已知 mock 元数据。改变任一族成员训练数据、缺项／额外／重复／无结果、真实 origin 或时点资格 unknown 均拒绝，不制造 p 值补齐族 |

训练为 512 个滞后完整日；当前决策日与未来不参与估计。最小样本门槛 256 不意味着允许偷偷缩短预注册窗口。输入为已审查的只读 DecisionSnapshot。相对收益残差为 ΔlogPy-alpha_r-Σ beta_r ΔlogPx，系数／尺度与价格模型分开。AR(1) 半衰期为描述性诊断，不冒充正式平稳性证明。

## 固定合成结果及保留的假阳性

固定种子 290411、700 日人工过程，6 个预注册候选、44 个实际检验。已知 pair／basket 筛查通过，价格 beta 约 1.2008／(.8008,.3048)；独立随机游走、结构突变及不符合 I(1) 前提的平稳水平案例拒绝。样本不足／缺失／低信息不输出完整族许可。

**随机游走残差负例 rw_residual 出现假阳性**：EG 原始 p≈.009663，Holm 后≈.038652，实际筛查为 qualified，而生成器真值为无稳定价格关系。两者都原样交付，不换种子、调门槛或用真值篡改检验输出。FWER 不保证每个有限样本零错误，这也说明合成筛查许可不能充当真实交易资格。没有真实训练／样本外或研究收益结果。

## Guard 整合及测试证据

- 下载的 bundle／patch／已附证据哈希与清单一致；两份清单列出但未附的旧 peer 文件不作最终证据。最终统一冻结 SHA-256：`f5a1fb9ca848a5d3dd98c5135a88f01f4d9ad87db2889573397a57b69840e6d2`。
- M3、M3B CLI 分别重新捕获实际 snapshot／connect／DNS 计数。声明覆盖五种解析 audit 入口与 connect；仅当前 Python 进程及线程发出这些事件的路径，不覆盖 subprocess、native C、直接系统调用或 OS 隔离。新文档索引 M3B_INTEGRATION.md 覆盖旧 connect-only 描述，旧文档不改。
- 两版完整 guarded pytest 各 **262 passed、1 条既有 Starlette 警告**（Python 3.12.3），snapshot／connect／DNS 计数全 0；其中新统计 21、guard P2 7、整合 CLI 失败路径 2 项。Ruff／格式／JS／diff 检查通过；未修改交易模块或既有并发测试。
- 两类 CLI 在两版、Python **3.11.14／3.12.3／3.14.0** 上均重新运行；3.11／3.14 未跑完整 pytest。统计离散判定 SHA 一致 `7bd7980a1558df12af6d7f3f0cf4d65436391a93de823d13b1373514a4c3dbff`，数值在预注册容差内；本机这三版 raw 统计 SHA 也一致，但不将此扩大为任意平台字节一致保证。
- 旧合成账本 golden manifest 仍为 `ef0e026e…9b5378d`，统计结果 SHA 为 `08254c03…8b4aecd`，两类参考与 guard 证据分开。两版两类 CLI 的 DNS／connect mock 共 8 条失败路径均 exit 3、对应实际计数 1，不运行 resolver body／连接 syscall；真实入口均 exit 2。

## 复现与范围

先在研究虚拟环境安装 `python3 -m pip install --require-hashes -r research/statistics/requirements.lock`；然后单命令 `python3 -m research.statistics verify`。旧合成账本 `python3 -m research.m3 verify` 仍为标准库入口。完整 mock 回归另需项目开发依赖，用 `python3 -m research.synthetic_guard pytest -q`。

两类 bundle 继承已审查基线；仅代码完整历史及最终新克隆对象库核验私有 417 个数据 blob 交集为 0，统计参考／合成黄金与原始数据分开分类。受控 bundle 只供原受控审计范围，不公开发布。最终 bundle 的新 stdout／版本／源／哈希记录独立交付，不沿用 guard-only 旧哈希作最终证明。

本轮仅合成／mock；包注册表安装与官方方法文档访问不涉及行情。未执行 verify_m2、读取 M1 缓存、请求市场、读取真实凭据／发订单、修改服务、重试发布或更改唤醒。结算标记价、长历史资金费、历史币池、发布权限／共享范围和真实时点资格仍未解决；真实训练、选参、锁定样本外、收益／容量／上线结论及 PR／CI 未验收。
