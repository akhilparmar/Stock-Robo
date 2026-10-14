"""NSE sector heatmap helpers: extreme sectors + constrained stock picks."""
from __future__ import annotations

from typing import Any

import httpx

from app.neo_client import NeoError, neo_client
from app.tech_analysis import analyze_candidates

NSE_HOME = "https://www.nseindia.com"
ALL_INDICES_URL = f"{NSE_HOME}/api/allIndices"
STOCK_INDICES_URL = f"{NSE_HOME}/api/equity-stock-indices"

# Cap used when picking extreme stocks inside extreme sectors.
MAX_STOCK_PCT = 3.0
# How many names from each band get technical analysis.
TA_TOP_N = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{NSE_HOME}/market-data/live-equity-market",
}


class NseHeatmapError(Exception):
    pass


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _session() -> httpx.Client:
    client = httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=True)
    # Warm cookies; NSE often returns 403 on the homepage but still sets session.
    try:
        client.get(NSE_HOME)
    except httpx.HTTPError:
        pass
    try:
        client.get(f"{NSE_HOME}/market-data/live-equity-market")
    except httpx.HTTPError:
        pass
    return client


def fetch_sectoral_indices(client: httpx.Client | None = None) -> list[dict[str, Any]]:
    owns = client is None
    client = client or _session()
    try:
        resp = client.get(ALL_INDICES_URL)
        if resp.status_code >= 400:
            raise NseHeatmapError(f"NSE allIndices failed ({resp.status_code})")
        rows = (resp.json() or {}).get("data") or []
        sectors: list[dict[str, Any]] = []
        for row in rows:
            if row.get("key") != "SECTORAL INDICES":
                continue
            name = (row.get("index") or "").strip()
            pct = _as_float(row.get("percentChange"))
            if not name or pct is None:
                continue
            sectors.append(
                {
                    "index": name,
                    "pct": pct,
                    "last": _as_float(row.get("last")),
                    "variation": _as_float(row.get("variation")),
                }
            )
        if not sectors:
            raise NseHeatmapError("No sectoral indices returned by NSE")
        sectors.sort(key=lambda x: x["pct"])
        return sectors
    finally:
        if owns:
            client.close()


