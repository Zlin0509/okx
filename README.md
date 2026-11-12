# OKX 自动交易工作台

可本地运行的合约策略研究与自动执行平台。Python / FastAPI + SQLite + 原生 JavaScript，中文控制台，无外部 CDN。默认 `demo` 环境、离线演练数据源，不需要密钥；接入 OKX 后仍默认模拟盘。

## 启动

需要 macOS 或 Linux、Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
uv sync --frozen --group dev
cp .env.example .env
chmod 600 .env
uv run uvicorn okx_workbench.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000>；接口文档 <http://127.0.0.1:8000/docs>。终端 `Ctrl+C` 停止服务。每个数据库只允许运行一个服务进程，请勿使用多个 worker。仅绑定本机；此版本不提供多人身份认证，不应公开到互联网。

操作顺序：选择合约和周期 → 自动生成策略 → 检查后 30% 样本外结果 → 按需调整参数运行全样本回测 → 确认环境后启动 → 查看执行记录。生成与回测不会启动交易。策略启动后每 15 秒检查已完成 K 线，可手动“执行一次信号”。页面启动确认显示保存的参数，修改表单只影响新回测，重新生成才能保存新策略。一次仅启用一个策略。

离线行情是随时间滚动的确定性合成 OHLC，不是真实市场行情；本地订单和收益只计入演练账本。`1H` 信号可能为空仓，未出现开仓信号时不会制造订单。回测与验证可在无密钥情况下完成。

## 连接 OKX 模拟盘

1. 在 OKX **模拟交易**环境创建 API Key，使用独立测试子账户，授予读取和交易权限，无需提币权限。
2. 在服务端 `.env` 填写以下项，保留 `OKX_MODE=demo`：

```dotenv
OKX_MODE=demo
OKX_DATA_SOURCE=okx
OKX_API_KEY=你的模拟盘APIKey
OKX_SECRET_KEY=你的Secret
OKX_PASSPHRASE=你的Passphrase
```

3. 在 OKX 设置单向持仓（`net_mode`），准备 USDT 模拟余额，目标合约使用逐仓模式，建议先设为 1 倍杠杆。软件不会修改账户持仓模式或杠杆。使用无其他机器人/人工交易的独立子账户，保证风险快照与实际执行一致。
4. 重启服务，**重新生成并验证策略**后手动启动；界面显示“OKX 模拟盘”。模拟请求带 `x-simulated-trading: 1`，凭据只在服务端读取，不写入数据库、不返回浏览器。环境变量优先于 `.env`。

支持官方域名 `https://www.okx.com`、`https://us.okx.com`、`https://eea.okx.com`；按注册地区在 `.env` 设置 `OKX_BASE_URL`。不会向自定义域名或重定向目标发送密钥。

支持 BTC-USDT-SWAP、ETH-USDT-SWAP，1H / 4H / 1D，USDT 本位线性永续；目标合约只接管单向逐仓多仓。已有空仓、全仓或双向持仓会被拒绝。请不要让软件接管同合约的人工作业仓位。

## 实盘必须显式开启

默认不连接实盘。本次仅在用户授权的 OKX 模拟账户完成受限交易验证，未连接实盘。用户决定启用实盘时，需在服务端同时配置：

```dotenv
OKX_MODE=live
OKX_DATA_SOURCE=okx
OKX_LIVE_ACK=I_UNDERSTAND_LIVE_TRADING_USES_REAL_FUNDS
# 同时换为实盘账户的凭据
```

缺少确认短语时服务拒绝启动。控制台没有环境切换接口；实盘界面持续显示红色标识，启动前另需手动输入 `START_LIVE`。本次已完成受限模拟账户开仓与平仓联调，实盘能力仍需人工审查。

## 策略和回测口径

- 策略为均线趋势的**做多 / 空仓**模型：快均线大于慢均线时目标为多仓，否则目标为空仓；不叠加仓位，不反手做空。交易量受预算、账户权益和硬限额共同限制。
- 自动生成搜索 9 组均线参数，仅用前 70% 连续 K 线按“收益率减最大回撤”选参，后 30% 独立验证。它是可解释的规则搜索，不是大模型预测或收益承诺。
- 使用最近最多 300 根 K 线，过滤未完成 K 线并验证时间连续。不是长历史研究系统。1D 在 OKX 按交易所默认日线边界；离线按 UTC 生成。
- 回测从上一根收盘获取信号，下一根开盘成交；最后一根收盘强制平仓。计算净收益、权益曲线、最大回撤、交易胜率、交易数和手续费，可导出权益 CSV。
- 理论回测含双边手续费和滑点，不含资金费、清算、实际盘口深度；连续数量未按合约张数取整。实际执行受最小张数约束，不能直接用回测收益推断模拟盘或实盘收益。
- 离线账户：初始 10,000 USDT、固定单边 5 bps 费用，显示累计已实现收益减全部手续费及当前未实现盈亏。
- OKX 账户：权益、可用余额与未实现盈亏来自账户接口；“已实现净收益”取最近最多 100 条成交的平仓盈亏 + USDT 手续费/返佣 + 最近最多 100 条资金费账单，窗口不同，**不是账户累计收益或存取款调整后的收益率**。接口保留期和返回记录不足时按实际可见窗口显示。离线、回测、真实账户口径在界面分别标注。

