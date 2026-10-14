const root = window.NIFTY_ROOT || "";
const api = root + "/api";

const money = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});
const points = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });

function text(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

let wakeLock = null;
let lastOkAt = 0;

function setAlive(ok, detail) {
  const el = document.getElementById("aliveLine");
  if (!el) return;
  el.className = ok ? "alive ok" : "alive bad";
  el.textContent = detail;
}

async function requestWakeLock() {
  if (!("wakeLock" in navigator)) {
    text("reasonLine", "This phone browser cannot keep the screen on. The PC server still trades.");
    return false;
  }
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => {
      const btn = document.getElementById("wakeBtn");
      if (btn) {
        btn.classList.remove("on");
        btn.textContent = "Keep screen on";
      }
    });
    return true;
  } catch (error) {
    text("reasonLine", error.message || "Could not keep the screen on");
    return false;
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && wakeLock === null) {
    const btn = document.getElementById("wakeBtn");
    if (btn && btn.classList.contains("on")) {
      requestWakeLock();
    }
  }
});

function stat(label, value, cls) {
  return `<div class="stat ${cls || ""}"><span>${label}</span><strong>${value}</strong></div>`;
}

function show(value) {
  if (value === null || value === undefined || value === "") return "—";
  return points.format(value);
}

async function post(path, body) {
  const response = await fetch(api + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : "{}",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Request failed");
  }
  return data;
}

function render(data) {
  const neo = data.neo || {};
  const market = data.market || {};
  const neoBits = [
    neo.has_access_token ? "Access token set" : "Access token missing",
    neo.connected
      ? neo.session_restored
        ? "Session restored for today"
        : "Session connected"
      : "Session not connected — connect once this morning",
  ];
  if (neo.session_saved_at) {
    neoBits.push(`saved ${String(neo.session_saved_at).slice(11, 16)} IST`);
  }
  text("neoLine", neoBits.join(" · "));
  const sessionBtn = document.getElementById("connectBtn");
  if (sessionBtn) {
    if (neo.connected) {
      sessionBtn.textContent = "Disconnect";
      sessionBtn.className = "danger";
      sessionBtn.dataset.mode = "disconnect";
    } else {
      sessionBtn.textContent = "Connect";
      sessionBtn.className = "primary";
      sessionBtn.dataset.mode = "connect";
    }
  }
  const badge = document.getElementById("autoBadge");
  const startBtn = document.getElementById("startBtn");
  const stopBtn = document.getElementById("stopBtn");
  if (data.enabled) {
    badge.textContent = "ON";
    badge.className = "auto-badge on";
    startBtn.disabled = true;
    startBtn.textContent = "Resume today";
    stopBtn.disabled = false;
    text(
      "runLine",
      "Auto is ON for market hours. Waiting for a clean setup — holding is normal until then."
    );
  } else if (data.paused_today) {
    badge.textContent = "PAUSED";
    badge.className = "auto-badge off";
    startBtn.disabled = false;
    startBtn.textContent = "Resume today";
    stopBtn.disabled = true;
    text("runLine", "Paused for today. Press Resume today if you want it watching again.");
  } else if (data.market_session) {
    badge.textContent = "ON";
    badge.className = "auto-badge on";
    startBtn.disabled = true;
    stopBtn.disabled = false;
    text("runLine", "Market is open — paper auto should be watching.");
  } else {
    badge.textContent = "OFF";
    badge.className = "auto-badge off";
    startBtn.disabled = true;
    startBtn.textContent = "Resume today";
    stopBtn.disabled = true;
    text(
      "runLine",
      "Outside market hours. Paper auto turns on by itself at 09:15 IST."
    );
  }
  text(
    "reasonLine",
    data.last_error || market.reason_text || "No reading yet. Refresh during or after a session."
  );

  const bias = market.bias || "mixed";
  document.getElementById("biasGrid").innerHTML = [
    stat("Nifty", show(market.ltp), bias === "call" ? "call" : bias === "put" ? "put" : ""),
    stat("Bias", bias, bias === "call" ? "call" : bias === "put" ? "put" : ""),
    stat("Chart", market.instrument || "—"),
    stat("Bar", market.bar_ts ? market.bar_ts.slice(11, 16) : "—"),
    stat("VWAP", show(market.vwap)),
    stat("EMA 9 / 21", market.ema9 ? `${show(market.ema9)} / ${show(market.ema21)}` : "—"),
    stat("15m EMA", market.ema9_15 ? `${show(market.ema9_15)} / ${show(market.ema21_15)}` : "—"),
    stat("ADX 15m", show(market.adx15)),
    stat("ATR 5m", show(market.atr5)),
    stat("Supertrend", market.supertrend || "—"),
    stat("Open range", market.orb_low ? `${show(market.orb_low)} – ${show(market.orb_high)}` : "—"),
    stat("CPR", market.cpr_bottom ? `${show(market.cpr_bottom)} – ${show(market.cpr_top)}` : "—"),
  ].join("");

  const position = data.position;
  const box = document.getElementById("position");
  if (!position) {
    box.className = "empty";
    box.textContent = "No open Nifty option.";
  } else {
    const pnl = position.pnl;
    const pnlClass = pnl > 0 ? "pnl-up" : pnl < 0 ? "pnl-down" : "";
    box.className = "position";
    box.innerHTML = `
      <strong>${position.trd_symbol}</strong>
      <span>Buy ${points.format(position.buy_premium)} · debit ${money.format(position.debit)} · lot ${position.lot_size}</span>
      <span>Now ${position.ltp == null ? "—" : points.format(position.ltp)}
        · P&L <span class="${pnlClass}">${pnl == null ? "—" : money.format(pnl)}</span></span>
      <span>Nifty target ${show(position.index_target)} (${show(position.reward_ratio)}R)
        · index stop ${show(position.index_stop)}</span>
      <span>Premium trail ${position.premium_stop == null ? "—" : points.format(position.premium_stop)}
        · peak ${position.peak_premium == null ? "—" : points.format(position.peak_premium)}${
          position.trail_stage >= 2
            ? " · +1% locked"
            : position.trail_stage >= 1
              ? " · breakeven locked"
              : ""
        }</span>
      <span>Regime ${position.market_regime || "—"} · risk distance ${show(position.risk_distance)} Nifty points</span>
      <span>${position.entry_reason || ""}</span>
    `;
  }

  const score = data.score || {};
  const rate = score.win_rate == null ? "—" : `${Math.round(score.win_rate * 100)}%`;
  document.getElementById("score").innerHTML = [
    stat("Closed trades", score.trades ?? 0),
    stat("Win rate", rate),
    stat("Net P&L", money.format(score.net_pnl || 0), (score.net_pnl || 0) >= 0 ? "call" : "put"),
    stat("Budget", money.format(data.max_budget || 20000)),
  ].join("");

  const trades = data.trades || [];
  document.getElementById("trades").innerHTML = trades.length
    ? trades.map((trade) => `
        <tr>
          <td>${(trade.opened_at || "").replace("T", " ").slice(0, 16)}</td>
          <td>${trade.trd_symbol}</td>
          <td>${points.format(trade.buy_premium)}</td>
          <td>${trade.sell_premium == null ? "—" : points.format(trade.sell_premium)}</td>
          <td class="${trade.pnl > 0 ? "pnl-up" : trade.pnl < 0 ? "pnl-down" : ""}">${trade.pnl == null ? "open" : money.format(trade.pnl)}</td>
          <td>${trade.exit_reason || trade.status}</td>
        </tr>
      `).join("")
    : `<tr><td colspan="6" class="empty">No paper trades yet.</td></tr>`;

  const signals = data.signals || [];
  document.getElementById("signals").innerHTML = signals.length
    ? signals.map((row) => `<li><time>${(row.created_at || "").slice(11, 16)}</time>${row.reason_text || row.reason}</li>`).join("")
    : `<li class="empty">No decisions yet.</li>`;

  lastOkAt = Date.now();
  const auto = data.enabled ? "auto ON" : data.paused_today ? "paused" : "auto waiting";
  setAlive(true, `Server live · ${auto} · trades run on PC, not on this phone`);
}

