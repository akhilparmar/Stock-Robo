"""Kotak Neo client owned by the Nifty index tester.

Quotes, candles, and Nifty contract lookup live here so this package does not
import the stock option client.
"""
from __future__ import annotations

import io
import threading
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from index_tester.config import Settings, get_settings
from neo_session import clear_session, load_session, save_session

IST = ZoneInfo("Asia/Kolkata")


class NeoError(Exception):
    pass


class NiftyNeo:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.RLock()
        self.view_token: str | None = None
        self.view_sid: str | None = None
        self.trading_token: str | None = None
        self.trading_sid: str | None = None
        self.base_url: str | None = None
        self.connected = False
        self.session_restored = False
        self.session_saved_at: str | None = None
        self._csv_cache: dict[str, pd.DataFrame] = {}
        self._csv_cache_day: str | None = None
        self._chart: dict[str, Any] | None = None
        self._spot: dict[str, Any] | None = None
        self._restore_session()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "has_access_token": bool(self.settings.neo_access_token),
            "mobile_set": bool(self.settings.neo_mobile),
            "ucc_set": bool(self.settings.neo_ucc),
            "base_url": self.base_url,
            "paper_only": True,
            "session_restored": self.session_restored,
            "session_saved_at": self.session_saved_at,
        }

    def connect(self, totp: str, mpin: str) -> dict[str, Any]:
        login = self._login_totp(totp.strip())
        session = self._validate_mpin(mpin.strip())
        self._persist_session()
        self.session_restored = False
        return {"ok": True, "login": login, "session": session}

    def disconnect(self) -> None:
        with self._lock:
            self.view_token = None
            self.view_sid = None
            self.trading_token = None
            self.trading_sid = None
            self.base_url = None
            self.connected = False
            self.session_restored = False
            self.session_saved_at = None
        clear_session()

    def _restore_session(self) -> None:
        saved = load_session()
        if not saved:
            return
        with self._lock:
            self.view_token = saved.get("view_token")
            self.view_sid = saved.get("view_sid")
            self.trading_token = saved.get("trading_token")
            self.trading_sid = saved.get("trading_sid")
            self.base_url = (saved.get("base_url") or "").rstrip("/") or None
            self.connected = bool(self.trading_token and self.base_url)
            self.session_restored = self.connected
            self.session_saved_at = saved.get("saved_at")

    def _persist_session(self) -> None:
        save_session(
            view_token=self.view_token,
            view_sid=self.view_sid,
            trading_token=self.trading_token,
            trading_sid=self.trading_sid,
            base_url=self.base_url,
        )
        self.session_saved_at = datetime.now(IST).isoformat(timespec="seconds")

    def chart_instrument(self) -> dict[str, Any]:
        if self._chart is None:
            self._chart = self._resolve_chart()
        return self._chart

    def spot_instrument(self) -> dict[str, Any] | None:
        if self._spot is None:
            try:
                self._spot = self._resolve_spot()
            except NeoError:
                self._spot = {}
        return self._spot or None

    def resolve_option(self, side: str, strike: float) -> dict[str, Any]:
        side = "CE" if side.upper() in {"CE", "CALL", "C"} else "PE"
        df = self._load_segment_csv("nse_fo")
        work = df[df["pSymbolName"].astype(str).str.upper().str.strip() == "NIFTY"].copy()
        if "pOptionType" in work.columns:
            work = work[work["pOptionType"].astype(str).str.upper() == side]
        strike_series = _strike_series(work)
        if strike_series is None:
            raise NeoError("Nifty option strike column missing in scrip master")
        work = work.assign(_strike=strike_series)
        work = work[work["_strike"].round(2) == round(float(strike), 2)]
        if work.empty:
            raise NeoError(f"No Nifty {side} {strike:g} contract in the scrip master")
        expiry = _nearest_expiry(work)
        work = work[work["pExpiryDateFmt"].astype(str).str.upper() == expiry]
        if work.empty:
            raise NeoError(f"No nearest expiry for Nifty {side} {strike:g}")
        row = work.iloc[0]
        lot = int(row.get("lLotSize") or row.get("iLotSize") or 0)
        if lot <= 0:
            raise NeoError("Nifty lot size missing on the contract")
        return {
            "symbol": "NIFTY",
            "option_type": side,
            "strike": float(strike),
            "expiry": expiry,
            "trd_symbol": str(row.get("pTrdSymbol", "")),
            "token": _token(row),
            "exchange_segment": "nse_fo",
            "lot_size": lot,
        }

    def ltp(self, exchange_segment: str, token: str) -> float | None:
        raw = self.quotes([{"exchange_segment": exchange_segment, "instrument_token": token}])
        return self._extract_ltps(raw).get(str(token))

    def quotes(self, instruments: list[dict[str, str]]) -> dict[str, Any]:
        self._require_token()
        neo_symbol = ",".join(f"{i['exchange_segment']}|{i['instrument_token']}" for i in instruments)
        encoded = quote(neo_symbol, safe="")
        url = f"{self._base()}/script-details/1.0/quotes/neosymbol/{encoded}/ltp"
        headers = {
            "Authorization": self.settings.neo_access_token,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers=headers)
        data = _parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(_err_message(data, resp.status_code))
        return data

    def historical(self, exchange_segment: str, token: str, fromdate: str, todate: str) -> list[Any]:
        self._require_token()
        neo_symbol = quote(f"{exchange_segment}|{token}", safe="")
        url = (
            f"{self._base()}/market-data/1.0/historical/details"
            f"?neosymbol={neo_symbol}&fromdate={fromdate}&todate={todate}&interval=1min"
        )
        headers = {
            "Authorization": self.settings.neo_access_token,
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=120.0) as client:
            resp = client.get(url, headers=headers)
        data = _parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(_err_message(data, resp.status_code))
        if isinstance(data, dict) and str(data.get("status", "")).upper() == "ERROR":
            fault = data.get("fault") or {}
            raise NeoError(fault.get("message") or data.get("message") or "Historical API error")
        payload = data.get("data") if isinstance(data, dict) else None
        candles = (payload or {}).get("candles") if isinstance(payload, dict) else None
        if candles is None and isinstance(data, dict):
            candles = data.get("candles")
        if not isinstance(candles, list):
            raise NeoError("No Nifty candles returned")
        return candles

    def _resolve_chart(self) -> dict[str, Any]:
        future = self._front_future()
        if future:
            return future
        spot = self._resolve_spot()
        return spot

    def _front_future(self) -> dict[str, Any] | None:
        df = self._load_segment_csv("nse_fo")
        work = df[df["pSymbolName"].astype(str).str.upper().str.strip() == "NIFTY"].copy()
        if "pInstType" in work.columns:
            typed = work[work["pInstType"].astype(str).str.upper() == "FUTIDX"]
            if not typed.empty:
                work = typed
        elif "pOptionType" in work.columns:
            kind = work["pOptionType"].astype(str).str.upper()
            work = work[kind.isin(["", "XX", "FF", "FUT", "NAN"])]
        if work.empty:
            return None
        expiry = _nearest_expiry(work)
        work = work[work["pExpiryDateFmt"].astype(str).str.upper() == expiry]
        if work.empty:
            return None
        row = work.iloc[0]
        return {
            "name": f"NIFTY FUT {expiry}",
            "trd_symbol": str(row.get("pTrdSymbol", "")),
            "token": _token(row),
            "exchange_segment": "nse_fo",
            "kind": "future",
        }

    def _resolve_spot(self) -> dict[str, Any]:
        df = self._load_segment_csv("nse_cm")
        work = df
        if "pInstType" in df.columns:
            typed = df[df["pInstType"].astype(str).str.upper().isin(["IDX", "INDEX"])]
            if not typed.empty:
                work = typed
        name = work["pSymbolName"].astype(str).str.upper().str.strip()
        trd = work["pTrdSymbol"].astype(str).str.upper().str.strip() if "pTrdSymbol" in work.columns else name
        exact = (name.isin(["NIFTY", "NIFTY 50"])) | (trd.isin(["NIFTY", "NIFTY 50"]))
        banned = name.str.contains("BANK|FINNIFTY|MID|NXT|NEXT") | trd.str.contains("BANK|FINNIFTY|MID|NXT|NEXT")
        rows = work[exact & ~banned]
        if rows.empty:
            raise NeoError("Nifty 50 index was not found in the scrip master")
        row = rows.iloc[0]
        return {
            "name": "NIFTY 50",
            "trd_symbol": str(row.get("pTrdSymbol", "")),
            "token": _token(row),
            "exchange_segment": "nse_cm",
            "kind": "index",
        }

    def _login_totp(self, totp: str) -> dict[str, Any]:
        if not self.settings.neo_access_token:
            raise NeoError("NEO_ACCESS_TOKEN is missing from .env")
        if not self.settings.neo_mobile or not self.settings.neo_ucc:
            raise NeoError("NEO_MOBILE and NEO_UCC must be set in .env")
        if not totp:
            raise NeoError("TOTP is required")
        url = f"{self.settings.login_base}/login/1.0/tradeApiLogin"
        payload = {"mobileNumber": self.settings.neo_mobile, "ucc": self.settings.neo_ucc, "totp": totp}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=self._auth_headers(), json=payload)
        data = _parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(_err_message(data, resp.status_code))
        inner = data.get("data") or {}
        with self._lock:
            self.view_token = inner.get("token")
            self.view_sid = inner.get("sid")
            self.connected = False
        if not self.view_token or not self.view_sid:
            raise NeoError("Login succeeded but token/sid was missing")
        return {"ok": True}

    def _validate_mpin(self, mpin: str) -> dict[str, Any]:
        if not self.view_token or not self.view_sid:
            raise NeoError("Run TOTP login first")
        if not mpin:
            raise NeoError("MPIN is required")
        url = f"{self.settings.login_base}/login/1.0/tradeApiValidate"
        headers = self._auth_headers()
        headers["sid"] = self.view_sid
        headers["Auth"] = self.view_token
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=headers, json={"mpin": mpin})
        data = _parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(_err_message(data, resp.status_code))
        inner = data.get("data") or {}
        with self._lock:
            self.trading_token = inner.get("token")
            self.trading_sid = inner.get("sid")
            self.base_url = (inner.get("baseUrl") or "").rstrip("/")
            self.connected = bool(self.trading_token and self.base_url)
        if not self.connected:
            raise NeoError("MPIN validate succeeded but the trading session is incomplete")
        self._persist_session()
        return {"ok": True, "base_url": self.base_url}

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": self.settings.neo_access_token,
            "neo-fin-key": self.settings.neo_fin_key,
            "Content-Type": "application/json",
        }

    def _require_token(self) -> None:
        if not self.settings.neo_access_token:
            raise NeoError("NEO_ACCESS_TOKEN is missing from .env")

    def _base(self) -> str:
        if self.base_url:
            return self.base_url
        return "https://cis.kotaksecurities.com"

    def _scrip_paths(self) -> list[str]:
        self._require_token()
        url = f"{self._base()}/script-details/1.0/masterscrip/file-paths"
        headers = {
            "Authorization": self.settings.neo_access_token,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(url, headers=headers)
        data = _parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(_err_message(data, resp.status_code))
        paths = (data.get("data") or {}).get("filesPaths") or []
        if not paths:
            raise NeoError("No scrip master file paths returned")
        return paths

    def _load_segment_csv(self, segment: str) -> pd.DataFrame:
        today = datetime.now(IST).date().isoformat()
        with self._lock:
            if self._csv_cache_day != today:
                self._csv_cache.clear()
                self._csv_cache_day = today
                self._chart = None
                self._spot = None
            if segment in self._csv_cache:
                return self._csv_cache[segment]
        matches = [p for p in self._scrip_paths() if segment.lower() in p.lower()]
        if not matches:
            raise NeoError(f"No scrip master file for {segment}")
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            resp = client.get(matches[0])
        if resp.status_code >= 400:
            raise NeoError(f"Failed downloading the {segment} scrip master")
        frame = pd.read_csv(io.StringIO(resp.text))
        frame = frame.rename(columns=lambda col: str(col).strip())
        if segment.endswith("fo") and "pExpiryDate" in frame.columns:
            exp = pd.to_datetime(frame["pExpiryDate"], unit="s", errors="coerce")
            exp = exp + pd.to_timedelta(315511200, unit="s")
            frame["pExpiryDateFmt"] = exp.dt.strftime("%d%b%Y").str.upper()
        with self._lock:
            self._csv_cache[segment] = frame
        return frame

    @staticmethod
    def _extract_ltps(raw: Any) -> dict[str, float]:
        out: dict[str, float] = {}

        def add(token: Any, ltp: Any) -> None:
            if token is None or ltp in (None, ""):
                return
            try:
                out[str(token)] = float(ltp)
            except (TypeError, ValueError):
                return

        items: list[Any] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            data = raw.get("data", raw)
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, dict):
                        add(
                            value.get("tk") or value.get("instrument_token") or value.get("exchange_token") or key,
                            value.get("ltp") or value.get("last_traded_price") or value.get("last_price"),
                        )
                    else:
                        add(key, value)
                return out
        for item in items:
            if not isinstance(item, dict):
                continue
            add(
                item.get("tk") or item.get("instrument_token") or item.get("exchange_token") or item.get("token"),
                item.get("ltp") or item.get("last_traded_price") or item.get("last_price") or item.get("v"),
            )
        return out