def fetch_index_stocks(index_name: str, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    owns = client is None
    client = client or _session()
    try:
        resp = client.get(STOCK_INDICES_URL, params={"index": index_name})
        if resp.status_code >= 400:
            raise NseHeatmapError(
                f"NSE equity-stock-indices failed for {index_name} ({resp.status_code})"
            )
        rows = (resp.json() or {}).get("data") or []
        out: list[dict[str, Any]] = []
        for row in rows:
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol or symbol == index_name.strip().upper():
                continue
            # Index summary row usually has priority=1 and no marketType.
            if row.get("priority") == 1 and row.get("marketType") in (None, ""):
                continue
            pct = _as_float(row.get("pChange"))
            if pct is None:
                continue
            meta = row.get("meta") or {}
            out.append(
                {
                    "symbol": symbol,
                    "name": meta.get("companyName") or symbol,
                    "pct": pct,
                    "ltp": _as_float(row.get("lastPrice") or row.get("last")),
                    "change": _as_float(row.get("change")),
                }
            )
        out.sort(key=lambda x: x["pct"])
        return out
    finally:
        if owns:
            client.close()


def _in_down_band(pct: float, max_pct: float) -> bool:
    """Down 0% to max_pct inclusive: [-max_pct, 0]."""
    return -max_pct <= pct <= 0


def _in_up_band(pct: float, max_pct: float) -> bool:
    """Up 0% to max_pct inclusive: [0, +max_pct]."""
    return 0 <= pct <= max_pct


def _pick_lowest_in_band(stocks: list[dict[str, Any]], max_pct: float) -> dict[str, Any] | None:
    """
    From lowest sector: keep only stocks down 0..max_pct, then take the top-lowest
    (most negative). Skips names worse than -max_pct and anything green.
    """
    band = [s for s in stocks if _in_down_band(s["pct"], max_pct)]
    if not band:
        return None
    # stocks are sorted ascending; band preserves that → first is most negative
    pick = band[0]
    skipped_before = sum(1 for s in stocks if s["pct"] < -max_pct)
    return {**pick, "rank_from_extreme": skipped_before + 1, "band": "0_to_-3"}


def _pick_highest_in_band(stocks: list[dict[str, Any]], max_pct: float) -> dict[str, Any] | None:
    """
    From highest sector: keep only stocks up 0..max_pct, then take the top-highest
    (most positive). Skips names above +max_pct and anything red.
    """
    band = [s for s in stocks if _in_up_band(s["pct"], max_pct)]
    if not band:
        return None
    pick = band[-1]  # most positive in the up band
    skipped_before = sum(1 for s in stocks if s["pct"] > max_pct)
    return {**pick, "rank_from_extreme": skipped_before + 1, "band": "0_to_+3"}


def _filter_optionable(
    stocks: list[dict[str, Any]],
    optionable: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only symbols that have FO options; return (kept, dropped)."""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for s in stocks:
        sym = str(s.get("symbol") or "").strip().upper()
        if sym in optionable:
            kept.append(s)
        else:
            dropped.append({**s, "skip_reason": "no_fo_options"})
    return kept, dropped


def _pick_sector_side(
    sectors: list[dict[str, Any]],
    *,
    side: str,
    max_pct: float,
    ta_top_n: int,
    optionable: set[str],
    client: httpx.Client,
) -> dict[str, Any]:
    """
    Walk sector ranking until we find FO-optionable names inside the % band.
    side=loser → lowest sectors first; side=winner → highest first.
    """
    ordered = sectors if side == "loser" else list(reversed(sectors))
    tried: list[dict[str, Any]] = []
    for sector in ordered:
        raw = fetch_index_stocks(sector["index"], client)
        kept, no_opt = _filter_optionable(raw, optionable)
        if side == "loser":
            band = [s for s in kept if _in_down_band(s["pct"], max_pct)]
            top = band[:ta_top_n]
            pick = _pick_lowest_in_band(kept, max_pct)
            outside_band = [s for s in kept if s["pct"] < -max_pct]
            wrong_sign = [s for s in kept if s["pct"] > 0]
        else:
            band = [s for s in kept if _in_up_band(s["pct"], max_pct)]
            top = list(reversed(band))[:ta_top_n]
            pick = _pick_highest_in_band(kept, max_pct)
            outside_band = [s for s in reversed(kept) if s["pct"] > max_pct]
            wrong_sign = [s for s in kept if s["pct"] < 0]

        tried.append(
            {
                "sector": sector,
                "optionable": len(kept),
                "in_band": len(band),
                "no_fo_options": len(no_opt),
            }
        )
        if top:
            return {
                "sector": sector,
                "stocks": kept,
                "band": band if side == "loser" else list(reversed(band)),
                "top": top,
                "pick": pick,
                "no_opt": no_opt,
                "outside_band": outside_band,
                "wrong_sign": wrong_sign,
                "sectors_tried": tried,
                "fallback_used": len(tried) > 1,
            }

    # Nothing found across all sectors
    empty_sector = ordered[0] if ordered else {"index": "—", "pct": 0}
    return {
        "sector": empty_sector,
        "stocks": [],
        "band": [],
        "top": [],
        "pick": None,
        "no_opt": [],
        "outside_band": [],
        "wrong_sign": [],
        "sectors_tried": tried,
        "fallback_used": True,
    }


def suggest_extreme_stocks(
    max_pct: float = MAX_STOCK_PCT,
    *,
    ta_top_n: int = TA_TOP_N,
    run_ta: bool = True,
) -> dict[str, Any]:
    """
    Heatmap shortlist + TA → one loser + one winner:
    1) Rank NSE sectoral indices
    2) From lowest/highest, keep FO-optionable stocks in ±max_pct band
       (walk next sectors if extreme sector has none)
    3) Take top N losers + top N winners
    4) Score with daily TA → best loser AND best winner
    """
    try:
        optionable = neo_client.optionable_equity_symbols()
    except NeoError as exc:
        raise NseHeatmapError(f"Could not load FO optionable list from Neo: {exc}") from exc
    if not optionable:
        raise NseHeatmapError("Neo FO master returned no optionable underlyings")

    client = _session()
    try:
        sectors = fetch_sectoral_indices(client)
        low = _pick_sector_side(
            sectors,
            side="loser",
            max_pct=max_pct,
            ta_top_n=ta_top_n,
            optionable=optionable,
            client=client,
        )
        high = _pick_sector_side(
            sectors,
            side="winner",
            max_pct=max_pct,
            ta_top_n=ta_top_n,
            optionable=optionable,
            client=client,
        )

        lowest_sector = low["sector"]
        highest_sector = high["sector"]
        low_stocks = low["stocks"]
        high_stocks = high["stocks"]
        low_band = low["band"]
        high_band = high["band"]
        top_losers = low["top"]
        top_winners = high["top"]
        lowest_stock = low["pick"]
        highest_stock = high["pick"]
        low_no_opt = low["no_opt"]
        high_no_opt = high["no_opt"]
        skipped_lower = low["outside_band"]
        skipped_higher = high["outside_band"]
        low_outside_green = low["wrong_sign"]
        high_outside_red = high["wrong_sign"]

        ta: dict[str, Any] | None = None
        selected_loser: dict[str, Any] | None = None
        selected_winner: dict[str, Any] | None = None
        if run_ta and (top_losers or top_winners):
            ta = analyze_candidates(top_losers, top_winners)
            selected_loser = ta.get("selected_loser")
            selected_winner = ta.get("selected_winner")

        return {
            "ok": True,
            "max_pct": max_pct,
            "source": "NSE sectoral indices (heatmap) + Neo FO options filter + Yahoo daily TA",
            "optionable_count": len(optionable),
            "rule": {
                "lowest": f"down 0% to -{max_pct:g}% -> FO-optionable -> top {ta_top_n} losers",
                "highest": f"up 0% to +{max_pct:g}% -> FO-optionable -> top {ta_top_n} winners",
                "final": "TA best loser + TA best winner (options available only)",
                "fallback": "If extreme sector has no FO names in band, try next sectors",
            },
            "sectors": sectors,
            "lowest_sector": lowest_sector,
            "highest_sector": highest_sector,
            "lowest_sector_fallback": low["fallback_used"],
            "highest_sector_fallback": high["fallback_used"],
            "sectors_tried_loser": low["sectors_tried"],
            "sectors_tried_winner": high["sectors_tried"],
            "lowest_stock": lowest_stock,
            "highest_stock": highest_stock,
            "top_losers": top_losers,
            "top_winners": top_winners,
            "ta": ta,
            "selected_loser": selected_loser,
            "selected_winner": selected_winner,
            # Backward-compatible alias = stronger of the two
            "selected_stock": ta.get("selected") if ta else None,
            "band_candidates": {
                "lowest_sector_0_to_minus_max": low_band,
                "highest_sector_0_to_plus_max": high_band,
            },
            "skipped": {
                "lowest_sector_below_minus_max": skipped_lower[:8],
                "highest_sector_above_plus_max": skipped_higher[:8],
                "lowest_sector_green_excluded": low_outside_green[:5],
                "highest_sector_red_excluded": list(reversed(high_outside_red))[:5],
                "lowest_sector_no_fo_options": low_no_opt[:8],
                "highest_sector_no_fo_options": high_no_opt[:8],
            },
            "preview": {
                "lowest_sector_bottom": low_stocks[:8],
                "highest_sector_top": list(reversed(high_stocks[-8:])) if high_stocks else [],
            },
        }
    finally:
        client.close()
