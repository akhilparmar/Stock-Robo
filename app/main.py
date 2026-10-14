from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import db
from app.backtest import backtest_resolved_option
from app.config import get_settings
from app.neo_client import NeoError, neo_client
from app.auto_live import AutoLiveError, auto_live_paper_buy, auto_status, set_auto_enabled
from app.nse_heatmap import NseHeatmapError, suggest_extreme_stocks
from app.option_name import parse_option_name
from app.paper_broker import app_state, close_incomplete_exits, execute_paper_buy, execute_paper_sell, resume_pending_priority
from app.scheduler import next_run_times, shutdown_scheduler, start_scheduler
from app.slots import DEFAULT_SLOT, SLOTS, all_slots, is_immediate, normalize_slot, slot_meta
from index_tester.app import app as nifty_app
from index_tester.runtime import shutdown_index_tester, startup_index_tester
from oi_spurts.app import app as oi_spurts_app
from oi_spurts.runtime import shutdown as shutdown_oi_spurts
from oi_spurts.runtime import startup as startup_oi_spurts

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    set_auto_enabled(True)
    # Never forget overnight opens: reopen orphans, then try live exit if market hours.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    now_m = now.hour * 60 + now.minute
    market_open = 9 * 60 <= now_m <= 15 * 60 + 30
    for slot_id in SLOTS:
        n = db.reopen_incomplete_exits(slot_id)
        pending = db.list_incomplete_exits(slot_id)
        if pending:
            logger.info(
                "Startup: %s pending exit(s) on %s (reopened=%s) — will finish same stock first",
                len(pending),
                slot_id,
                n,
            )
        if market_open and pending:
            resume = resume_pending_priority(slot_id)
            for action in resume.get("actions") or []:
                if action.get("ok"):
                    logger.info("Startup closed carry: %s", action.get("trade") or action)
            # After EOD only, allow flat so nothing is abandoned forever.
            if now_m >= 15 * 60 + 10:
                for result in close_incomplete_exits(slot_id, allow_flat=True):
                    if result.get("ok"):
                        logger.info("Startup EOD flat/close: %s", result.get("trade") or result)
    start_scheduler()
    startup_index_tester()
    startup_oi_spurts()
    logger.info("Paper option tester ready (paper_mode=%s)", get_settings().paper_mode)
    yield
    shutdown_oi_spurts()
    shutdown_index_tester()
    shutdown_scheduler()


app = FastAPI(title="Neo Paper Option Tester", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/nifty", nifty_app)
app.mount("/oi-spurts", oi_spurts_app)


@app.get("/nifty", include_in_schema=False)
def nifty_page() -> RedirectResponse:
    return RedirectResponse("/nifty/")


@app.get("/oi-spurts", include_in_schema=False)
def oi_spurts_page() -> RedirectResponse:
    return RedirectResponse("/oi-spurts/")


class ConnectBody(BaseModel):
    totp: str = Field(min_length=4, max_length=10)
    mpin: str = Field(min_length=4, max_length=10)


class ResolveBody(BaseModel):
    """Prefer option_name like 'WIPRO call 160'. Expiry/qty optional (auto)."""

    option_name: str | None = None
    stock: str | None = None
    option_type: str | None = None
    strike: float | None = None
    expiry: str | None = None
    qty: int | None = None
    slot: str = DEFAULT_SLOT


class SlotBody(BaseModel):
    slot: str = DEFAULT_SLOT


class BacktestBody(BaseModel):
    days: int = Field(default=29, ge=1, le=29)
    qty: int = Field(default=1, ge=1)
    include_stock: bool = True
    slot: str = DEFAULT_SLOT


class AutoLiveBody(BaseModel):
    max_pct: float = Field(default=3.0, ge=0.5, le=10.0)
    force: bool = False


class AutoLiveToggleBody(BaseModel):
    enabled: bool = True


def _slot_or_400(slot: str | None) -> str:
    try:
        return normalize_slot(slot)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/suggest")
def suggest_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "suggest.html")


@app.get("/api/slots")
def slots() -> dict[str, Any]:
    return {"ok": True, "slots": all_slots(), "default": DEFAULT_SLOT}