## 风控与订单生命周期

默认 `MAX_ORDER_USDT=100`、`MAX_POSITION_USDT=500`、`MAX_EQUITY_FRACTION=0.10`、`MAX_SLIPPAGE_BPS=20`，均从服务端读取。开仓预算为策略预算、单笔限额、账户全部永续仓位剩余限额和可用余额的最小值。其他永续持仓也占用额度；账户有挂单时暂停下单。退出必须为 `reduceOnly`，允许一次减少超过单笔开仓限额的已有仓位。

合约张数从 OKX `ctVal`、`lotSz`、`minSz` 计算并向下取整，价格按 `tickSz` 取整。使用在实时买一/卖一附近、受价格偏移上限约束的 IOC 限价单，避免无界市价成交。IOC 可能部分成交或取消；提交成功仅标为 `accepted`，后续通过查询订单获取实际状态。实际滑点相对信号价仍受信号到执行的时间差影响。

本地 SQLite 在请求发出前持久化 `reserved` 意图；`clOrdId` 由环境、数据源、API Key 指纹、合约、周期、K 线时间和方向确定，不随策略重建改变。唯一键 + 数据库写锁防止并发重复提交。

- 请求超时、断网或响应不完整 → `unknown`，暂停策略，**不自动重发**。
- 点击“订单对账”按 `clOrdId` 查询；未结束和未知订单阻止后续开仓。可对已确认挂单发出撤单并再次查单。
- 查无订单不等于未下单。若长时间查不到，保持停单，先在 OKX 核对历史订单和成交，再由维护者审计意图记录；界面不提供跳过未知订单的按钮。
- 重启服务后全部策略暂停，必须手动重新启动。切换账户/数据源需重新生成策略。
- “暂停全部策略”停止后续下单，可能等待当前已开始的请求结束；**不会自动平仓或撤销已有订单**。未知订单按上述流程处理，仓位应在 OKX 人工管理。
- `data/` 包含持仓账本、意图和审计记录，应持久化和备份。不要删除数据库或运行多个数据库实例连接同一账户；新数据库不能替代旧意图记录。`.env` 和 `data/` 均被 Git 忽略。

本机接口有 Host 校验、同源检查、JSON 写请求和 CSP，防止网页跨站调用。它不是多人访问控制系统。首版没有交易所端止损、日亏损熔断、WebSocket 推送、长历史分页研究和完整资金流水归因。

## 验证与开发

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
node --check okx_workbench/static/app.js
```

测试采用 HTTP Mock 与临时数据库，无需密钥，不发送真实订单。GitHub Actions 使用 Python 3.11 / 3.14。详细验证记录见 `docs/VALIDATION.md`。

主要接口：`GET /api/state`、`GET /api/market`、`GET /api/account`、`POST /api/strategies/generate`、`POST /api/backtests`、`GET /api/backtests/{id}/export.csv`、`POST /api/strategies/{id}/start`、`POST /api/strategies/{id}/tick`、`POST /api/orders/reconcile`、`POST /api/orders/{id}/cancel`、`POST /api/stop`。写请求均需 `Content-Type: application/json`；启动体为 `{"confirmation":"START_OFFLINE"}` / `START_DEMO` / `START_LIVE`。

实现依据：[OKX V5 官方接口文档](https://www.okx.com/docs-v5/en/)（签名、模拟盘、合约单位、订单状态与账户接口；2026-11-13 核对）。

## 一次性模拟联调脚本

`scripts/demo_smoke.py` 只使用模拟盘，不读取 `.env`，凭据从当前用户独占权限的 JSON 文件在进程内读取（字段 `api_key`、`secret_key`、`passphrase`），不复制文件、不输出凭据。默认仅预检；只有明确授权后传 `--execute-demo` 才会交易：

```bash
# 仅预检，不下单。将路径替换为自己的受限权限文件。
uv run python scripts/demo_smoke.py --credentials-file /secure/path/okx-demo.json
# 在用户明确授权后，一次性测试并清理。脚本不运行后台任务。
uv run python scripts/demo_smoke.py --credentials-file /secure/path/okx-demo.json --execute-demo --report data/demo-result.json
```

脚本固定上限 25 USDT，仅在无永续持仓、无挂单、单向持仓、认证和行情预检通过时继续。按真实已完成 K 线选择一组满足快均线大于慢均线的简单规则，经生产 `Engine.execute` 链路触发开仓；查询订单终态与持仓变化，再以 `reduceOnly` 清理本次可归属新增仓位，最终暂停。没有做多信号时停止，不伪造行情。报告只有脱敏的信号、合约、数量、成交状态、费用和清理结果。凭据类型/权限不符时停止，不切换实盘。
