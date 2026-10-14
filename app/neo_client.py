from __future__ import annotations

import io
import threading
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from app.config import Settings, get_settings
from neo_session import clear_session, load_session, save_session

IST = ZoneInfo("Asia/Kolkata")


class NeoError(Exception):
    pass


class NeoClient:
    """Thin Kotak Neo REST client for auth, scrip resolve, and quotes (no order APIs)."""

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
        self._restore_session()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "has_access_token": bool(self.settings.neo_access_token),
            "mobile_set": bool(self.settings.neo_mobile),
            "ucc_set": bool(self.settings.neo_ucc),
            "base_url": self.base_url,
            "paper_mode": self.settings.paper_mode,
            "session_restored": self.session_restored,
            "session_saved_at": self.session_saved_at,
        }

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
        self.session_restored = False

    def _auth_headers(self, *, use_trading: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": self.settings.neo_access_token,
            "neo-fin-key": self.settings.neo_fin_key,
            "Content-Type": "application/json",
        }
        if use_trading:
            if not self.trading_token or not self.trading_sid:
                raise NeoError("Not connected. Complete TOTP + MPIN login first.")
            headers["Auth"] = self.trading_token
            headers["Sid"] = self.trading_sid
        return headers

    def login_totp(self, totp: str, mobile: str | None = None, ucc: str | None = None) -> dict[str, Any]:
        mobile = mobile or self.settings.neo_mobile
        ucc = ucc or self.settings.neo_ucc
        if not self.settings.neo_access_token:
            raise NeoError("NEO_ACCESS_TOKEN missing in .env")
        if not mobile or not ucc:
            raise NeoError("NEO_MOBILE and NEO_UCC must be set in .env")
        if not totp:
            raise NeoError("TOTP is required")

        url = f"{self.settings.login_base}/login/1.0/tradeApiLogin"
        payload = {"mobileNumber": mobile, "ucc": ucc, "totp": totp}
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=self._auth_headers(), json=payload)
        data = self._parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(self._err_message(data, resp.status_code))
        inner = data.get("data") or {}
        with self._lock:
            self.view_token = inner.get("token")
            self.view_sid = inner.get("sid")
            self.connected = False
        if not self.view_token or not self.view_sid:
            raise NeoError("Login succeeded but token/sid missing in response")
        return {"ok": True, "kType": inner.get("kType"), "ucc": inner.get("ucc")}

    def validate_mpin(self, mpin: str) -> dict[str, Any]:
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
        data = self._parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(self._err_message(data, resp.status_code))
        inner = data.get("data") or {}
        with self._lock:
            self.trading_token = inner.get("token")
            self.trading_sid = inner.get("sid")
            self.base_url = (inner.get("baseUrl") or "").rstrip("/")
            self.connected = bool(self.trading_token and self.base_url)
        if not self.connected:
            raise NeoError("MPIN validate succeeded but trading session incomplete")
        self._persist_session()
        return {"ok": True, "base_url": self.base_url, "kType": inner.get("kType")}

    def connect(self, totp: str, mpin: str) -> dict[str, Any]:
        login = self.login_totp(totp)
        validate = self.validate_mpin(mpin)
        return {"ok": True, "login": login, "session": validate}

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

    def _require_base(self) -> str:
        # Quotes/scrip master work with access token; prefer session baseUrl when connected.
        if self.base_url:
            return self.base_url
        # Fallback DC used by Neo docs; session baseUrl is preferred after MPIN.
        return "https://cis.kotaksecurities.com"

    def _scrip_master_paths(self) -> list[str]:
        url = f"{self._require_base()}/script-details/1.0/masterscrip/file-paths"
        headers = {
            "Authorization": self.settings.neo_access_token,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(url, headers=headers)
        data = self._parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(self._err_message(data, resp.status_code))
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
            if segment in self._csv_cache:
                return self._csv_cache[segment]

        paths = self._scrip_master_paths()
        matches = [p for p in paths if segment.lower() in p.lower()]
        if not matches:
            raise NeoError(f"No scrip master file found for segment {segment}")
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            csv_resp = client.get(matches[0])
        if csv_resp.status_code >= 400:
            raise NeoError(f"Failed downloading scrip master for {segment}")
        df = pd.read_csv(io.StringIO(csv_resp.text))
        df = df.rename(columns=lambda x: str(x).strip())
        if segment.endswith("fo") and "pExpiryDate" in df.columns:
            # Neo FO expiry conversion (same approach as official SDK)
            if segment not in {"mcx_fo", "bse_fo"}:
                exp = pd.to_datetime(df["pExpiryDate"], unit="s", errors="coerce")
                exp = exp + pd.to_timedelta(315511200, unit="s")
                df["pExpiryDateFmt"] = exp.dt.strftime("%d%b%Y").str.upper()
            else:
                exp = pd.to_datetime(df["pExpiryDate"], unit="s", errors="coerce")
                df["pExpiryDateFmt"] = exp.dt.strftime("%d%b%Y").str.upper()
        with self._lock:
            self._csv_cache[segment] = df
        return df

    @staticmethod
    def _normalize_expiry(expiry: str) -> str:
        """Accept YYYY-MM-DD or DDMMMYYYY → DDMMMYYYY upper."""
        expiry = expiry.strip()
        if "-" in expiry and len(expiry) == 10:
            dt = datetime.strptime(expiry, "%Y-%m-%d")
            return dt.strftime("%d%b%Y").upper()
        return expiry.upper()

    def resolve_stock(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        df = self._load_segment_csv("nse_cm")
        mask = df["pSymbolName"].astype(str).str.upper().str.strip() == symbol
        rows = df[mask]
        if rows.empty:
            mask = df["pSymbolName"].astype(str).str.upper().str.contains(symbol, na=False)
            rows = df[mask]
        eq = rows[rows["pTrdSymbol"].astype(str).str.upper().str.endswith("-EQ")]
        if eq.empty:
            eq = rows
        if eq.empty:
            raise NeoError(f"Stock not found: {symbol}")
        row = eq.iloc[0]
        return {
            "symbol": str(row.get("pSymbolName", symbol)).upper(),
            "trd_symbol": str(row.get("pTrdSymbol", "")),
            "token": str(int(row["pSymbol"])) if pd.notna(row.get("pSymbol")) else "",
            "exchange_segment": "nse_cm",
            "lot_size": int(row.get("iLotSize") or row.get("lLotSize") or 1),
        }

    def optionable_equity_symbols(self) -> set[str]:
        """Underlying symbols that have NSE FO call/put contracts (from Neo scrip master)."""
        df = self._load_segment_csv("nse_fo")
        work = df
        if "pOptionType" in work.columns:
            ot = work["pOptionType"].astype(str).str.upper().str.strip()
            work = work[ot.isin({"CE", "PE", "CALL", "PUT", "C", "P"})]
        if work.empty:
            return set()
        names = work["pSymbolName"].astype(str).str.upper().str.strip()
        return {n for n in names.tolist() if n and n != "NAN"}

    def resolve_option(
        self,
        symbol: str,
        option_type: str,
        strike: float,
        expiry: str | None = None,
    ) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        option_type = option_type.strip().upper()
        if option_type in {"PUT", "P"}:
            option_type = "PE"
        if option_type in {"CALL", "C"}:
            option_type = "CE"
        if option_type not in {"CE", "PE"}:
            raise NeoError("option_type must be CE/PE or Call/Put")

        df = self._load_segment_csv("nse_fo")
        work = df.copy()
        work = work[work["pSymbolName"].astype(str).str.upper().str.strip() == symbol]
        if "pOptionType" in work.columns:
            work = work[work["pOptionType"].astype(str).str.upper() == option_type]
        strike_col = "dStrikePrice;" if "dStrikePrice;" in work.columns else None
        if strike_col:
            target = float(strike) * 100.0
            work = work[work[strike_col].astype(float) == target]

        if work.empty:
            raise NeoError(
                f"No contracts found for {symbol} {option_type} {strike}. "
                "Check name/strike (example: WIPRO call 160)."
            )

        if expiry:
            expiry_fmt = self._normalize_expiry(expiry)
            if "pExpiryDateFmt" in work.columns:
                work = work[work["pExpiryDateFmt"].astype(str).str.upper() == expiry_fmt]
            if work.empty:
                raise NeoError(
                    f"Option not found: {symbol} {option_type} {strike} expiry {expiry_fmt}."
                )
        else:
            expiry_fmt = self._nearest_expiry_from_frame(work)
            work = work[work["pExpiryDateFmt"].astype(str).str.upper() == expiry_fmt]
            if work.empty:
                raise NeoError(f"Could not lock nearest expiry for {symbol} {option_type} {strike}")

        row = work.iloc[0]
        lot_size = int(row.get("iLotSize") or row.get("lLotSize") or 1)
        if lot_size <= 0:
            lot_size = 1
        return {
            "symbol": symbol,
            "option_type": option_type,
            "strike": float(strike),
            "expiry": expiry_fmt,
            "trd_symbol": str(row.get("pTrdSymbol", "")),
            "token": str(int(row["pSymbol"])) if pd.notna(row.get("pSymbol")) else "",
            "exchange_segment": "nse_fo",
            "lot_size": lot_size,
            "call_name": f"{symbol} {'call' if option_type == 'CE' else 'put'} {strike:g}",
        }

    def _nearest_expiry_from_frame(self, work: pd.DataFrame) -> str:
        if "pExpiryDateFmt" not in work.columns:
            raise NeoError("Expiry column missing in FO scrip master")
        today = datetime.now(IST).date()
        candidates: list[tuple[Any, str]] = []
        for raw in work["pExpiryDateFmt"].dropna().unique():
            text = str(raw).strip().upper()
            try:
                exp_date = datetime.strptime(text, "%d%b%Y").date()
            except ValueError:
                continue
            if exp_date >= today:
                candidates.append((exp_date, text))
        if not candidates:
            # Fall back to latest available if all look expired/unparsed
            for raw in work["pExpiryDateFmt"].dropna().unique():
                text = str(raw).strip().upper()
                try:
                    exp_date = datetime.strptime(text, "%d%b%Y").date()
                except ValueError:
                    continue
                candidates.append((exp_date, text))
        if not candidates:
            raise NeoError("No valid expiry dates found for this option")
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def resolve_pair(
        self,
        symbol: str,
        option_type: str,
        strike: float,
        expiry: str | None = None,
    ) -> dict[str, Any]:
        stock = self.resolve_stock(symbol)
        option = self.resolve_option(symbol, option_type, strike, expiry)
        return {"stock": stock, "option": option}

    def get_stock_ltp(self, stock: dict[str, Any]) -> float:
        raw = self.quotes(
            [{"exchange_segment": stock["exchange_segment"], "instrument_token": stock["token"]}],
            quote_type="ltp",
        )
        parsed = self._extract_ltps(raw)
        ltp = parsed.get(stock["token"])
        if ltp is None:
            raise NeoError(f"Stock LTP unavailable for {stock.get('trd_symbol') or stock.get('symbol')}")
        return float(ltp)

    def resolve_atm_pair(self, symbol: str, option_type: str) -> dict[str, Any]:
        """Nearest expiry + strike closest to live spot for CE/PE."""
        symbol = symbol.strip().upper()
        option_type = option_type.strip().upper()
        if option_type in {"PUT", "P"}:
            option_type = "PE"
        if option_type in {"CALL", "C"}:
            option_type = "CE"
        if option_type not in {"CE", "PE"}:
            raise NeoError("option_type must be CE/PE")

        stock = self.resolve_stock(symbol)
        spot = self.get_stock_ltp(stock)

        df = self._load_segment_csv("nse_fo")
        work = df.copy()
        work = work[work["pSymbolName"].astype(str).str.upper().str.strip() == symbol]
        if "pOptionType" in work.columns:
            work = work[work["pOptionType"].astype(str).str.upper() == option_type]
        if work.empty:
            raise NeoError(f"No {option_type} contracts for {symbol}")

        expiry_fmt = self._nearest_expiry_from_frame(work)
        work = work[work["pExpiryDateFmt"].astype(str).str.upper() == expiry_fmt]
        strike_col = "dStrikePrice;" if "dStrikePrice;" in work.columns else None
        if not strike_col or work.empty:
            raise NeoError(f"No strikes for {symbol} {option_type} {expiry_fmt}")

        strikes = (work[strike_col].astype(float) / 100.0).tolist()
        if not strikes:
            raise NeoError(f"Empty strike list for {symbol}")
        atm_strike = min(strikes, key=lambda s: abs(float(s) - spot))
        option = self.resolve_option(symbol, option_type, float(atm_strike), expiry_fmt)
        return {
            "stock": stock,
            "option": option,
            "spot": spot,
            "atm_strike": float(atm_strike),
            "expiry": expiry_fmt,
        }

    def quotes(self, instruments: list[dict[str, str]], quote_type: str = "ltp") -> dict[str, Any]:
        if not instruments:
            raise NeoError("No instruments for quotes")
        neo_symbol_str = ",".join(f"{i['exchange_segment']}|{i['instrument_token']}" for i in instruments)
        encoded = quote(neo_symbol_str, safe="")
        url = f"{self._require_base()}/script-details/1.0/quotes/neosymbol/{encoded}/{quote_type}"
        headers = {
            "Authorization": self.settings.neo_access_token,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers=headers)
        data = self._parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(self._err_message(data, resp.status_code))
        return data

    def get_ltps(self, stock: dict[str, Any], option: dict[str, Any]) -> dict[str, Any]:
        instruments = [
            {"exchange_segment": stock["exchange_segment"], "instrument_token": stock["token"]},
            {"exchange_segment": option["exchange_segment"], "instrument_token": option["token"]},
        ]
        raw = self.quotes(instruments, quote_type="ltp")
        parsed = self._extract_ltps(raw)
        stock_ltp = parsed.get(stock["token"])
        option_ltp = parsed.get(option["token"])
        return {
            "stock": {
                "token": stock["token"],
                "trd_symbol": stock["trd_symbol"],
                "ltp": stock_ltp,
            },
            "option": {
                "token": option["token"],
                "trd_symbol": option["trd_symbol"],
                "ltp": option_ltp,
            },
            "updated_at": datetime.now(IST).isoformat(timespec="seconds"),
            "raw_keys": list(parsed.keys()),
        }

    def historical_candles(
        self,
        *,
        exchange_segment: str,
        instrument_token: str,
        fromdate: str,
        todate: str,
        interval: str = "1min",
    ) -> list[list[Any]]:
        neo_symbol = quote(f"{exchange_segment}|{instrument_token}", safe="")
        url = (
            f"{self._require_base()}/market-data/1.0/historical/details"
            f"?neosymbol={neo_symbol}&fromdate={fromdate}&todate={todate}&interval={interval}"
        )
        headers = {
            "Authorization": self.settings.neo_access_token,
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=120.0) as client:
            resp = client.get(url, headers=headers)
        data = self._parse_json(resp)
        if resp.status_code >= 400:
            raise NeoError(self._err_message(data, resp.status_code))
        if isinstance(data, dict) and str(data.get("status", "")).upper() == "ERROR":
            fault = data.get("fault") or {}
            raise NeoError(fault.get("message") or data.get("message") or "Historical API error")
        payload = data.get("data") if isinstance(data, dict) else None
        candles = (payload or {}).get("candles") if isinstance(payload, dict) else None
        if candles is None and isinstance(data, dict):
            candles = data.get("candles")
        if not isinstance(candles, list):
            raise NeoError("No candles returned from historical API")
        return candles

    def _extract_ltps(self, raw: Any) -> dict[str, float]:
        out: dict[str, float] = {}

        def maybe_add(token: Any, ltp: Any) -> None:
            if token is None or ltp is None or ltp == "":
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
                for k, v in data.items():
                    if isinstance(v, dict):
                        maybe_add(
                            v.get("tk")
                            or v.get("instrument_token")
                            or v.get("exchange_token")
                            or k,
                            v.get("ltp")
                            or v.get("last_traded_price")
                            or v.get("last_price"),
                        )
                    else:
                        maybe_add(k, v)
                return out

        for item in items:
            if not isinstance(item, dict):
                continue
            maybe_add(
                item.get("tk")
                or item.get("instrument_token")
                or item.get("exchange_token")
                or item.get("token"),
                item.get("ltp")
                or item.get("last_traded_price")
                or item.get("last_price")
                or item.get("v"),
            )
        return out

    @staticmethod
    def _parse_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return {"error": resp.text[:500], "status_code": resp.status_code}

    @staticmethod
    def _err_message(data: Any, status: int) -> str:
        if isinstance(data, dict):
            for key in ("message", "errMsg", "error", "Error"):
                if data.get(key):
                    return str(data[key])
            if data.get("data") and isinstance(data["data"], dict) and data["data"].get("message"):
                return str(data["data"]["message"])
        return f"Neo API error HTTP {status}"


neo_client = NeoClient()