@app.get("/api/suggest-stock")
def suggest_stock(max_pct: float = Query(default=3.0, ge=0.5, le=10.0)) -> dict[str, Any]:
    """NSE heatmap: extreme sectors → constrained extreme stocks (default ±3%)."""
    try:
        return suggest_extreme_stocks(max_pct=max_pct)
    except NseHeatmapError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — surface NSE/network failures cleanly
        logger.exception("suggest-stock failed")
        raise HTTPException(status_code=502, detail=f"NSE heatmap fetch failed: {exc}") from exc


@app.get("/api/auto-live/status")
def auto_live_status() -> dict[str, Any]:
    return {"ok": True, **auto_status()}


@app.post("/api/auto-live/toggle")
def auto_live_toggle(body: AutoLiveToggleBody) -> dict[str, Any]:
    enabled = set_auto_enabled(body.enabled)
    return {"ok": True, "enabled": enabled, **auto_status()}


@app.post("/api/auto-live")
def auto_live(body: AutoLiveBody | None = None) -> dict[str, Any]:
    """
    Live tab: heatmap + best TA score → ATM call/put → paper buy now.
    """
    if not get_settings().paper_mode:
        raise HTTPException(status_code=403, detail="Disabled when not in paper mode")
    payload = body or AutoLiveBody()
    try:
        return auto_live_paper_buy(max_pct=payload.max_pct, force=payload.force)
    except AutoLiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NeoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("auto-live failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/status")
def status(slot: str = Query(default=DEFAULT_SLOT)) -> dict[str, Any]:
    slot_id = _slot_or_400(slot)
    snap = app_state.snapshot(slot_id)
    snap["schedule"] = next_run_times(slot_id)
    return snap


@app.post("/api/connect")
def connect(body: ConnectBody) -> dict[str, Any]:
    try:
        result = neo_client.connect(body.totp.strip(), body.mpin.strip())
        return {"ok": True, **result, "status": neo_client.status()}
    except NeoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/disconnect")
def disconnect() -> dict[str, Any]:
    neo_client.disconnect()
    return {"ok": True, "status": neo_client.status()}


