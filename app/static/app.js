const $ = (id) => document.getElementById(id);

const SLOT_LABELS = {
  t0920: "Live · auto",
  t0940: "09:40",
};

let activeSlot = localStorage.getItem("activeSlot") || "t0920";
if (activeSlot !== "t0920" && activeSlot !== "t0940") activeSlot = "t0920";
let resolveInFlight = false;

const optionDrafts = {
  t0920: localStorage.getItem("option_t0920") || "",
  t0940: localStorage.getItem("option_t0940") || "",
};

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2800);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = {};
  }
  if (!res.ok) {
    const detail = data.detail || data.error || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function withSlot(path) {
  const join = path.includes("?") ? "&" : "?";
  return `${path}${join}slot=${encodeURIComponent(activeSlot)}`;
}

function fmt(n) {
  if (n === null || n === undefined || n === "") return "—";
  const x = Number(n);
  if (Number.isNaN(x)) return String(n);
  return x.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

function slotLabel(slot) {
  return SLOT_LABELS[slot] || slot;
}

function exitReasonLabel(code) {
  const map = {
    tp: "Take profit +5%",
    sl: "Stop loss −5%",
    trail_sl: "Trail stop (locked profit)",
    eod: "EOD square-off",
    eod_flat: "EOD flat (quote failed)",
    carry: "Next-day carry close",
    manual: "Manual",
  };
  return map[code] || code || "—";
}

function isImmediate(status) {
  return status?.entry_mode === "immediate" || activeSlot === "t0920";
}

function updateChrome(status) {
  const label = status?.label || slotLabel(activeSlot);
  const exitMode = status?.exit_mode || "+/-5%";
  const eodTime = status?.eod_exit_time || "15:10";
  const buyTime = status?.buy_time || "09:40";
  $("slotLabel").textContent = label;
  $("pnlSlotLabel").textContent = label;
  $("histSlotLabel").textContent = label;
  $("exitModeLabel").textContent = exitMode;
  $("eodLabel").textContent = eodTime;

  const btn = $("resolveBtn");
  const autoBox = $("autoLiveBox");
  const manual = $("manualSetup");
  const title = $("setupTitle");
  if (isImmediate(status)) {
    if (title) title.textContent = "1. Live auto trading";
    $("setupHint").innerHTML =
      "No button buy. Auto-scan every 5 min (09:15–15:00). Hard SL <strong>−5%</strong>; at <strong>+5%</strong> trail starts (lock SL, raise target +1% each rung) · EOD <strong>15:10</strong>.";
    $("todayHintPrefix").textContent = "Auto buy/sell ·";
    $("buyTimeWrap").classList.add("hidden");
    autoBox?.classList.remove("hidden");
    manual?.classList.add("hidden");
    if (btn) btn.disabled = true;
  } else {
    if (title) title.textContent = "1. Call / Put name";
    $("setupHint").innerHTML =
      "Type like <strong>WIPRO call 160</strong>. One armed trade per day — auto buy at <strong>" +
      buyTime +
      "</strong>.";
    if (btn) btn.textContent = "Save & arm for " + buyTime;
    $("todayHintPrefix").textContent = "Auto buy";
    $("buyTimeWrap").classList.remove("hidden");
    $("buyTimeLabel").textContent = buyTime;
    if (btn) btn.disabled = resolveInFlight;
    autoBox?.classList.add("hidden");
    manual?.classList.remove("hidden");
  }
}

function renderPlan(plan, pick) {
  const box = $("planBox");
  const el = $("planText");
  if (!box || !el) return;
  if (!plan) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const bits = [];
  if (pick?.symbol) bits.push(`${pick.symbol} ${pick.option_type || ""} score ${fmt(pick.score)}`);
  if (plan.entry_option_ltp != null) bits.push(`entry ${fmt(plan.entry_option_ltp)}`);
  if (plan.tp_price != null) bits.push(`TP ${fmt(plan.tp_price)}`);
  if (plan.sl_price != null) bits.push(`SL ${fmt(plan.sl_price)}`);
  bits.push(`RR ${plan.risk_reward || "1:1"}`);
  if (plan.spot != null) bits.push(`spot ${fmt(plan.spot)}`);
  el.textContent = bits.join(" · ");
}

async function refreshAutoLiveStatus() {
  if (activeSlot !== "t0920") return;
  try {
    const data = await api("/api/auto-live/status");
    const st = $("autoLiveStatus");
    if (!st) return;
    const on = data.enabled !== false;
    const last = data.last_auto_result;
    const pend = (data.pending_exits && data.pending_exits[0]) || null;
    if (pend) {
      const sameDay = pend.trade_date === new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
      st.textContent = sameDay
        ? `Open #${pend.id} ${pend.option_trd_symbol || ""} @ ${fmt(pend.buy_price)} — watching +/-5% / EOD (no new buy until exit).`
        : `Finishing pending #${pend.id} first (${pend.option_trd_symbol || "option"} from ${pend.trade_date}) — no new buy until closed.`;
      st.className = sameDay ? "status ok" : "status err";
    } else if (last?.ok && last.call_name) {
      st.textContent = `Auto ON · last: ${last.call_name} · score ${fmt(last.score)} · ${last.at || ""}`;
      st.className = "status ok";
    } else if (last?.error) {
      st.textContent = `Auto ON · last scan: ${last.error}`;
      st.className = "status";
    } else {
      st.textContent = on
        ? data.in_window
          ? "Auto-scan ON · every 5 min · 09:15–15:00 · pending overnight opens close first next day."
          : `Auto ON · outside window (${data.entry_window || "09:15–15:00"} IST).`
        : "Auto-scan OFF (should stay ON).";
      st.className = "status";
    }
  } catch {
    /* ignore */
  }
}

function setActiveTab(slot) {
  activeSlot = slot;
  localStorage.setItem("activeSlot", slot);
  document.querySelectorAll(".tab").forEach((btn) => {
    const on = btn.dataset.slot === slot;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("optionName").value = optionDrafts[slot] || "";
  $("resolveStatus").textContent = "";
  $("resolveStatus").className = "status";
}

function renderResolved(resolved, callName, qty) {
  if (!resolved) {
    $("resolvedBox").classList.add("hidden");
    $("stockName").textContent = "—";
    $("optionNameMeta").textContent = "—";
    return;
  }
  $("resolvedBox").classList.remove("hidden");
  $("callName").textContent = callName || resolved.option?.call_name || "—";
  $("expiryVal").textContent = resolved.option?.expiry || "—";
  $("qtyVal").textContent = qty != null ? String(qty) : String(resolved.option?.lot_size || "—");
  $("optSym").textContent = resolved.option.trd_symbol || "—";
  $("stockSym").textContent = resolved.stock.trd_symbol || "—";
  $("stockName").textContent = resolved.stock.trd_symbol;
  $("optionNameMeta").textContent = resolved.option.trd_symbol;
}

function renderQuotes(q) {
  if (!q) return;
  $("stockLtp").textContent = fmt(q.stock?.ltp);
  $("optionLtp").textContent = fmt(q.option?.ltp);
  if (q.stock?.trd_symbol) $("stockName").textContent = q.stock.trd_symbol;
  if (q.option?.trd_symbol) $("optionNameMeta").textContent = q.option.trd_symbol;
  $("quoteUpdated").textContent = `Updated: ${q.updated_at || "—"}`;
}

function statusLabel(trade, status) {
  const buyTime = status?.buy_time || "09:40";
  const exitMode = status?.exit_mode || "+/-5%";
  const labels = {
    armed: `Ready — will buy at ${buyTime} market price`,
    bought: `Open — watching for ${exitMode} exit`,
    completed: "Completed",
    failed: "Failed",
    cancelled: "Cancelled",
  };
  return labels[trade.status] || trade.status;
}

function renderTodaySingle(trade, status) {
  $("todaySingle").classList.remove("hidden");
  $("todayMulti").classList.add("hidden");
  $("todayMulti").innerHTML = "";

  const levels = status?.exit_levels;
  if (levels?.trail_active) {
    $("exitLevels").textContent =
      `TRAIL lock +${fmt(levels.trail_sl_pct)}% (${fmt(levels.sl)}) · next +${fmt(levels.trail_target_pct)}% (${fmt(levels.tp)}) · buy ${fmt(levels.buy)}`;
  } else if (levels) {
    $("exitLevels").textContent =
      `Arm trail at +${fmt(levels.exit_pct)}% (${fmt(levels.tp)}) · hard SL ${fmt(levels.sl)} · buy ${fmt(levels.buy)}`;
  } else {
    $("exitLevels").textContent = "Hard SL −5% · trail from +5%";
  }

  if (!trade) {
    $("todayStatus").textContent = "Waiting — save option name above";
    $("todayBuy").textContent = "—";
    $("todaySell").textContent = "—";
    $("todayExitReason").textContent = "—";
    $("todaySpend").textContent = "—";
    $("todayPnl").textContent = "—";
    $("todayPnl").className = "";
    $("todayFail").textContent = "";
    updateCountdown(null, status);
    return;
  }
  $("todayStatus").textContent = statusLabel(trade, status);
  $("todayBuy").textContent =
    trade.buy_price != null ? `${fmt(trade.buy_price)} (stock ${fmt(trade.stock_ltp_at_buy)})` : "—";
  $("todaySell").textContent =
    trade.sell_price != null ? `${fmt(trade.sell_price)} (stock ${fmt(trade.stock_ltp_at_sell)})` : "—";
  $("todayExitReason").textContent = trade.exit_reason ? exitReasonLabel(trade.exit_reason) : "—";
  if (trade.buy_price != null && trade.qty != null) {
    const spend = Number(trade.buy_price) * Number(trade.qty);
    $("todaySpend").textContent = `${fmt(spend)} (${fmt(trade.buy_price)} × ${trade.qty})`;
  } else {
    $("todaySpend").textContent = "—";
  }
  const pnl = trade.pnl;
  $("todayPnl").textContent = pnl == null ? "—" : fmt(pnl);
  $("todayPnl").className = pnl == null ? "" : Number(pnl) >= 0 ? "pnl-pos" : "pnl-neg";
  $("todayFail").textContent = trade.fail_reason || "";
  updateCountdown(trade, status);
}

function renderTodayMulti(trades, status) {
  $("todaySingle").classList.add("hidden");
  $("todayMulti").classList.remove("hidden");
  $("todayFail").textContent = "";

  const pending = status?.pending_exits || [];
  const open = pending.length
    ? pending
    : (trades || []).filter((t) => t.status === "bought");
  if (open.length) {
    const t = open[0];
    const levels = status?.exit_levels;
    const carryNote =
      status?.pending_carry && status.pending_carry.trade_date
        ? ` · finishing #${status.pending_carry.id} from ${status.pending_carry.trade_date} first`
        : "";
    if (levels?.trail_active) {
      $("exitLevels").textContent =
        `Open ${open.length}${carryNote} · TRAIL lock SL +${fmt(levels.trail_sl_pct)}% (${fmt(levels.sl)}) · next +${fmt(levels.trail_target_pct)}% (${fmt(levels.tp)})`;
    } else if (levels) {
      $("exitLevels").textContent =
        `Open ${open.length}${carryNote} · arm trail at TP ${fmt(levels.tp)} · hard SL ${fmt(levels.sl)}`;
    } else {
      const pct = status?.exit_pct || 5;
      const buy = Number(t.buy_price);
      $("exitLevels").textContent = `Open ${open.length}${carryNote} · trail from +${pct}% · SL −${pct}%`;
    }
  } else {
    $("exitLevels").textContent = "Hard SL −5% · at +5% trail lock + raise target +1%";
  }

  const todayIds = new Set((trades || []).map((t) => t.id));
  const carryOnly = pending.filter((t) => !todayIds.has(t.id));
  const rows = [...carryOnly, ...(trades || [])];

  if (!rows.length) {
    $("todayMulti").innerHTML = `<p class="hint">No trades yet today — auto-scan will buy when a setup qualifies.</p>`;
    updateCountdown(null, status);
    return;
  }

  $("todayMulti").innerHTML = rows
    .map((t) => {
      const pnlClass = t.pnl == null ? "" : Number(t.pnl) >= 0 ? "pnl-pos" : "pnl-neg";
      const spend =
        t.buy_price != null && t.qty != null ? fmt(Number(t.buy_price) * Number(t.qty)) : "—";
      const carry =
        t.trade_date && status?.pending_carry?.id === t.id
          ? ` · carry from ${t.trade_date}`
          : "";
      return `<div class="hist-item">
        <div class="hist-date">#${t.id} · ${t.status}${carry}</div>
        <div class="hist-sym">${t.option_trd_symbol || "—"}</div>
        <div class="hist-meta">${t.option_type || ""} ${t.strike || ""} · qty ${t.qty ?? "—"} · spend ${spend}</div>
        <div class="hist-prices">${fmt(t.buy_price)} → ${fmt(t.sell_price)}${
          t.exit_reason ? ` · ${exitReasonLabel(t.exit_reason)}` : ""
        }</div>
        <div class="hist-pnl ${pnlClass}">${fmt(t.pnl)}</div>
        ${t.fail_reason ? `<div class="error">${t.fail_reason}</div>` : ""}
      </div>`;
    })
    .join("");

  const watching = open[0] || rows[0];
  updateCountdown(watching?.status === "bought" || watching?.status === "armed" ? watching : null, status);
}

function renderToday(status) {
  updateChrome(status);
  if (status?.today_pnl != null) setPnlEl("todayPnlBanner", status.today_pnl);
  if (isImmediate(status)) {
    renderTodayMulti(status.today_trades || [], status);
  } else {
    renderTodaySingle(status.today, status);
  }
}

function updateCountdown(trade, status) {
  const now = new Date();
  const istParts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(now);
  const get = (t) => istParts.find((p) => p.type === t)?.value || "00";
  const cur = `${get("hour")}:${get("minute")}:${get("second")}`;
  const exitMode = status?.exit_mode || "+/-5%";
  const buyTime = status?.buy_time || "09:40";

  if (isImmediate(status)) {
    const openCount = status?.open_count || 0;
    if (openCount > 0) {
      $("countdown").textContent = `Watching ${openCount} open · ${exitMode} · IST ${cur}`;
    } else {
      $("countdown").textContent = `Ready to buy on save · IST ${cur}`;
    }
    return;
  }

  if (!trade || ["completed", "failed", "cancelled"].includes(trade.status)) {
    $("countdown").textContent = `IST ${cur}`;
    return;
  }
  if (trade.status === "armed") {
    $("countdown").textContent = `Auto buy at ${buyTime} market · IST ${cur}`;
    return;
  }
  if (trade.status === "bought") {
    $("countdown").textContent = `Watching ${exitMode} exit · IST ${cur}`;
  }
}

function setPnlEl(id, total) {
  const el = $(id);
  if (!el) return;
  if (total == null || Number.isNaN(Number(total))) {
    el.textContent = "—";
    el.className = "";
    return;
  }
  const n = Number(total);
  el.textContent = fmt(n);
  el.className = n >= 0 ? "pnl-pos" : "pnl-neg";
}

function renderPnlBanners(todayPnl, totalPnl) {
  setPnlEl("todayPnlBanner", todayPnl);
  setPnlEl("totalPnl", totalPnl);
}

function renderHistory(trades, totalPnl, todayPnl) {
  renderPnlBanners(todayPnl, totalPnl);
  const box = $("history");
  if (!trades?.length) {
    box.innerHTML = `<p class="hint">No trades yet for this tab.</p>`;
    return;
  }
  box.innerHTML = trades
    .map((t) => {
      const pnlClass = t.pnl == null ? "" : Number(t.pnl) >= 0 ? "pnl-pos" : "pnl-neg";
      const qty = t.qty != null ? ` · qty ${t.qty}` : "";
      return `<div class="hist-item">
        <div class="hist-date">${t.trade_date}${t.id != null ? ` · #${t.id}` : ""}</div>
        <div class="hist-sym">${t.option_trd_symbol || "—"}</div>
        <div class="hist-meta">${t.option_type || ""} ${t.strike || ""}${qty}</div>
        <div class="hist-prices">${fmt(t.buy_price)} → ${fmt(t.sell_price)}${
          t.exit_reason ? ` · ${exitReasonLabel(t.exit_reason)}` : ""
        }${t.status === "failed" && t.buy_price != null && t.sell_price == null ? " · incomplete exit" : ""}</div>
        <div class="hist-pnl ${pnlClass}">${fmt(t.pnl)}</div>
        ${t.fail_reason ? `<div class="error">${t.fail_reason}</div>` : ""}
      </div>`;
    })
    .join("");
}

async function refreshStatus() {
  const data = await api(withSlot("/api/status"));
  if (data.option_name) {
    optionDrafts[activeSlot] = data.option_name;
    localStorage.setItem(`option_${activeSlot}`, data.option_name);
    if (document.activeElement !== $("optionName")) {
      $("optionName").value = data.option_name;
    }
  }
  const callName = data.resolved?.option?.call_name;
  renderResolved(data.resolved, callName, data.qty);
  if (data.last_quotes) renderQuotes(data.last_quotes);
  else {
    $("stockLtp").textContent = "—";
    $("optionLtp").textContent = "—";
    $("quoteUpdated").textContent = "Updated: —";
  }
  renderToday(data);
  if (data.today_pnl != null) setPnlEl("todayPnlBanner", data.today_pnl);
  return data;
}

async function refreshHistory() {
  const data = await api(withSlot("/api/history"));
  renderHistory(data.trades, data.total_pnl, data.today_pnl);
}

async function pollQuotes() {
  try {
    const status = await api(withSlot("/api/status"));
    if (status.resolved && status.neo?.has_access_token) {
      try {
        const q = await api(withSlot("/api/quotes"));
        renderQuotes(q);
      } catch {
        /* ignore */
      }
    }
    renderToday(status);
  } catch {
    /* ignore */
  }
}

async function switchSlot(slot) {
  optionDrafts[activeSlot] = $("optionName").value.trim();
  localStorage.setItem(`option_${activeSlot}`, optionDrafts[activeSlot] || "");
  setActiveTab(slot);
  try {
    await refreshStatus();
    await refreshHistory();
  } catch (e) {
    toast(e.message);
  }
}

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.slot === activeSlot) return;
    switchSlot(btn.dataset.slot);
  });
});

$("optionName").addEventListener("input", () => {
  optionDrafts[activeSlot] = $("optionName").value;
  localStorage.setItem(`option_${activeSlot}`, optionDrafts[activeSlot] || "");
});

$("resolveBtn")?.addEventListener("click", async () => {
  // Live tab has no resolve button; only 09:40 timed arm uses this.
  if (activeSlot === "t0920") return;
  if (resolveInFlight || $("resolveBtn").disabled) return;
  resolveInFlight = true;
  $("resolveBtn").disabled = true;
  try {
    $("resolveStatus").textContent = "Auto-picking nearest expiry + lot qty…";
    $("resolveStatus").className = "status";
    const option_name = $("optionName").value.trim();
    optionDrafts[activeSlot] = option_name;
    localStorage.setItem(`option_${activeSlot}`, option_name);
    const body = { option_name, slot: activeSlot };
    const data = await api("/api/resolve", { method: "POST", body: JSON.stringify(body) });
    renderResolved(data.resolved, data.call_name, data.qty);
    if (data.quotes) renderQuotes(data.quotes);
    $("resolveStatus").textContent = `${data.call_name} · expiry ${data.expiry} · qty ${data.qty}${
      data.armed ? " · armed" : ""
    }`;
    $("resolveStatus").className = "status ok";
    toast(`${slotLabel(activeSlot)} · ${data.resolved.option.trd_symbol} · lot ${data.qty}`);
    await refreshStatus();
    await refreshHistory();
  } catch (e) {
    $("resolveStatus").textContent = e.message;
    $("resolveStatus").className = "status err";
    toast(e.message);
  } finally {
    resolveInFlight = false;
    try {
      await refreshStatus();
    } catch {
      $("resolveBtn").disabled = false;
    }
  }
});

(async function init() {
  setActiveTab(activeSlot);
  try {
    await refreshStatus();
    await refreshHistory();
    await refreshAutoLiveStatus();
  } catch (e) {
    toast(e.message);
  }
  setInterval(pollQuotes, 2000);
  setInterval(async () => {
    try {
      await refreshHistory();
      await refreshAutoLiveStatus();
    } catch {
      /* ignore */
    }
  }, 15000);
})();
