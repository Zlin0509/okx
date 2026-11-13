# M3B 正式统计接口与合成验证（先冻结）

范围：独立 statistics 模块，仅已标注的合成快照；不修改 guard／CLI 计数文件或交易模块，不运行 verify_m2，不读 M1，不请求市场。应用并发未决 P1、旧缓存越界及旧冻结／结果保留。依赖与方法先冻结后实现，参数不根据真实收益选择。

## 方法与定义

statsmodels 0.14.6 及 numpy／scipy／pandas 精确版本与完整传递依赖 SHA 锁单单独交付。文档使用对应版本而非滚动 stable。EG 对 y=log(Py)、X=log(Px_i)，带截距第一步 OLS，固定 8 阶、autolag=None，调用 coint(method='aeg',trend='c',return_results=False)；主 p 值和临界值使用该库 MacKinnon 协整分布。不能把估计价格残差的普通 ADF p 值当成 EG p 值；残差 ADF(regression='n') 只记录统计量用于核对 coint 的第二步，不输出其普通 ADF p 值。来源：[官方 coint](https://www.statsmodels.org/v0.14.6/generated/statsmodels.tsa.stattools.coint.html)。

分别对每腿 log 水平与一阶差分调用固定 8 阶 ADF(regression='c')。水平不拒绝单位根、差分拒绝单位根只是与 I(1) 相容，不能证明 I(1) 或把不拒绝当稳定性证据；平稳价格腿不进入本 EG 许可路径。AR(1) 残差半衰期仍为描述性诊断，phi 不在 (0,1) 时拒绝该许可，不伪造半衰期。来源：[官方 ADF](https://www.statsmodels.org/v0.14.6/generated/statsmodels.tsa.stattools.adfuller.html)。

相对收益残差定义为 ΔlogPy - alpha_r - Σ beta_r_i ΔlogPx_i；独立 OLS，与价格协整 beta、价格残差和其尺度分开记录。收益平稳不蕴含价格协整。关系稳定性使用该收益回归的 OLS 残差 CUSUM（ddof 为截距加回归系数数目）；另在预设训练中点增加全部系数与 split indicator 的交互项，用 HAC(Bartlett,8阶,小样本修正) 协方差进行联合 Wald χ² 检验。断点不扫描；这是预设中点和累计残差筛查，非未知断点一致性证明。不会把普通 CUSUM 的 p 值直接用于 I(1) 价格 OLS 的正式推断。来源：[CUSUM](https://www.statsmodels.org/v0.14.6/generated/statsmodels.stats.diagnostic.breaks_cusumolsresid.html)、[HAC](https://www.statsmodels.org/v0.14.6/generated/statsmodels.regression.linear_model.RegressionResults.get_robustcov_results.html)、[Wald](https://www.statsmodels.org/v0.14.6/generated/statsmodels.regression.linear_model.RegressionResults.wald_test.html)。CUSUM 低功效、序列相关、样本长度和渐近近似均是限制；不拒绝变化不等于证明永久稳定。

## 时点、检验族与许可

固定 512 个滞后完整日训练（至少 256），决策日不参与任何估计；输入必须是 M3 已知且不可变的 DecisionSnapshot。只取 ts<decision 且 available_at<=decision；间断、缺失、非正／非有限、零残差尺度、奇异矩阵、版本不匹配、统计异常／警告均拒绝。不静默缩短窗口或补值。

FamilyPlan 在看数据前注册候选方向、币种 tuple、训练长度及测试 ID。EG 主族含每个预注册候选一个检验；差分 ADF 族含每候选各腿（共享币重复也保守保留）；稳定性族含每候选 CUSUM 与中点 Wald。各族用 Holm alpha=.05。水平 ADF 作为保守 I(1) 兼容性排除筛查，不拒绝不能作为单位根的证明。EG 必须调整后拒绝无协整、差分 ADF 必须调整后拒绝单位根，稳定性族不得检出变化，且半衰期诊断有效，才能产生**合成统计筛查**许可。

报告同时计算 EG 全族 Benjamini–Yekutieli FDR 调整，仅作对照，不用于切换许可或按结果选择方法。Holm 控制 FWER，BY 控制 FDR，均依赖有效输入 p 值；不宣称在真实非理想数据中自动成立。来源：[官方 multipletests](https://www.statsmodels.org/v0.14.6/generated/statsmodels.stats.multitest.multipletests.html)。

所有预注册候选和测试必须出现，缺项／额外／重复／无结果（如样本不足）使族判定不可用，不制造 p=1 冒充已执行检验，不从族删掉失败者。元数据可交易资格 unknown 时即使统计通过也拒绝许可；M3B 对真实 origin 无条件拒绝。许可绑定方向、snapshot 时间、训练样本摘要和完整族摘要，过期／别族／改历史均拒绝。提供从族结果到已冻结价格 Model 与收益残差的接口，不改 M3 交易或真实策略入口。

## 合成规格与验收

固定种子 290411 生成 700 日：已知 pair beta1.2、basket beta(.8,.3) 加 AR(.8) 平稳残差，独立随机游走／随机游走残差负例，训练中点 beta 改变的结构突变，平稳水平的错误 I(1) 负例。随机过程有限样本可能出现误报／漏报，不调门槛保证每次识别；无真实参数选择。先固定规格，再记录实际行为及限制。

测试：官方函数逐值核对、残差 ADF 统计量与 EG 相等但 p 分布口径不同、已知关系／非关系／突变、样本不足／零波动／间断、可得性／未来扰动、族缺失／重复、真实时点资格 unknown 拒绝、snapshot／族摘要绑定、价格／相对收益模型区分及 Holm／BY 示例。跨 Python 3.11／3.12／3.14 验证容差内数值与精确离散判定，不假装浮点统计跨平台字节完全相同；记录依赖、线程配置、版本和真实覆盖。

交付可执行合成入口、机器可读原始／调整统计结果及许可原因、中文范围说明、源／规格冻结和逐文件哈希、两类隔离 bundle。统计安装只访问包注册表及官方方法文档，不获取市场数据。guard 改动由日常开发另交补丁，集成后单独记录，不覆盖旧冻结。
