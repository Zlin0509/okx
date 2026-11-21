# ZLIN-21 现货防守研究

离线研究程序使用 Python 3.11 以上版本的标准库，读取公开行情并生成训练、验证和合成压力报告。

## 离线复现

将审核收到的输入快照解包至当前目录。使用交付记录中登记的清单 SHA-256 校验快照并执行以下命令。输出目录需为新目录。

```bash
python3 -m research.spot_defense.run zlin21-market MANIFEST_SHA256 reproduced
python3 -m pytest tests/test_spot_defense.py -q
```

`summary.json` 包含覆盖、各成本档和现金及被动基准指标。各档 `*-ledgers.json` 记录逐日信号、成交、费用、陈旧估值和未完成退出。`stress.json` 保存合成路径与保护检查结果。

## 重新采集

```bash
python3 -m research.spot_defense.collect new-market-snapshot
```

采集使用 OKX 历史 `1Dutc` 与 `1H` 数据、Coinbase Exchange `USDT-USD` 日线和有限的 OKX 历史公告。清单登记请求、UTC 抓取时间及响应 SHA-256。重采会生成新清单，新清单需要单独核对。行情采集和快照读取均校验日期上界。复现命令固定输出训练及验证窗。

01:00 UTC 的信号只读取在检查时点可用的已结束日线。历史接口缺少首次发布时间，采集适配器将日线标记为 UTC 日界线可用。该假设与其他证据缺口随结果报告。02:00 UTC 模拟窗口要求已确认且有正成交量的小时线，使用开盘价作为首笔成交代理。双币首价在模型中合为同一估值截面。分钟内先后顺序、深度及模拟订单的实际可执行性需要额外证据。

[OKX 历史 K 线文档](https://www.okx.com/docs-v5/en/#order-book-trading-market-data-get-candlesticks-history) · [Coinbase Exchange 历史 K 线文档](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)
