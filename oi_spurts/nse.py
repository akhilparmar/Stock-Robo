"""NSE Nifty 200 movers and OI-spurts list. No Kotak calls."""
from __future__ import annotations

from typing import Any

import httpx

from oi_spurts.config import OI_TOP_N

NSE_HOME = "https://www.nseindia.com"
OI_PAGE = f"{NSE_HOME}/market-data/oi-spurts"
UNDERLYINGS_URL = f"{NSE_HOME}/api/live-analysis-oi-spurts-underlyings"
INDEX_URL = f"{NSE_HOME}/api/equity-stock-indices"
NIFTY_200 = "NIFTY 200"
INDEX_UNDERLYINGS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "SENSEX",
    "BANKEX",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": OI_PAGE,
}


class NseError(Exception):
    pass


def load_board(client: httpx.Client | None = None) -> dict[str, Any]:
    owns = client is None
    client = client or _session()
    try:
        stocks = fetch_nifty_200(client)
        spurts = select_spurts(fetch_oi_spurts(client), OI_TOP_N)
        return {"stocks": stocks, "spurts": spurts}
    finally:
        if owns:
            client.close()


def fetch_nifty_200(client: httpx.Client) -> list[dict[str, Any]]:
    resp = client.get(INDEX_URL, params={"index": NIFTY_200})
    if resp.status_code >= 400:
        raise NseError(f"Nifty 200 request failed ({resp.status_code})")
    stocks = parse_index_rows(resp.json(), NIFTY_200)
    if not stocks:
        raise NseError("Nifty 200 returned no stocks")
    return stocks


def fetch_oi_spurts(client: httpx.Client) -> list[dict[str, Any]]:
    resp = client.get(UNDERLYINGS_URL)
    if resp.status_code >= 400:
        raise NseError(f"OI spurts request failed ({resp.status_code})")
    spurts = parse_oi_rows(resp.json())
    if not spurts:
        raise NseError("OI spurts returned no underlyings")
    return spurts


def parse_index_rows(payload: Any, index_name: str) -> list[dict[str, Any]]:
    rows = _rows(payload)
    index_key = index_name.strip().upper()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol == index_key:
            continue
        if row.get("priority") == 1 and row.get("marketType") in (None, ""):
            continue
        pct = _as_float(row.get("pChange"))
        if pct is None:
            continue
        out.append(
            {
                "symbol": symbol,
                "pct": pct,
                "ltp": _as_float(row.get("lastPrice") or row.get("last")),
                "prev_close": _as_float(row.get("previousClose") or row.get("prevClose")),
            }
        )
    return out


def parse_oi_rows(payload: Any) -> list[dict[str, Any]]:
    rows = _rows(payload)
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("underlying") or "").strip().upper()
        if not symbol:
            continue
        oi_pct = _oi_pct(row)
        current = found.get(symbol)
        if current is None or _abs(oi_pct) > _abs(current.get("oi_pct")):
            found[symbol] = {"symbol": symbol, "oi_pct": oi_pct}
    return list(found.values())


def select_spurts(rows: list[dict[str, Any]], n: int = OI_TOP_N) -> list[dict[str, Any]]:
    """Head of the OI-spurts page: biggest positive % change in open interest."""
    ranked = [
        row
        for row in rows
        if row.get("symbol") not in INDEX_UNDERLYINGS and (row.get("oi_pct") or 0) > 0
    ]
    ranked.sort(key=lambda row: float(row["oi_pct"]), reverse=True)
    return ranked[:n]


def _session() -> httpx.Client:
    client = httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=True)
    for url in (NSE_HOME, OI_PAGE):
        try:
            client.get(url)
        except httpx.HTTPError:
            pass
    return client


def _rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "underlying", "underlyings", "contracts"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return inner
    return []


def _oi_pct(row: dict[str, Any]) -> float | None:
    # NSE names this column avgInOI. It is percent change in open interest.
    named = _as_float(row.get("avgInOI"))
    if named is not None:
        return named
    for key, value in row.items():
        compact = str(key).lower().replace("_", "")
        if compact == "avginioi":
            return _as_float(value)
        if "pchange" in compact and "oi" in compact:
            return _as_float(value)
        if compact in {"pchangeinoi", "pchangeinopeninterest", "percentchangeinoi"}:
            return _as_float(value)
    return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _abs(value: float | None) -> float:
    if value is None:
        return -1.0
    return abs(value)
