const root = window.OI_ROOT || "";
const $ = (id) => document.getElementById(id);

let busy = false;
let latest = null;

function money(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return sign + "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function plainMoney(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return "₹" + Number(value).toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function num(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-IN", { maximumFractionDigits: digits });
}

function pct(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = Number(value);
  return (n > 0 ? "+" : "") + n.toFixed(2) + "%";
}

async function api(path, options) {
  const resp = await fetch(root + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = data.detail || data.message || resp.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function actionButtons() {
  return [$("connectBtn"), $("autoBtn"), $("paperTodayBtn"), $("refreshBtn")].filter(Boolean);
}

function setBusy(active, activeBtn, workingLabel) {
  busy = active;
  actionButtons().forEach((btn) => {
    if (active) {
      btn.disabled = true;
      if (btn === activeBtn) {
        btn.dataset.prevLabel = btn.textContent;
        btn.textContent = workingLabel;
        btn.classList.add("busy");
      }
    } else {
      btn.classList.remove("busy");
      if (btn.dataset.prevLabel) {
        btn.textContent = btn.dataset.prevLabel;
        delete btn.dataset.prevLabel;
      }
    }
  });
  if (!active && latest) {
    applyButtonState(latest);
  }
}

async function runAction(btn, workingLabel, work) {
  if (busy || btn.disabled) return;
  setBusy(true, btn, workingLabel);
  try {
    const data = await work();
    if (data) render(data);
    else await refreshStatus();
  } catch (err) {
    $("errorLine").hidden = false;
    $("errorLine").textContent = err.message;
    $("runLine").textContent = err.message;
  } finally {
    setBusy(false, btn);
  }
}

function applyButtonState(data) {
  const neo = data.neo || {};
  const sessionBtn = $("connectBtn");
  if (neo.connected) {
    sessionBtn.textContent = "Disconnect";
    sessionBtn.className = "danger";
    sessionBtn.dataset.mode = "disconnect";
  } else {
    sessionBtn.textContent = "Connect";
    sessionBtn.className = "primary";
    sessionBtn.dataset.mode = "connect";
  }
  sessionBtn.disabled = busy;

  const autoBtn = $("autoBtn");
  const openCount = (data.score && data.score.open) || 0;
  if (data.paused_today) {
    autoBtn.textContent = "Resume auto";
    autoBtn.className = "primary";
    autoBtn.dataset.mode = "resume";
    autoBtn.disabled = busy;
    $("helpLine").textContent =
      "Auto is paused for today. Resume turns paper auto back on. It does not invent new buys after 09:24 — it only manages the day again and will auto-buy tomorrow at 09:20.";
  } else {
    autoBtn.textContent = openCount > 0 ? "Pause & flatten" : "Pause today";
    autoBtn.className = "danger";
    autoBtn.dataset.mode = "pause";
    autoBtn.disabled = busy;
    $("helpLine").textContent =
      openCount > 0
        ? "Auto is ON and managing your open paper options. Pause & flatten sells those open options now and stops auto for the rest of today. Resume is only needed after a pause."
        : "Auto is ON. At 09:20 it can paper-buy up to 2 names. Pause today stops auto; if anything is open it also sells those paper options.";
  }

  const paperBtn = $("paperTodayBtn");
  const tradesToday = data.trades_today || 0;
  const maxEntries = data.max_entries || 2;
  if (!data.can_paper_today) {
    paperBtn.textContent = tradesToday > 0 ? "Redo today’s entries" : "Paper today’s entries";
    paperBtn.disabled = true;
    paperBtn.className = "is-disabled";
    paperBtn.title = data.paused_today
      ? "Resume auto first, then try again."
      : "Only during market hours.";
  } else {
    paperBtn.textContent = tradesToday > 0 ? "Redo today’s entries" : "Paper today’s entries";
    paperBtn.disabled = busy;
    paperBtn.className = busy ? "is-disabled" : "primary";
    paperBtn.title =
      tradesToday > 0
        ? `Closes the current ${tradesToday} paper trade(s), then rechecks the 09:20 candle/TA and buys up to ${maxEntries} again. Tomorrow stays normal auto.`
        : `Checks today’s 09:20 setup with TA and paper-buys up to ${maxEntries}.`;
  }

  $("refreshBtn").disabled = busy;
  if (busy) $("refreshBtn").classList.add("is-disabled");
  else $("refreshBtn").classList.remove("is-disabled");
}

function bandTable(rows, matches) {
  const hit = new Set((matches || []).map((row) => row.symbol));
  if (!rows || !rows.length) return "<p class='empty'>None yet.</p>";
  const body = rows
    .map((row) => {
      const mark = hit.has(row.symbol) ? "<span class='match'>OI</span>" : "";
      const cls = row.pct > 0 ? "up" : "down";
      return `<tr><td>${row.symbol} ${mark}</td><td class="${cls}">${pct(row.pct)}</td><td>${num(row.ltp)}</td></tr>`;
    })
    .join("");
  return `<table><thead><tr><th>Symbol</th><th>Change</th><th>LTP</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderCandidates(rows, picked) {
  const pickedSet = new Set((picked || []).map((row) => row.symbol));
  if (!rows || !rows.length) {
    $("candidates").innerHTML = "<p class='empty'>No TA-passed names yet. They appear after a scan or Paper today’s entries.</p>";
    return;
  }
  const body = rows
    .map((row) => {
      const mark = pickedSet.has(row.symbol) ? "<span class='match'>PICK</span>" : "";
      const vol = row.vol_ratio == null ? "—" : Number(row.vol_ratio).toFixed(2) + "x";
      return `<tr>
        <td>${row.symbol} ${row.option_type} ${mark}</td>
        <td>${num(row.score, 1)}</td>
        <td>${num(row.rsi14, 1)}</td>
        <td>${num(row.ema9)} / ${num(row.ema21)}</td>
        <td>${row.body == null ? "—" : Math.round(Number(row.body) * 100) + "%"}</td>
        <td>${vol}</td>
      </tr>`;
    })
    .join("");
  $("candidates").innerHTML = `<table><thead><tr><th>Name</th><th>Score</th><th>RSI</th><th>EMA9/21</th><th>Body</th><th>Vol</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderPositions(rows) {
  if (!rows || !rows.length) {
    $("positions").innerHTML = "<p class='empty'>No open option.</p>";
    return;
  }
  $("positions").innerHTML = rows
    .map((row) => {
      const pnlClass = row.unrealized > 0 ? "pos" : row.unrealized < 0 ? "neg" : "";
      const roiClass = row.roi_pct > 0 ? "pos" : row.roi_pct < 0 ? "neg" : "";
      return `
        <article class="pos-card">
          <h3>${row.symbol} ${num(row.strike, 2)} ${row.option_type} · ${row.expiry}</h3>
          <div class="grid">
            <div class="stat"><span>Debit</span><strong>${plainMoney(row.debit)}</strong></div>
            <div class="stat"><span>Buy / now</span><strong>${num(row.buy_premium)} → ${num(row.option_ltp)}</strong></div>
            <div class="stat"><span>Open P&amp;L</span><strong class="${pnlClass}">${money(row.unrealized)}</strong></div>
            <div class="stat"><span>ROI</span><strong class="${roiClass}">${pct(row.roi_pct)}</strong></div>
            <div class="stat"><span>Lot</span><strong>${row.lot_size}</strong></div>
            <div class="stat"><span>Stock</span><strong>${num(row.stock_entry)} → ${num(row.stock_ltp)}</strong></div>
            <div class="stat"><span>Stop</span><strong>${num(row.stop_price)}</strong></div>
            <div class="stat"><span>Next target</span><strong>${num(row.target_price)}</strong></div>
            <div class="stat"><span>Trail / R</span><strong>${row.trail_stage} · ${row.r_multiple == null ? "—" : Number(row.r_multiple).toFixed(2) + "R"}</strong></div>
          </div>
        </article>`;
    })
    .join("");
}

function renderSignals(rows) {
  if (!rows || !rows.length) {
    $("signals").innerHTML = "<p class='empty'>No notes yet.</p>";
    return;
  }
  const body = rows
    .map((row) => `<tr><td>${(row.created_at || "").slice(11, 19)}</td><td>${row.symbol || "—"}</td><td>${row.action}</td><td>${row.detail || row.reason}</td></tr>`)
    .join("");
  $("signals").innerHTML = `<table><thead><tr><th>Time</th><th>Symbol</th><th></th><th>Note</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderTrades(rows) {
  const closed = (rows || []).filter((row) => row.status !== "open");
  if (!closed.length) {
    $("trades").innerHTML = "<p class='empty'>No closed paper trades yet.</p>";
    return;
  }
  const body = closed
    .map((row) => {
      const cls = row.pnl > 0 ? "pos" : row.pnl < 0 ? "neg" : "";
      const roi = row.debit > 0 && row.pnl != null ? (Number(row.pnl) / Number(row.debit)) * 100 : null;
      const roiClass = roi > 0 ? "pos" : roi < 0 ? "neg" : "";
      return `<tr>
        <td>${row.trade_date}</td>
        <td>${row.symbol} ${num(row.strike, 2)} ${row.option_type}</td>
        <td>${plainMoney(row.debit)}</td>
        <td class="${cls}">${money(row.pnl)}</td>
        <td class="${roiClass}">${pct(roi)}</td>
        <td>${row.exit_reason || ""}</td>
      </tr>`;
    })
    .join("");
  $("trades").innerHTML = `<table><thead><tr><th>Date</th><th>Contract</th><th>Spent</th><th>P&amp;L</th><th>ROI</th><th>Exit</th></tr></thead><tbody>${body}</tbody></table>`;
}

function render(data) {
  latest = data;
  const neo = data.neo || {};
  const session = neo.connected ? "Kotak session is on" : "Kotak session is off";
  const token = neo.has_access_token ? "access token found" : "access token missing in .env";
  $("neoLine").textContent = `${session}. ${token}.`;

  const badge = $("autoBadge");
  const on = data.enabled && !data.paused_today;
  badge.textContent = data.paused_today ? "PAUSED" : on ? "ON" : "OFF";
  badge.className = "pill " + (data.paused_today || !on ? "off" : "on");

  const tradesToday = data.trades_today || 0;
  const maxEntries = data.max_entries || 2;
  const scan =
    data.scan_status === "closed"
      ? `09:20 auto window is closed. Open ${tradesToday}/${maxEntries}. Use Redo today’s entries to flatten and rebuy with the live TA rules (today only).`
      : `Buys are allowed from 09:20 to 09:24 IST · ${tradesToday}/${maxEntries} today.`;
  $("runLine").textContent = `${data.clock || ""} · ${scan}`;

  const err = $("errorLine");
  if (data.last_error) {
    err.hidden = false;
    err.textContent = data.last_error;
  } else {
    err.hidden = true;
  }

  const score = data.score || {};
  const todayPnl = score.today_pnl != null ? score.today_pnl : score.pnl;
  const pnlClass = todayPnl > 0 ? "pos" : todayPnl < 0 ? "neg" : "";
  const roi = score.roi_pct;
  const roiClass = roi > 0 ? "pos" : roi < 0 ? "neg" : "";
  $("score").innerHTML = `
    <div><span>Spent today</span><strong>${plainMoney(score.debit)}</strong></div>
    <div><span>Today’s P&amp;L</span><strong class="${pnlClass}">${money(todayPnl)}</strong></div>
    <div><span>ROI</span><strong class="${roiClass}">${pct(roi)}</strong></div>
    <div><span>Open P&amp;L</span><strong class="${score.open_pnl > 0 ? "pos" : score.open_pnl < 0 ? "neg" : ""}">${money(score.open_pnl)}</strong></div>
    <div><span>Closed P&amp;L</span><strong class="${score.pnl > 0 ? "pos" : score.pnl < 0 ? "neg" : ""}">${money(score.pnl)}</strong></div>
    <div><span>Open / closed</span><strong>${score.open || 0} / ${score.closed || 0}</strong></div>`;

  applyButtonState(data);

  const board = data.board || {};
  const matches = board.matches || [];
  const names = matches.map((row) => `${row.symbol} ${row.option_type} (OI ${pct(row.oi_pct)})`).join(", ");
  $("boardMeta").textContent = board.as_of
    ? `Updated ${board.as_of.replace("T", " ").slice(11, 19)} · ${board.spurts_count || 0} OI-spurt names · match: ${names || "none"} · buy top ${maxEntries}`
    : "Waiting for the NSE lists. They refresh from 09:15.";
  $("gainers").innerHTML = bandTable(board.gainers, matches);
  $("losers").innerHTML = bandTable(board.losers, matches);
  renderCandidates(board.candidates || [], board.picked || []);
  renderPositions(data.positions);
  renderSignals(data.signals);
  renderTrades(data.trades);
}

async function refreshStatus() {
  const alive = $("aliveLine");
  try {
    const data = await api("/api/status");
    alive.textContent = "Server · live";
    alive.className = "alive ok";
    render(data);
  } catch (err) {
    alive.textContent = "Server · " + err.message;
    alive.className = "alive bad";
  }
}

$("connectBtn").addEventListener("click", () => {
  const btn = $("connectBtn");
  if (btn.dataset.mode === "disconnect") {
    runAction(btn, "Disconnecting…", async () => api("/api/disconnect", { method: "POST" }).then(() => api("/api/status")));
    return;
  }
  runAction(btn, "Connecting…", async () => {
    await api("/api/connect", {
      method: "POST",
      body: JSON.stringify({ totp: $("totp").value.trim(), mpin: $("mpin").value.trim() }),
    });
    $("mpin").value = "";
    return api("/api/status");
  });
});

$("autoBtn").addEventListener("click", () => {
  const btn = $("autoBtn");
  if (btn.dataset.mode === "resume") {
    runAction(btn, "Resuming…", () => api("/api/start", { method: "POST" }));
    return;
  }
  runAction(btn, "Pausing…", () => api("/api/stop", { method: "POST" }));
});

$("paperTodayBtn").addEventListener("click", () => {
  const btn = $("paperTodayBtn");
  const hasTrades = (latest && latest.trades_today) || 0;
  const working = hasTrades ? "Closing & rebuying…" : "Scanning & buying…";
  runAction(btn, working, async () => {
    const data = await api("/api/paper-today", { method: "POST" });
    const open = (data.score && data.score.open) || 0;
    const bought = data.trades_today || 0;
    if (data.last_error) {
      $("runLine").textContent = data.last_error;
    } else {
      $("runLine").textContent =
        `Today redo done · ${bought} fresh trade(s) · ${open} open now. Tomorrow auto stays at 09:20.`;
    }
    return data;
  });
});

$("refreshBtn").addEventListener("click", () => {
  runAction($("refreshBtn"), "Refreshing…", () => api("/api/refresh", { method: "POST" }));
});

$("wakeBtn").addEventListener("click", async () => {
  try {
    if (!navigator.wakeLock) return;
    await navigator.wakeLock.request("screen");
    $("wakeBtn").textContent = "Screen stays on";
  } catch (_) {
    $("wakeBtn").textContent = "Screen lock blocked";
  }
});

refreshStatus();
setInterval(() => {
  if (!busy) refreshStatus();
}, 4000);
