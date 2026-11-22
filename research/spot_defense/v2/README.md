# ZLIN-21 v2 离线候选

v2 沿用 `22a690c367cc2465251fff252d088e1ec17734b8` 的日线信号、成本、目标、风险阈值和六项合成压力路径。

| 区间 | UTC，含起点、排除终点 | 用途 |
| --- | --- | --- |
| 原样本外 | 2025-07-01 至 2026-11-01 | 永久封存 |
| 预热 | 2026-12-03 至 2027-02-01 | 日线信号预热 |
| 评估 | 2027-02-01 至 2028-02-01 | 经独立批准后一次性离线评估 |

行情日期按日线桶开始时刻及交易所成交时刻校验。**预热与评估共 425 天逐日存证。** 自 2026-12-03 起，每个 UTC 日的完整日线均须在其收盘后、次日 01:00 之前收到。预热结束后一次补抓 60 天不能证明可用性。最后一个评估日的收盘日线仍须在 2028-02-01 01:00 之前收到。

## 证据清单与原档比对

公开 `manifest.json` 包含 `schema=zlin21-v2-evidence-2`、`profile`、`bounds`、`source_manifest_sha256`、`files`（每个文件的 `bytes`、`records`、`sha256`）、`missing_counts`（`daily`、`check_state`、`execution_state`、`trade_feed`、`first_trade`）和 `conflict_count`。清单 SHA-256 由独立渠道登记。

受控复核接收托管方的原始清单元数据文件。该文件包含 `schema=zlin21-v2-custody-1`、同一 `profile` 和 `bounds`、原始回执列表 `receipts`。逐字节计算其 SHA-256，与已签归一化清单引用的哈希比对后再读归一化载荷。回执及归一化文件字段如下。

| 文件 | 每行字段 |
| --- | --- |
| `availability.json` | `symbol day source received_at response_sha256 candle`，保留 OKX `1Dutc` 或 Coinbase 86400 秒日线原始行。 |
| `trades.json` | `symbol day source received_at stream response_sha256 frame`，保留 OKX `trades-all` 单笔原始消息字段。 |
| `status.json` | `kind day symbol source start end state received_at response_sha256`。`check` 为在检查前收到的状态，`state` 为有效状态区间，`feed` 为有连续性记录的采集区间，流 ID 记在 `state`。 |
| `provenance.json` 及原始清单的 `receipts` | `kind symbol source received_at response_sha256`。归一化回执必须出现在原始清单中，每条记录按五字段绑定回执。 |

日期为 YYYY-MM-DD，时间戳为以 `Z` 结束的 UTC，允许微秒精度。OKX 和 Coinbase 日线来源标签分别为 `okx:1Dutc`、`coinbase-exchange:86400`，OKX 逐笔与流标签为 `okx:trades-all`，OKX 和 Coinbase 状态标签分别为 `okx:instrument+system+announcements`、`coinbase-exchange:product`。

- `check` 的 `start` 必须等于原始回执接收时刻，严格早于该日 01:00，`end` 为 01:00。已知会影响执行窗的停牌或下架应反映在检查状态里。
- `state` 与 `feed` 的原始回执接收时刻必须在同日且不晚于所证明区间的起点。事后收到的回执不能向前弥补空档。只有原始状态及流日志支持的区间才可进入归一化。事前一次快照或本地没有断线报告，都不足以证明整个区间连续。
- 补充预登记须把下列比对列为最终揭盲签批前的必检项。隔离批准后，独立审核人逐项核对托管方原始字节、UTC 接收时钟、状态公告、连接和连续性记录、原始清单哈希以及归一化回执。原始回执文件的内容语义与完整归一化过程由独立审核确认。公开清单只提供覆盖和哈希。 [OKX 逐笔成交与标的状态文档](https://tr.okx.com/docs-v5/en/)

## 因果执行和覆盖

- 逐笔的交易所时刻落在已证实的不可交易区间时登记冲突，即使收到时已复牌或该笔并非当日首笔。冲突和证据缺口分别计数，两者均阻断评估。完整证实的停牌区间可没有成交，单独披露。
- 每标的只考虑 02:00–03:00 内可证明的首笔。仅同一交易时点与接收时点的两笔可共用估值。接收时钟上较晚的首价不能改变较早成交。无充分状态或成交流证明时不成交。另一标的已有仓位且未见其当窗首价时阻断新增敞口。
- 被动组合为两腿各预留 50%。执行要求及时的 FX 收盘、01:00 前证实的 FX 在线状态及截至接收首笔的连续 FX 状态。单腿成交则保留账本并判基准证据不足。
- 零持仓腿市值为零，不影响敞口测量。逐日及逐事件账本披露持仓腿的陈旧估值。陈旧价格显示疑似越限时判证据不足。日末按次日 01:00 前收到的当日收盘价估值。

## 受控启动和签批

生产部署尚未授权。研究环境可运行公开清单预检。实际授权前，直接复核及评估默认拒绝。候选没有行情采集或解密入口。

`commands.json` 固定命令模板。补充预登记要记录启动器、命令文件、实现（含该证据说明）、授权 JSON、签批公钥、验签程序绝对路径与版本及哈希、解释器哈希、研究与评估 UID、一次性状态目录。两个签批均包含授权 JSON 的逐字节 SHA-256 及 `profile`。更换上述任何绑定都需要独立复核和新签批。

| 生产绑定 | 配置 |
| --- | --- |
| 受控安装 | `/opt/zlin21/v2`，根用户所有且无组或其他用户写权限，独立账户运行 `commands.json` 的隔离命令。 |
| 信任根 | 根用户所有的 `/etc/zlin21/v2/authority.json` 和 `authority.sha256`，后者为前者原始字节的 SHA-256。 |
| 解释器及验签 | 固定 `/usr/bin/python3 -I -B` 与 `/usr/bin/openssl`。哈希和 OpenSSL 版本经配置核对，验签进程使用固定环境。 |
| 一次性标记 | `/var/lib/zlin21/v2`，其父级由根用户控制，目录由独立评估 UID 持有且为 0700。研究 UID 必须与评估 UID 分离。研究账户无需也不得取得托管明文或评估写权限。 |

授权 JSON 必填字段为 `schema profile reviewer_id reviewer_public_key reviewer_key_sha256 revision implementation_sha256 commands_sha256 launcher_sha256 verifier_path verifier_sha256 verifier_version python_path python_sha256 state_directory state_uid research_uid synthetic_now`。生产 `profile=production`、`synthetic_now=null`。模拟模式使用 `profile=synthetic` 的独立信任根、测试时钟与目录，只与同类数据清单和签批兼容。

隔离签批绑定清单、登记、日志和候选版本。独立证据复核输出覆盖或冲突报告。只有同一授权下、绑定该报告的最终签批可运行评估。评估独占创建并同步一次性标记，失败时也保留。两段行情继续封存，实际生产 UID、正式公钥和受控主机由授权后补充预登记确定。

```bash
python3 -m research.spot_defense.v2.run inspect MANIFEST MANIFEST_SHA256
python3 -m pytest tests/test_spot_defense.py tests/test_spot_defense_v2.py -q
```
