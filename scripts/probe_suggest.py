"""Run sector extreme stock suggestion once."""
from __future__ import annotations

from urllib.parse import quote

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
}

MAX_PCT = 3.0


def main() -> None:
    with httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=True) as c:
        c.get("https://www.nseindia.com/")
        indices = c.get("https://www.nseindia.com/api/allIndices").json()["data"]
        sectors = []
        for row in indices:
            if row.get("key") != "SECTORAL INDICES":
                continue
            name = row.get("index")
            if not name:
                continue
            try:
                pct = float(row.get("percentChange") or 0)
            except (TypeError, ValueError):
                continue
            sectors.append(
                {
                    "index": name,
                    "pct": pct,
                    "last": row.get("last"),
                }
            )
        sectors.sort(key=lambda x: x["pct"])
        print("SECTORS low->high:")
        for s in sectors:
            print(f"  {s['pct']:+.2f}%  {s['index']}")

        lowest_sector = sectors[0]
        highest_sector = sectors[-1]

        def stocks(index_name: str) -> list[dict]:
            r = c.get(
                "https://www.nseindia.com/api/equity-stock-indices",
                params={"index": index_name},
            )
            r.raise_for_status()
            out = []
            for row in r.json().get("data") or []:
                sym = row.get("symbol")
                if not sym or sym.upper() == index_name.upper():
                    continue
                if row.get("priority") == 1 and not row.get("meta"):
                    continue
                try:
                    pct = float(row.get("pChange") or 0)
                except (TypeError, ValueError):
                    continue
                meta = row.get("meta") or {}
                out.append(
                    {
                        "symbol": sym,
                        "name": meta.get("companyName") or sym,
                        "pct": pct,
                        "ltp": row.get("lastPrice") or row.get("last"),
                        "change": row.get("change"),
                    }
                )
            out.sort(key=lambda x: x["pct"])
            return out

        low_stocks = stocks(lowest_sector["index"])
        high_stocks = stocks(highest_sector["index"])

        print("\nLowest sector:", lowest_sector)
        print("Bottom stocks:")
        for s in low_stocks[:8]:
            print(" ", s)
        print("\nHighest sector:", highest_sector)
        print("Top stocks:")
        for s in reversed(high_stocks[-8:]):
            print(" ", s)

        pick_low = None
        for s in low_stocks:
            if s["pct"] >= -MAX_PCT:
                pick_low = s
                break
        pick_high = None
        for s in reversed(high_stocks):
            if s["pct"] <= MAX_PCT:
                pick_high = s
                break

        print("\n=== PICKS (<=3% move) ===")
        print("LOWEST stock:", pick_low)
        print("HIGHEST stock:", pick_high)


if __name__ == "__main__":
    main()
