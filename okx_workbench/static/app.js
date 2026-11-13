"use strict";
const $ = (id) => document.getElementById(id);
let state,
  selectedId = null,
  timer,
  marketData = [],
  curveData = [],
  curveBaseline = 10000,
  connected = false,
  lastSync = null,
  controlGeneration = 0,
  refreshGeneration = 0,
  lastSnapshot = null,
  lastInputFingerprint = null,
  recoveryOrderId = null,
  slipInitialized = false,
  confirmedPause = false;

const inputIds = [
  "symbol",
  "bar",
  "fast",
  "slow",
  "allocation",
  "capital",
  "fee",
  "slippage",
];
const fingerprint = () =>
  JSON.stringify(
    inputIds.map((id) =>
      ["symbol", "bar"].includes(id) ? $(id).value : Number($(id).value),
    ),
  );
function disconnected(note = "服务连接中断；执行状态待同步") {
  connected = false;
  $("connection").textContent = note;
  $("connection-notice").textContent =
    `${note}。最后同步：${lastSync ? date(lastSync) : "尚未成功"}。金额显示已清空；暂停入口仍可用。`;
  $("connection-notice").classList.add("warning");
  $("execution-state").textContent = confirmedPause
    ? "暂停已确认 / 待同步"
    : "状态待同步";
  $("execution-state").classList.remove("positive");
  $("execution-dot").classList.add("inactive");
  ["equity", "unrealized", "realized"].forEach((id) => {
    $(id).textContent = "—";
    $(id).classList.remove("positive", "negative");
  });
  $("account-note").textContent = "数据已过期，等待连接恢复";
  $("active-note").textContent = "无法确认后台状态，请勿重复启动";
  $("start").disabled = true;
  $("tick").disabled = true;
}
function markSnapshotStale() {
  const stale = lastSnapshot && fingerprint() !== lastInputFingerprint;
  $("backtest-stale").classList.toggle("hidden", !stale);
  if (lastSnapshot && !$("export").classList.contains("hidden"))
    $("export").textContent = stale ? "导出上次回测 CSV ↗" : "导出该快照 CSV ↗";
}
const statusText = {
  reserved: "待提交",
  submitting: "提交结果待确认",
  unknown: "结果未知，禁止重发",
  accepted: "已受理，尚未确认成交",
  live: "挂单中",
  partially_filled: "部分成交",
  filled: "已成交",
  canceled: "已撤销",
  canceled_local: "本地取消，未重发",
  rejected: "已拒绝",
  abandoned: "人工解除，保留去重",
};
const fmt = (x, digits = 2) =>
  Number(x).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
const date = (x) =>
  new Date(x).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
const sourceName = () =>
  state?.source === "offline"
    ? "离线演练"
    : state?.mode === "live"
      ? "OKX 实盘"
      : "OKX 模拟盘";
