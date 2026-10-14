from datetime import date, timedelta
from urllib.parse import quote
import json
import httpx
from app.neo_client import neo_client

stock = neo_client.resolve_stock("RELIANCE")
print("stock", stock)
base = neo_client._require_base()
todate = date.today().isoformat()
fromdate = (date.today() - timedelta(days=30)).isoformat()
neosymbol = quote(f"nse_cm|{stock['token']}", safe="")
url = (
    f"{base}/market-data/1.0/historical/details"
    f"?neosymbol={neosymbol}&fromdate={fromdate}&todate={todate}&interval=1min"
)
print("URL", url)
headers = {
    "Authorization": neo_client.settings.neo_access_token,
    "Content-Type": "application/json",
}
r = httpx.get(url, headers=headers, timeout=90)
print("status", r.status_code)
print(r.text[:2000])