async function refresh() {
  try {
    const data = await post("/refresh");
    render(data);
  } catch (error) {
    setAlive(false, "Server unreachable — paper trading on the PC may have stopped");
    throw error;
  }
}

document.getElementById("connectBtn").addEventListener("click", async () => {
  const btn = document.getElementById("connectBtn");
  if (btn.dataset.mode === "disconnect") {
    text("neoLine", "Disconnecting…");
    try {
      await post("/disconnect");
      await refresh();
    } catch (error) {
      text("neoLine", error.message);
    }
    return;
  }
  text("neoLine", "Connecting…");
  try {
    await post("/connect", {
      totp: document.getElementById("totp").value.trim(),
      mpin: document.getElementById("mpin").value.trim(),
    });
    document.getElementById("mpin").value = "";
    await refresh();
  } catch (error) {
    text("neoLine", error.message);
  }
});

document.getElementById("startBtn").addEventListener("click", async () => {
  text("runLine", "Starting…");
  try {
    render(await post("/start"));
  } catch (error) {
    text("reasonLine", error.message);
  }
});

document.getElementById("stopBtn").addEventListener("click", async () => {
  text("runLine", "Stopping…");
  try {
    render(await post("/stop"));
  } catch (error) {
    text("reasonLine", error.message);
  }
});

document.getElementById("refreshBtn").addEventListener("click", () => {
  refresh().catch((error) => text("reasonLine", error.message));
});

const wakeBtn = document.getElementById("wakeBtn");
if (wakeBtn) {
  wakeBtn.addEventListener("click", async () => {
    if (wakeBtn.classList.contains("on") && wakeLock) {
      await wakeLock.release();
      wakeLock = null;
      wakeBtn.classList.remove("on");
      wakeBtn.textContent = "Keep screen on";
      return;
    }
    if (await requestWakeLock()) {
      wakeBtn.classList.add("on");
      wakeBtn.textContent = "Screen locked on";
    }
  });
}

refresh().catch((error) => text("reasonLine", error.message));
setInterval(() => {
  refresh().catch(() => {});
}, 20000);
setInterval(() => {
  if (lastOkAt && Date.now() - lastOkAt > 45000) {
    setAlive(false, "No server update — check that the PC app is still running");
  }
}, 15000);
