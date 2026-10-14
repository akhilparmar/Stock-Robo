"""Probe NSE APIs for sector heatmap + constituents."""
from __future__ import annotations

import json
import re
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


def main() -> None:
    with httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=True) as c:
        c.get("https://www.nseindia.com/")
        c.get("https://www.nseindia.com/market-data/live-equity-market?symbol=NIFTY%20METAL")

        paths = [
            "/api/equity-stock-indices?index=NIFTY%20METAL",
            "/api/equity-stockIndices?index=NIFTY%20METAL",
            "/api/live-equity-market?index=NIFTY%20METAL",
            "/api/equity-stock-indices?index=NIFTY%20METAL&type=equity",
        ]
        for path in paths:
            r = c.get("https://www.nseindia.com" + path)
            snippet = r.text[:180].replace("\n", " ")
            print(path, "->", r.status_code, r.headers.get("content-type"), snippet)

        page = c.get("https://www.nseindia.com/market-data/live-market-indices/heatmap").text
        apis = sorted(set(re.findall(r"/api/[A-Za-z0-9_\-/?=&%.]+", page)))
        print("\nAPIs in heatmap html:", len(apis))
        for a in apis[:50]:
            print(a)

        # Also try with params style
        for index in ["NIFTY METAL", "NIFTY MEDIA"]:
            r = c.get(
                "https://www.nseindia.com/api/equity-stock-indices",
                params={"index": index},
            )
            print("\nparams", index, r.status_code, r.text[:200].replace("\n", " "))
            if r.status_code == 200:
                data = r.json().get("data") or []
                print("rows", len(data))
                if data:
                    print(json.dumps(data[0], indent=2)[:500])
                    print(json.dumps(data[1] if len(data) > 1 else {}, indent=2)[:500])


if __name__ == "__main__":
    main()
