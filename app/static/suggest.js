(() => {
  const $ = (id) => document.getElementById(id);

  function fmtPct(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    const sign = n > 0 ? "+" : "";
    return `${sign}${n.toFixed(2)}%`;
  }

  function fmtLtp(v) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    return Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }

  function toast(msg) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add("hidden"), 2200);
  }

  function renderSimpleList(elId, rows, emptyMsg) {
    const el = $(elId);
    el.innerHTML = "";
    if (!rows || !rows.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = emptyMsg;
      el.appendChild(p);
      return;
    }
    rows.forEach((s, i) => {
      const row = document.createElement("div");
      row.className = "hist-row";
      const cls = (s.pct || 0) >= 0 ? "ok" : "bad";
      row.innerHTML = `<span>#${i + 1} ${s.symbol}</span><span class="${cls}">${fmtPct(s.pct)}</span>`;
      el.appendChild(row);
    });
  }

  function renderTa(list) {
    const el = $("taList");
    el.innerHTML = "";
    if (!list || !list.length) {
      el.innerHTML = `<p class="hint">No TA results.</p>`;
      return;
    }
    list.forEach((a, i) => {
      const row = document.createElement("div");
      row.className = "hist-item ta-item";
      const ind = a.indicators || {};
      const cls = a.side === "winner" ? "ok" : "bad";
      const scoreCls = (a.score || 0) >= 65 ? "pnl-pos" : (a.score || 0) < 45 ? "pnl-neg" : "";
      const title = a.ok
        ? `${a.symbol} · ${a.bias}`
        : `${a.symbol} · failed`;
      const detail = a.ok
        ? `RSI ${ind.rsi14 ?? "—"} · SMA20 ${ind.above_sma20 ? "above" : "below"} · MACD hist ${ind.macd_hist ?? "—"} · vol ${ind.volume_vs_avg20 ?? "—"}x`
        : a.error || "error";
      const reasons = (a.reasons || []).slice(0, 3).join(" · ");
      row.innerHTML = `
        <span class="hist-date">#${i + 1}</span>
        <span class="hist-sym">${title} <span class="${cls}">${fmtPct(a.pct)}</span></span>
        <span class="hist-pnl ${scoreCls}">${a.ok ? a.score : "—"}</span>
        <span class="hist-meta">${detail}</span>
        <span class="hist-prices">${reasons || ""}</span>
      `;
      el.appendChild(row);
    });
  }

  function fillPick(symId, metaId, reasonsId, pick, sectorName) {
    if (pick && pick.ok) {
      $(symId).textContent = pick.symbol;
      $(metaId).textContent = [
        `score ${pick.score}`,
        fmtPct(pick.pct),
        `LTP ${fmtLtp(pick.ltp)}`,
        sectorName || "",
        `RSI ${pick.indicators?.rsi14 ?? "—"}`,
      ]
        .filter(Boolean)
        .join(" · ");
      $(reasonsId).textContent = (pick.reasons || []).join(" · ");
    } else {
      $(symId).textContent = "None";
      $(metaId).textContent = "No TA match on this side";
      $(reasonsId).textContent = "";
    }
  }

  function render(data) {
    fillPick(
      "loserSym",
      "loserMeta",
      "loserReasons",
      data.selected_loser,
      data.lowest_sector?.index
    );
    fillPick(
      "winnerSym",
      "winnerMeta",
      "winnerReasons",
      data.selected_winner,
      data.highest_sector?.index
    );

    const ranked = data.ta?.ranked || data.ta?.analyses || [];
    renderTa(ranked.length ? ranked : data.ta?.analyses || []);

    renderSimpleList("topLosers", data.top_losers, "No losers in 0% to −3%.");
    renderSimpleList("topWinners", data.top_winners, "No winners in 0% to +3%.");

    $("lowSector").textContent = data.lowest_sector
      ? `${data.lowest_sector.index}  ${fmtPct(data.lowest_sector.pct)}${data.lowest_sector_fallback ? " (next FO sector)" : ""}`
      : "—";
    $("highSector").textContent = data.highest_sector
      ? `${data.highest_sector.index}  ${fmtPct(data.highest_sector.pct)}${data.highest_sector_fallback ? " (next FO sector)" : ""}`
      : "—";

    const skipLow = (data.skipped?.lowest_sector_below_minus_max || [])
      .map((s) => `${s.symbol} ${fmtPct(s.pct)}`)
      .join(", ");
    const skipHigh = (data.skipped?.highest_sector_above_plus_max || [])
      .map((s) => `${s.symbol} ${fmtPct(s.pct)}`)
      .join(", ");
    const noFoHigh = (data.skipped?.highest_sector_no_fo_options || [])
      .map((s) => s.symbol)
      .join(", ");
    const bits = [];
    if (skipLow) bits.push(`Skipped (&lt; −3%): ${skipLow}`);
    if (skipHigh) bits.push(`Skipped (&gt; +3%): ${skipHigh}`);
    if (noFoHigh) bits.push(`No FO options (skipped): ${noFoHigh}`);
    if (data.highest_sector_fallback) bits.push("Winner side used next sector(s) — top sector had no FO names in 0% to +3%.");
    if (data.lowest_sector_fallback) bits.push("Loser side used next sector(s) — bottom sector had no FO names in 0% to −3%.");
    if (data.ta?.method) bits.push(data.ta.method);
    $("skipped").innerHTML = bits.join("<br/>");

    const list = $("sectorList");
    list.innerHTML = "";
    (data.sectors || []).forEach((s) => {
      const row = document.createElement("div");
      row.className = "hist-row";
      const cls = s.pct >= 0 ? "ok" : "bad";
      row.innerHTML = `<span>${s.index}</span><span class="${cls}">${fmtPct(s.pct)}</span>`;
      list.appendChild(row);
    });
  }

  async function refresh() {
    const btn = $("refreshBtn");
    btn.disabled = true;
    $("status").textContent = "Fetching heatmap + running TA (may take ~10–20s)…";
    try {
      const res = await fetch("/api/suggest-stock?max_pct=3");
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Fetch failed");
      render(data);
      const loser = data.selected_loser?.symbol || "—";
      const winner = data.selected_winner?.symbol || "—";
      $("status").textContent = `Selected · loser ${loser} · winner ${winner}`;
      toast(`${loser} + ${winner}`);
    } catch (err) {
      $("status").textContent = err.message || String(err);
      toast("Failed to fetch");
    } finally {
      btn.disabled = false;
    }
  }

  $("refreshBtn").addEventListener("click", refresh);
  refresh();
})();
