"""Persist Kotak Neo morning session for the stock and Nifty paper pages.

Tokens live only on this PC under data/neo_session.json (gitignored).
Restored for the same IST calendar day. Connect once after market open.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_LOCK = threading.RLock()
SESSION_PATH = Path(__file__).resolve().parent / "data" / "neo_session.json"


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def load_session() -> dict[str, Any] | None:
    with _LOCK:
        if not SESSION_PATH.exists():
            return None
        try:
            payload = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("session_date") != today_ist():
        clear_session()
        return None
    if not payload.get("trading_token") or not payload.get("base_url"):
        return None
    return payload


def save_session(
    *,
    view_token: str | None,
    view_sid: str | None,
    trading_token: str | None,
    trading_sid: str | None,
    base_url: str | None,
) -> None:
    if not trading_token or not base_url:
        return
    payload = {
        "session_date": today_ist(),
        "saved_at": datetime.now(IST).isoformat(timespec="seconds"),
        "view_token": view_token,
        "view_sid": view_sid,
        "trading_token": trading_token,
        "trading_sid": trading_sid,
        "base_url": base_url,
    }
    with _LOCK:
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_session() -> None:
    with _LOCK:
        if SESSION_PATH.exists():
            try:
                SESSION_PATH.unlink()
            except OSError:
                pass
