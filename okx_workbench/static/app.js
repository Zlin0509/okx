"use strict";
const $ = (id) => document.getElementById(id);
let state,
  selectedId = null,
  timer,
  marketData = [],
  curveData = [],
  curveBaseline = 10000;
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
    if (state && ["tick", "start"].includes(id)) renderSelection();
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
async function loadAccount() {
  try {
    const account = await api("/account");
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
    $("equity").textContent = "—";
    $("unrealized").textContent = "—";
    $("realized").textContent = "—";
    $("account-note").textContent = e.message;
    $("realized-note").textContent = "账户收益暂不可用";
  }
}
function renderSelection() {
  const strategy = state.strategies.find((s) => s.id === selectedId);
  $("start").disabled = !strategy;
  $("tick").disabled = !strategy?.active;
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
      `${order.state} / ${order.filled}`,
    ].forEach((value, i) => {
      const cell = row.insertCell();
      cell.textContent = value;
      if (i === 1) {
        const small = document.createElement("small");
        small.textContent = order.id;
        cell.append(small);
      }
      if (i === 3 || i === 4) cell.classList.add("number");
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
    } else action.textContent = "—";
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
  state = await api("/state");
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
  const active = state.strategies.find((s) => s.active);
  $("execution-state").textContent = active ? "运行中" : "已暂停";
  $("execution-state").classList.toggle("positive", !!active);
  $("active-note").textContent = active
    ? `策略 #${active.id} · 每 15 秒检查已完成 K 线`
    : "等待手动启动策略";
  if (!selectedId && state.strategies.length)
    selectedId = state.strategies[0].id;
  renderSelection();
  renderOrders();
  await loadAccount();
}
function showBacktest(report, label) {
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
    `${label} · ${sourceName()} · ${report.assumptions}`;
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
    showBacktest(report, "当前表单参数 · 全样本回测");
    $("export").href = `/api/backtests/${report.id}/export.csv`;
    $("export").classList.remove("hidden");
    notify("回测完成，权益曲线可导出 CSV。");
  }),
);
$("start").addEventListener("click", () => {
  const s = state.strategies.find((s) => s.id === selectedId);
  $("start-description").textContent =
    `策略 #${selectedId}：${s.spec.symbol} / ${s.spec.bar} / MA ${s.spec.fast}/${s.spec.slow}。环境：${sourceName()}。启动后每 15 秒检查信号并自动下单，单笔上限 ${state.risk.max_order_usdt} USDT。`;
  $("live-confirm-label").classList.toggle("hidden", state.mode !== "live");
  $("live-confirm").value = "";
  $("start-dialog").showModal();
});
$("confirm-start").addEventListener("click", () =>
  busy("confirm-start", async () => {
    if (state.mode === "live" && $("live-confirm").value !== "START_LIVE")
      throw new Error("请输入 START_LIVE 确认真实资金交易");
    const confirmation =
      state.source === "offline"
        ? "START_OFFLINE"
        : "START_" + state.mode.toUpperCase();
    await api(`/strategies/${selectedId}/start`, { confirmation });
    $("start-dialog").close();
    try {
      const result = await api(`/strategies/${selectedId}/tick`, {});
      notify(`策略已启动，本次信号：${result.action}`);
    } finally {
      await refresh();
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
$("stop").addEventListener("click", () =>
  busy("stop", async () => {
    await api("/stop", {});
    await refresh();
    notify("全部策略已暂停。已有仓位和挂单需在 OKX 单独处理。");
  }),
);
$("reconcile").addEventListener("click", () =>
  busy("reconcile", async () => {
    const result = await api("/orders/reconcile", {});
    await refresh();
    notify(`对账完成，更新 ${result.orders.length} 笔订单。`);
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