function notify(message, error = false) {
  $("toast").textContent = message;
  $("toast").className = "toast" + (error ? " error" : "");
  clearTimeout(timer);
  timer = setTimeout(() => $("toast").classList.add("hidden"), 6500);
}
async function api(path, body) {
  const res = await fetch(
    "/api" + path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const data = await res.json();
  if (!res.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "参数无效，请检查输入范围与均线顺序。",
    );
  return data;
}
async function busy(id, action) {
  const button = $(id),
    original = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    await action();
  } catch (e) {
    notify(e.message, true);
  } finally {
    button.textContent = original;
    button.disabled = false;
    if (state) renderSelection();
  }
}
function spec() {
  return {
    symbol: $("symbol").value,
    bar: $("bar").value,
    fast: Number($("fast").value),
    slow: Number($("slow").value),
    allocation: Number($("allocation").value) / 100,
  };
}
function signed(id, value) {
  $(id).textContent = (Number(value) > 0 ? "+" : "") + fmt(value);
  $(id).classList.toggle("positive", Number(value) >= 0);
  $(id).classList.toggle("negative", Number(value) < 0);
}
function chart(id, points, key, baseline) {
  const container = $(id);
  if (!points.length) return;
  const w = Math.max(300, container.clientWidth),
    h = container.clientHeight;
  const left = 8,
    right = 67,
    top = 14,
    bottom = 30,
    innerW = w - left - right,
    innerH = h - top - bottom;
  let min = Math.min(
    ...points.map((p) => Number(p[key])),
    baseline ?? Infinity,
  );
  let max = Math.max(
    ...points.map((p) => Number(p[key])),
    baseline ?? -Infinity,
  );
  const spread = Math.max((max - min) * 0.12, max * 0.0001, 0.01);
  min -= spread;
  max += spread;
  const x = (i) => left + (i / (points.length - 1 || 1)) * innerW;
  const y = (v) => top + ((max - v) / (max - min)) * innerH;
  const line = points
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${x(i).toFixed(2)},${y(Number(p[key])).toFixed(2)}`,
    )
    .join(" ");
  let grids = "";
  for (let i = 0; i < 4; i++) {
    const v = min + ((max - min) * i) / 3,
      yy = y(v);
    grids += `<line class="grid" x1="${left}" y1="${yy}" x2="${w - right + 3}" y2="${yy}"/><text x="${w - right + 10}" y="${yy + 3}">${fmt(v, v > 1000 ? 0 : 2)}</text>`;
  }
  let labels = "";
  for (const i of [0, Math.floor(points.length / 2), points.length - 1]) {
    labels += `<text x="${x(i)}" y="${h - 4}" text-anchor="${i === 0 ? "start" : i === points.length - 1 ? "end" : "middle"}">${new Date(points[i].ts).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })}</text>`;
  }
  const base =
    baseline === undefined
      ? ""
      : `<line class="baseline" x1="${left}" x2="${w - right}" y1="${y(baseline)}" y2="${y(baseline)}"/>`;
  // Only validated numeric series and fixed date formatting enter SVG markup.
  container.innerHTML = `<svg viewBox="0 0 ${w} ${h}" aria-hidden="true">${grids}<path class="area" d="${line} L${x(points.length - 1)},${h - bottom} L${left},${h - bottom} Z"/>${base}<path class="line" d="${line}"/>${labels}</svg>`;
}
async function loadMarket() {
  const data = await api(
    `/market?symbol=${encodeURIComponent($("symbol").value)}&bar=${encodeURIComponent($("bar").value)}`,
  );
  marketData = data.candles;
  $("market-heading").textContent = $("symbol").value.replace(
    "-USDT-SWAP",
    " / USDT 永续",
  );
  $("last-price").textContent = fmt(marketData.at(-1).close);
  $("market-source").textContent =
    data.source === "offline" ? "合成行情 · 非真实价格" : "OKX 已完成 K 线";
  $("market-range").textContent =
    `${date(marketData[0].ts)} — ${date(marketData.at(-1).ts)}`;
  chart("market-chart", marketData, "close");
}
async function loadAccount(generation = refreshGeneration) {
  try {
    const account = await api("/account");
    if (!connected || generation !== refreshGeneration) return;
    $("equity").textContent = fmt(account.equity);
    signed("unrealized", account.unrealized_pnl);
    $("account-note").textContent = `可用余额 ${fmt(account.available)} USDT`;
    if (account.source === "offline") {
      signed("realized", account.realized - account.fees);
      $("realized-note").textContent =
        `演练累计 · 手续费 ${fmt(account.fees)} USDT`;
    } else {
      signed("realized", account.recent_net_realized);
      $("realized-note").textContent =
        `最近成交 ${account.fills_count} 条 / 资金费 ${account.funding_count} 条，非累计`;
      $("realized-note").title = account.scope_note;
    }
  } catch (e) {
    if (!connected || generation !== refreshGeneration) return;
    $("equity").textContent = "—";
    $("unrealized").textContent = "—";
    $("realized").textContent = "—";
    $("account-note").textContent = e.message;
    $("realized-note").textContent = "账户收益暂不可用";
  }
}
function renderSelection() {
  const strategy = state.strategies.find((s) => s.id === selectedId);
  $("start").disabled =
    !connected ||
    !strategy ||
    Boolean(state.blockers?.length || state.startup_error);
  $("tick").disabled = !connected || !strategy?.active;
  if (!strategy) return;
  const { spec: s, validation: v } = strategy;
  $("selected-strategy").textContent =
    `已选 #${strategy.id} · ${s.symbol} · ${s.bar} · MA ${s.fast}/${s.slow} · 预算 ${s.allocation * 100}%`;
  $("strategy-result").className = "strategy-result";
  $("strategy-result").replaceChildren();
  const info = document.createElement("div"),
    strong = document.createElement("strong"),
    hint = document.createElement("small");
  strong.textContent = `MA ${s.fast} / ${s.slow} 趋势策略`;
  hint.textContent = `快均线上穿区间持多，回落至慢均线下方平仓 · ${strategy.source === "offline" ? "合成样本" : "OKX 样本"}`;
  info.append(strong, hint);
  const result = document.createElement("div");
  result.textContent = `样本外收益 ${fmt(v.return_pct)}% / 回撤 ${fmt(v.max_drawdown_pct)}%`;
  $("strategy-result").append(info, result);
  const age = strategy.last_signal;
  const budget = v.risk?.initial_effective_budget_usdt;
  if (budget !== undefined)
    hint.textContent += ` · 验证有效单笔预算 ${fmt(budget)} USDT；滑点假设 ${v.risk.slippage_bps} bps`;
  const signalNote = document.createElement("small");
  signalNote.textContent = age
    ? `上次检查：信号年龄 ${fmt(age.age_seconds, 0)} 秒 / 上限 ${age.max_age_seconds} 秒；${age.executable ? "当时处于窗口内" : "超时或时间超前，等待新 K 线，不补单"}`
    : `执行窗口：收盘后最多 ${state.risk.max_signal_age_seconds ?? 300} 秒；超时等待新 K 线。`;
  info.append(signalNote);
}
function renderOrders() {
  const rows = $("order-rows");
  rows.replaceChildren();
  if (!state.orders.length) {
    const row = rows.insertRow(),
      cell = row.insertCell();
    cell.colSpan = 7;
    cell.className = "empty";
    cell.textContent = "暂无订单。启动策略后，信号和订单将在这里记录。";
  }
  for (const order of state.orders) {
    const row = rows.insertRow();
    [
      date(order.created * 1000),
      order.symbol,
      order.side === "buy" ? "买入 / 开多" : "卖出 / 平多",
      order.quantity,
      fmt(order.price),
      `${statusText[order.state] || order.state} / 已成交 ${order.filled} 张`,
    ].forEach((value, i) => {
      const cell = row.insertCell();
      cell.textContent = value;
      if (i === 1) {
        const small = document.createElement("small");
        small.textContent = order.id;
        cell.append(small);
      }
      if (i === 3 || i === 4) cell.classList.add("number");
      if (i === 5) {
        cell.classList.add("order-status");
        const detail = document.createElement("small");
        detail.textContent = order.error || "";
        cell.append(detail);
      }
    });
    const action = row.insertCell();
    if (
      state.source === "okx" &&
      ["accepted", "live", "partially_filled"].includes(order.state)
    ) {
      const button = document.createElement("button");
      button.className = "button subtle";
      button.textContent = "撤单";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api(`/orders/${order.id}/cancel`, {});
          await refresh();
          notify("撤单请求已处理，策略已暂停");
        } catch (e) {
          notify(e.message, true);
        } finally {
          button.disabled = false;
        }
      });
      action.append(button);
    } else if (
      order.state === "unknown" &&
      order.not_found_count >= 3 &&
      order.last_error_code === "51603"
    ) {
      const button = document.createElement("button");
      button.className = "button subtle";
      button.textContent = "人工审计解除";
      button.addEventListener("click", () => {
        recoveryOrderId = order.id;
        $("recovery-description").textContent =
          `${order.symbol} / ${order.id}，连续查无订单 ${order.not_found_count} 次。最新查单和账户状态仍需服务端再次核对。`;
        $("recovery-form").reset();
        $("recovery-dialog").showModal();
      });
      action.append(button);
    } else
      action.textContent =
        order.state === "unknown"
          ? `查无订单 ${order.not_found_count || 0}/3；先对账核对`
          : "—";
  }
  $("events").replaceChildren();
  state.events.slice(0, 4).forEach((event) => {
    const node = document.createElement("div");
    node.className = "event " + event.level;
    node.textContent = `${date(event.created * 1000)}　${event.message}`;
    $("events").append(node);
  });
}
async function refresh() {
  const generation = ++refreshGeneration;
  let next;
  try {
    next = await api("/state");
  } catch (e) {
    if (generation === refreshGeneration) disconnected();
    throw e;
  }
  if (generation !== refreshGeneration) return;
  state = next;
  connected = true;
  lastSync = Date.now();
  confirmedPause = false;
  $("connection-notice").textContent =
    `已连接 · 执行状态同步于 ${date(lastSync)} · 本版仅支持模拟盘/离线，实盘已禁用。`;
  $("connection-notice").classList.remove("warning");
  $("connection").textContent = "本地服务已连接";
  $("environment").textContent = sourceName();
  $("environment").classList.toggle("live", state.mode === "live");
  $("source-notice").classList.toggle("live", state.mode === "live");
  $("source-notice").textContent =
    state.source === "offline"
      ? "离线演练已启用 · 行情为合成样本，订单仅记入本地账本。连接 OKX 模拟盘请在服务端配置凭据和数据源。"
      : state.mode === "live"
        ? "实盘环境 · 启动策略将使用真实资金。请核对账户、逐仓设置及仓位限额。"
        : "OKX 模拟盘 · 订单将发送至模拟账户。密钥仅由服务端读取。";
  $("risk-order").textContent = `${state.risk.max_order_usdt} USDT`;
  $("risk-position").textContent = `${state.risk.max_position_usdt} USDT`;
  $("risk-fraction").textContent =
    `${Number(state.risk.max_equity_fraction) * 100}%`;
  $("risk-slip").textContent = `${state.risk.slippage_bps} bps`;
  if (!slipInitialized) {
    $("slippage").value = state.risk.slippage_bps;
    slipInitialized = true;
  }
  const blockers =
    state.blockers ||
    state.orders.filter((o) =>
      [
        "unknown",
        "reserved",
        "submitting",
        "live",
        "accepted",
        "partially_filled",
      ].includes(o.state),
    );
  state.blockers = blockers;
  const messages = blockers.map(
    (o) =>
      `${o.symbol}：${statusText[o.state] || o.state}。${o.error || "请先订单对账"}；订单 ${o.id}，明确查无 ${o.not_found_count || 0}/3 次。`,
  );
  if (state.startup_error) messages.unshift(state.startup_error);
  $("blocker-notice").classList.toggle("hidden", !messages.length);
  $("blocker-notice").textContent = messages.length
    ? `执行已阻断：${messages.join(" ")} 先在 OKX 核对订单与成交。连续三次明确查无订单且无挂单/目标持仓后，可填写依据人工解除；原信号永久禁止重发。`
    : "";
  const active = state.strategies.find((s) => s.active);
  $("execution-state").textContent = active ? "运行中" : "已暂停";
  $("execution-state").classList.toggle("positive", !!active);
  $("execution-dot").classList.toggle("inactive", !active);
  $("active-note").textContent = active
    ? `策略 #${active.id} · 每 15 秒检查已完成 K 线`
    : blockers.length
      ? "异常或未结束订单阻断，先核对恢复"
      : "等待手动启动策略";
  if (!selectedId && state.strategies.length)
    selectedId = state.strategies[0].id;
  renderSelection();
  renderOrders();
  await loadAccount(generation);
}
function showBacktest(report, label) {
  lastSnapshot = report.snapshot;
  const snapshot = report.snapshot;
  lastInputFingerprint = snapshot
    ? JSON.stringify([
        snapshot.symbol,
        snapshot.bar,
        snapshot.fast,
        snapshot.slow,
        snapshot.allocation * 100,
        snapshot.initial_capital,
        snapshot.fee_bps,
        snapshot.slippage_bps,
      ])
    : null;
  const values = [
    fmt(report.return_pct) + "%",
    fmt(report.max_drawdown_pct) + "%",
    fmt(report.win_rate_pct) + "%",
    String(report.trade_count),
    fmt(report.fees),
  ];
  [...$("backtest-summary").querySelectorAll("strong")].forEach((node, i) => {
    node.textContent = values[i];
    node.classList.toggle("positive", i === 0 && report.return_pct >= 0);
    node.classList.toggle("negative", i === 0 && report.return_pct < 0);
  });
  curveData = report.curve;
  curveBaseline = report.initial_capital;
  chart("equity-chart", curveData, "equity", curveBaseline);
  $("backtest-note").textContent =
    `${label} · ${report.source || report.snapshot?.source || "未知数据源"} · ${report.assumptions}`;
  const s = report.snapshot;
  $("backtest-snapshot").textContent = s
    ? `结果快照 ${report.id ? "#" + report.id : "样本外验证"}：${s.symbol} / ${s.bar} / MA ${s.fast}/${s.slow} / 预算 ${s.allocation * 100}% / 资金 ${fmt(s.initial_capital)} / 费率 ${s.fee_bps} bps / 单边滑点假设 ${s.slippage_bps} bps；数据 ${date(s.data_start_ts)}—${date(s.data_end_ts)}，生成 ${date(s.generated_at_ms)}。有效初始单笔预算 ${fmt(report.risk.initial_effective_budget_usdt)} USDT（${fmt((report.risk.initial_effective_budget_usdt / s.initial_capital) * 100)}%），IOC 实时盘口偏移上限 ${report.risk.execution_price_offset_bps} bps。`
    : "该旧版结果没有完整参数快照，请重新回测。";
  markSnapshotStale();
}
$("strategy-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy("generate", async () => {
    const report = await api("/strategies/generate", spec());
    selectedId = report.id;
    $("fast").value = report.spec.fast;
    $("slow").value = report.spec.slow;
    await refresh();
    showBacktest(report.validation, `策略 #${report.id} · 后 30% 样本外验证`);
    $("export").classList.add("hidden");
    notify(`已生成策略 #${report.id}，尚未启动。${report.method}`);
  });
});
$("backtest").addEventListener("click", () =>
  busy("backtest", async () => {
    const report = await api("/backtests", {
      ...spec(),
      initial_capital: Number($("capital").value),
      fee_bps: Number($("fee").value),
      slippage_bps: Number($("slippage").value),
    });
    showBacktest(report, "已保存参数快照 · 全样本回测");
    $("export").href = `/api/backtests/${report.id}/export.csv`;
    $("export").classList.remove("hidden");
    markSnapshotStale();
    notify("回测完成，权益曲线可导出 CSV。");
  }),
);
$("start").addEventListener("click", async () => {
  if (!connected) return;
  const generation = controlGeneration;
  const s = state.strategies.find((s) => s.id === selectedId);
  let preview;
  try {
    preview = await api(`/strategies/${selectedId}/signal`);
  } catch (e) {
    notify(e.message, true);
    return;
  }
  if (generation !== controlGeneration || !connected) return;
  $("start-description").textContent =
    `策略 #${selectedId}：${s.spec.symbol} / ${s.spec.bar} / MA ${s.spec.fast}/${s.spec.slow}。环境：${sourceName()}。单笔上限 ${state.risk.max_order_usdt} USDT。信号年龄 ${fmt(preview.age_seconds, 0)} 秒 / 上限 ${preview.max_age_seconds} 秒；${preview.executable ? "处于有效窗口" : "已过期或时间超前，启动后等待新 K 线，不补单"}。`;
  $("live-confirm-label").classList.toggle("hidden", state.mode !== "live");
  $("live-confirm").value = "";
  $("start-dialog").returnValue = "";
  $("start-dialog").showModal();
});
let startPending = false;
$("confirm-start").addEventListener("click", () =>
  busy("confirm-start", async () => {
    const generation = ++controlGeneration;
    startPending = true;
    if (state.mode === "live" && $("live-confirm").value !== "START_LIVE")
      throw new Error("请输入 START_LIVE 确认真实资金交易");
    const confirmation =
      state.source === "offline"
        ? "START_OFFLINE"
        : "START_" + state.mode.toUpperCase();
    try {
      await api(`/strategies/${selectedId}/start`, {
        confirmation,
        control_epoch: state.control_epoch,
      });
      if (generation !== controlGeneration) return;
      $("start-dialog").close("started");
      if (!connected) return;
      const result = await api(`/strategies/${selectedId}/tick`, {});
      if (generation !== controlGeneration) return;
      notify(`策略已启动，本次信号：${result.action}`);
    } catch (e) {
      if (generation === controlGeneration) throw e;
    } finally {
      startPending = false;
      if (generation === controlGeneration) await refresh();
    }
  }),
);
$("tick").addEventListener("click", () =>
  busy("tick", async () => {
    try {
      const r = await api(`/strategies/${selectedId}/tick`, {});
      notify(`信号检查：${r.action}${r.state ? " / " + r.state : ""}`);
    } finally {
      await refresh();
    }
  }),
);
async function pauseAll() {
  ++controlGeneration;
  ++refreshGeneration;
  try {
    await api("/stop", {});
  } catch (e) {
    disconnected("暂停结果未确认；请检查服务或 OKX");
    throw e;
  }
  confirmedPause = true;
  if (state) state.strategies.forEach((s) => (s.active = false));
  $("execution-state").textContent = "已暂停";
  $("execution-state").classList.remove("positive");
  $("execution-dot").classList.add("inactive");
  renderSelection();
  try {
    await refresh();
    notify("暂停已确认。已有仓位和挂单需另行处理。");
  } catch {
    confirmedPause = true;
    disconnected("暂停已确认，数据刷新失败");
    notify("暂停已确认，数据刷新失败；请恢复连接后核对。", true);
  }
}
$("stop").addEventListener("click", () => busy("stop", pauseAll));
$("start-dialog").addEventListener("close", () => {
  if (startPending && $("start-dialog").returnValue !== "started")
    busy("stop", pauseAll);
});
$("reconcile").addEventListener("click", () =>
  busy("reconcile", async () => {
    const result = await api("/orders/reconcile", {});
    await refresh();
    notify(
      `策略已暂停；对账处理 ${result.orders.length} 笔订单，未确认结果请按阻断提示核对。`,
    );
  }),
);
$("refresh").addEventListener("click", () =>
  busy("refresh", async () => {
    await Promise.all([refresh(), loadMarket()]);
    notify("数据已刷新");
  }),
);
["symbol", "bar"].forEach((id) =>
  $(id).addEventListener("change", () =>
    loadMarket().catch((e) => notify(e.message, true)),
  ),
);
inputIds.forEach((id) => {
  ["input", "change"].forEach((event) =>
    $(id).addEventListener(event, markSnapshotStale),
  );
});
$("cancel-recovery").addEventListener("click", () =>
  $("recovery-dialog").close(),
);
$("recovery-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy("confirm-recovery", async () => {
    await api(`/orders/${recoveryOrderId}/abandon`, {
      operator: $("recovery-operator").value,
      reason: $("recovery-reason").value,
      confirmation: $("recovery-confirm").value,
    });
    $("recovery-dialog").close();
    await refresh();
    notify("已人工解除并保留审计，原信号未重发，策略仍暂停。");
  });
});
document.querySelectorAll("nav a").forEach((link) =>
  link.addEventListener("click", () => {
    document
      .querySelectorAll("nav a")
      .forEach((a) => a.classList.remove("selected"));
    link.classList.add("selected");
  }),
);
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    chart("market-chart", marketData, "close");
    chart("equity-chart", curveData, "equity", curveBaseline);
  }, 100);
});
Promise.all([refresh(), loadMarket()]).catch((e) => {
  $("connection").textContent = "连接失败";
  notify(e.message, true);
});
setInterval(() => {
  if (!document.hidden)
    refresh().catch((e) => {
      $("connection").textContent = "服务连接中断";
      notify(e.message, true);
    });
}, 15000);