@app.post("/api/resolve")
def resolve(body: ResolveBody) -> dict[str, Any]:
    slot_id = _slot_or_400(body.slot)
    try:
        if body.option_name:
            parsed = parse_option_name(body.option_name)
            stock = parsed["stock"]
            option_type = parsed["option_type"]
            strike = parsed["strike"]
            call_name = parsed["display"]
            option_name = body.option_name.strip()
        else:
            if not body.stock or not body.option_type or body.strike is None:
                raise ValueError("Enter option name like: WIPRO call 160")
            stock = body.stock
            option_type = body.option_type
            strike = float(body.strike)
            call_name = f"{stock.upper()} {'call' if option_type.upper() in {'CE', 'CALL', 'C'} else 'put'} {strike:g}"
            option_name = call_name

        pair = neo_client.resolve_pair(stock, option_type, strike, expiry=body.expiry)
        lot = int(pair["option"].get("lot_size") or 1)
        qty = max(1, int(body.qty)) if body.qty is not None else max(1, lot)
        app_state.set_resolved(pair, qty=qty, slot=slot_id, option_name=option_name)
        quotes = None
        try:
            quotes = neo_client.get_ltps(pair["stock"], pair["option"])
            app_state.set_quotes(quotes, slot_id)
        except NeoError as qe:
            app_state.set_error(str(qe), slot_id)

        trade = None
        buy_result = None
        armed = False
        # Live (t0920) is auto-only — no resolve/button buys. Timed slot still arms for 09:40.
        if is_immediate(slot_id):
            raise HTTPException(
                status_code=400,
                detail="Live tab is auto-only. Background scan buys and sells — no button buy.",
            )
        try:
            trade = app_state.arm(slot_id)
            armed = bool(trade and trade.get("status") == "armed")
        except ValueError as arm_err:
            app_state.set_error(str(arm_err), slot_id)
            raise HTTPException(status_code=400, detail=str(arm_err)) from arm_err

        meta = slot_meta(slot_id)
        return {
            "ok": True,
            "slot": slot_id,
            "entry_mode": meta.get("entry_mode"),
            "buy_time": meta.get("buy_time"),
            "exit_pct": meta.get("exit_pct"),
            "exit_mode": f"+/-{meta.get('exit_pct'):g}%",
            "call_name": call_name,
            "resolved": pair,
            "quotes": quotes,
            "qty": qty,
            "lot_size": lot,
            "expiry": pair["option"]["expiry"],
            "armed": armed,
            "bought": bool(trade and trade.get("status") == "bought"),
            "trade": trade,
            "buy_result": buy_result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NeoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/quotes")
def quotes(slot: str = Query(default=DEFAULT_SLOT)) -> dict[str, Any]:
    slot_id = _slot_or_400(slot)
    resolved = app_state.get(slot_id).resolved
    if not resolved:
        raise HTTPException(status_code=400, detail="Resolve an option first")
    try:
        data = neo_client.get_ltps(resolved["stock"], resolved["option"])
        app_state.set_quotes(data, slot_id)
        return {"ok": True, "slot": slot_id, **data}
    except NeoError as exc:
        app_state.set_error(str(exc), slot_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/arm")
def arm(body: SlotBody | None = None) -> dict[str, Any]:
    slot_id = _slot_or_400((body.slot if body else None) or DEFAULT_SLOT)
    try:
        trade = app_state.arm(slot_id)
        return {"ok": True, "slot": slot_id, "trade": trade}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/disarm")
def disarm(body: SlotBody | None = None) -> dict[str, Any]:
    slot_id = _slot_or_400((body.slot if body else None) or DEFAULT_SLOT)
    try:
        trade = app_state.disarm(slot_id)
        return {"ok": True, "slot": slot_id, "trade": trade}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/history")
def history(slot: str = Query(default=DEFAULT_SLOT)) -> dict[str, Any]:
    slot_id = _slot_or_400(slot)
    trades = db.list_trades(40, slot_id)
    return {
        "ok": True,
        "slot": slot_id,
        "trades": trades,
        "total_pnl": db.total_pnl(slot_id),
        "today_pnl": db.today_pnl(slot_id),
        "trade_date": db.today_ist(),
    }


@app.post("/api/backtest")
def run_backtest(body: BacktestBody) -> dict[str, Any]:
    slot_id = _slot_or_400(body.slot)
    resolved = app_state.get(slot_id).resolved
    if not resolved:
        raise HTTPException(status_code=400, detail="Resolve an option first")
    try:
        return backtest_resolved_option(
            neo_client,
            resolved,
            days=body.days,
            qty=body.qty or app_state.get(slot_id).qty,
            include_stock=body.include_stock,
        )
    except NeoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/test/buy-now")
def test_buy_now(body: SlotBody | None = None) -> dict[str, Any]:
    if not get_settings().paper_mode:
        raise HTTPException(status_code=403, detail="Disabled when not in paper mode")
    slot_id = _slot_or_400((body.slot if body else None) or DEFAULT_SLOT)
    if is_immediate(slot_id):
        raise HTTPException(
            status_code=400,
            detail="Live tab is auto-only — use background auto-scan, not manual buy.",
        )
    return execute_paper_buy(slot_id)


@app.post("/api/test/sell-now")
def test_sell_now(body: SlotBody | None = None) -> dict[str, Any]:
    if not get_settings().paper_mode:
        raise HTTPException(status_code=403, detail="Disabled when not in paper mode")
    slot_id = _slot_or_400((body.slot if body else None) or DEFAULT_SLOT)
    return execute_paper_sell(slot_id)


@app.post("/api/test/reset-today")
def reset_today(body: SlotBody | None = None) -> dict[str, Any]:
    if not get_settings().paper_mode:
        raise HTTPException(status_code=403, detail="Disabled when not in paper mode")
    slot_id = _slot_or_400((body.slot if body else None) or DEFAULT_SLOT)
    trade_date = db.today_ist()
    cleared = db.delete_trades_for_date(trade_date, slot_id)
    app_state.get(slot_id).armed = False
    return {"ok": True, "slot": slot_id, "cleared": trade_date, "rows": cleared}