def _token(row: pd.Series) -> str:
    value = row.get("pSymbol")
    if pd.isna(value):
        raise NeoError("Instrument token missing")
    return str(int(value))


def _strike_series(frame: pd.DataFrame) -> pd.Series | None:
    column = None
    for name in frame.columns:
        key = str(name).replace(";", "").lower()
        if key in {"dstrikeprice", "pstrikeprice", "strikeprice"}:
            column = name
            break
    if column is None:
        return None
    series = pd.to_numeric(frame[column], errors="coerce")
    median = series.median(skipna=True)
    if pd.notna(median) and float(median) > 100_000:
        return series / 100.0
    return series


def _nearest_expiry(frame: pd.DataFrame) -> str:
    if "pExpiryDateFmt" not in frame.columns:
        raise NeoError("Expiry column missing in the FO scrip master")
    today = datetime.now(IST).date()
    found: list[tuple[Any, str]] = []
    for raw in frame["pExpiryDateFmt"].dropna().unique():
        text = str(raw).strip().upper()
        try:
            exp = datetime.strptime(text, "%d%b%Y").date()
        except ValueError:
            continue
        if exp >= today:
            found.append((exp, text))
    if not found:
        raise NeoError("No upcoming Nifty expiry in the scrip master")
    found.sort(key=lambda item: item[0])
    return found[0][1]


def _parse_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return {"error": resp.text[:500], "status_code": resp.status_code}


def _err_message(data: Any, status: int) -> str:
    if isinstance(data, dict):
        for key in ("message", "errMsg", "error", "Error"):
            if data.get(key):
                return str(data[key])
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])
        fault = data.get("fault")
        if isinstance(fault, dict) and fault.get("message"):
            return str(fault["message"])
    return f"Kotak request failed ({status})"


neo = NiftyNeo()
