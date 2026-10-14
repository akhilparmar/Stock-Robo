"""SQLite store for the OI-spurts paper tester. Not shared with the other apps."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from oi_spurts.config import get_settings

IST = ZoneInfo("Asia/Kolkata")
_lock = threading.RLock()


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> str:
    return now_ist().date().isoformat()


def _path() -> Path:
    path = Path(get_settings().oi_spurts_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                paused_date TEXT,
                last_error TEXT,
                scan_date TEXT,
                scan_status TEXT,
                snapshot_json TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                band TEXT NOT NULL,
                option_type TEXT NOT NULL,
                stock_pct REAL,
                oi_pct REAL,
                strike REAL NOT NULL,
                expiry TEXT NOT NULL,
                trd_symbol TEXT NOT NULL,
                option_token TEXT NOT NULL,
                option_segment TEXT NOT NULL,
                stock_token TEXT NOT NULL,
                stock_segment TEXT NOT NULL,
                lot_size INTEGER NOT NULL,
                buy_premium REAL NOT NULL,
                sell_premium REAL,
                debit REAL NOT NULL,
                stock_entry REAL NOT NULL,
                stock_exit REAL,
                candle_open REAL NOT NULL,
                candle_close REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                risk_distance REAL NOT NULL,
                trail_stage INTEGER NOT NULL DEFAULT 0,
                best_price REAL NOT NULL,
                mark_premium REAL,
                status TEXT NOT NULL,
                exit_reason TEXT,
                pnl REAL,
                opened_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                symbol TEXT,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        row = conn.execute("SELECT id FROM state WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO state (id, enabled, paused, updated_at) VALUES (1, 0, 0, ?)",
                (now_ist().isoformat(timespec="seconds"),),
            )


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def state_row() -> dict[str, Any]:
    with _lock, connect() as conn:
        row = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
    return dict(row) if row else {}


def enabled() -> bool:
    return bool(state_row().get("enabled"))


def set_enabled(value: bool) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE state SET enabled = ?, updated_at = ? WHERE id = 1",
            (1 if value else 0, now_ist().isoformat(timespec="seconds")),
        )


def paused_today() -> bool:
    row = state_row()
    if not row.get("paused"):
        return False
    return str(row.get("paused_date") or "") == today_ist()


def set_paused(value: bool) -> None:
    with _lock, connect() as conn:
        conn.execute(
            """
            UPDATE state
            SET paused = ?, paused_date = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                1 if value else 0,
                today_ist() if value else None,
                now_ist().isoformat(timespec="seconds"),
            ),
        )


def set_error(message: str | None) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE state SET last_error = ?, updated_at = ? WHERE id = 1",
            (message, now_ist().isoformat(timespec="seconds")),
        )


def save_snapshot(payload: str) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE state SET snapshot_json = ?, updated_at = ? WHERE id = 1",
            (payload, now_ist().isoformat(timespec="seconds")),
        )


def entry_closed_today() -> bool:
    row = state_row()
    return str(row.get("scan_date") or "") == today_ist() and row.get("scan_status") == "closed"


def mark_scan_closed() -> None:
    with _lock, connect() as conn:
        conn.execute(
            """
            UPDATE state
            SET scan_date = ?, scan_status = ?, updated_at = ?
            WHERE id = 1
            """,
            (today_ist(), "closed", now_ist().isoformat(timespec="seconds")),
        )


def traded_today(symbol: str) -> bool:
    with _lock, connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM trades
            WHERE trade_date = ? AND symbol = ?
            LIMIT 1
            """,
            (today_ist(), symbol.upper()),
        ).fetchone()
    return row is not None


def trades_today_count() -> int:
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE trade_date = ?",
            (today_ist(),),
        ).fetchone()
    return int(row["n"] or 0) if row else 0


def clear_today_paper() -> int:
    """Remove today's paper trades so a same-day redo can start clean."""
    day = today_ist()
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE trade_date = ?",
            (day,),
        ).fetchone()
        removed = int(row["n"] or 0) if row else 0
        conn.execute("DELETE FROM trades WHERE trade_date = ?", (day,))
        conn.execute(
            """
            DELETE FROM signals
            WHERE trade_date = ?
              AND reason IN ('skipped', 'waiting', 'entered', 'paper_today', 'stock_stop',
                             'trailed_stop', 'clock', 'stopped', 'stale_open', 'redo_today')
            """,
            (day,),
        )
        conn.execute(
            """
            UPDATE state
            SET scan_status = NULL, scan_date = NULL, last_error = NULL, updated_at = ?
            WHERE id = 1
            """,
            (now_ist().isoformat(timespec="seconds"),),
        )
    return removed


def open_trades() -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def insert_trade(fields: dict[str, Any]) -> int:
    columns = list(fields)
    holders = ", ".join("?" for _ in columns)
    names = ", ".join(columns)
    with _lock, connect() as conn:
        cur = conn.execute(
            f"INSERT INTO trades ({names}) VALUES ({holders})",
            [fields[name] for name in columns],
        )
        return int(cur.lastrowid)


def update_levels(
    trade_id: int,
    *,
    stop_price: float,
    target_price: float,
    trail_stage: int,
    best_price: float,
    mark_premium: float | None,
) -> None:
    with _lock, connect() as conn:
        conn.execute(
            """
            UPDATE trades
            SET stop_price = ?, target_price = ?, trail_stage = ?,
                best_price = ?, mark_premium = ?
            WHERE id = ?
            """,
            (stop_price, target_price, trail_stage, best_price, mark_premium, trade_id),
        )


def close_trade(
    trade_id: int,
    *,
    sell_premium: float,
    stock_exit: float,
    reason: str,
    pnl: float,
) -> None:
    with _lock, connect() as conn:
        conn.execute(
            """
            UPDATE trades
            SET status = 'closed', sell_premium = ?, stock_exit = ?,
                exit_reason = ?, pnl = ?, closed_at = ?, mark_premium = ?
            WHERE id = ?
            """,
            (
                sell_premium,
                stock_exit,
                reason,
                pnl,
                now_ist().isoformat(timespec="seconds"),
                sell_premium,
                trade_id,
            ),
        )


def list_trades(limit: int = 40) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def noted(symbol: str, reason: str) -> bool:
    with _lock, connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM signals
            WHERE trade_date = ? AND symbol = ? AND reason = ?
            LIMIT 1
            """,
            (today_ist(), symbol, reason),
        ).fetchone()
    return row is not None


def insert_signal(symbol: str | None, action: str, reason: str, detail: str) -> None:
    with _lock, connect() as conn:
        conn.execute(
            """
            INSERT INTO signals (trade_date, symbol, action, reason, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                today_ist(),
                symbol,
                action,
                reason,
                detail,
                now_ist().isoformat(timespec="seconds"),
            ),
        )


def list_signals(limit: int = 20) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def score() -> dict[str, Any]:
    day = today_ist()
    with _lock, connect() as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(debit), 0) AS debit,
                COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl ELSE 0 END), 0) AS closed_pnl,
                COALESCE(SUM(
                    CASE
                        WHEN status = 'open' AND mark_premium IS NOT NULL
                        THEN (mark_premium - buy_premium) * lot_size
                        ELSE 0
                    END
                ), 0) AS open_pnl,
                COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) AS open_count,
                COALESCE(SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END), 0) AS closed_count
            FROM trades
            WHERE trade_date = ?
            """,
            (day,),
        ).fetchone()
    debit = float(row["debit"] or 0)
    closed_pnl = float(row["closed_pnl"] or 0)
    open_pnl = float(row["open_pnl"] or 0)
    today_pnl = round(closed_pnl + open_pnl, 2)
    roi_pct = round((today_pnl / debit) * 100.0, 2) if debit > 0 else None
    return {
        "trade_date": day,
        "debit": debit,
        "pnl": closed_pnl,
        "closed_pnl": closed_pnl,
        "open_pnl": round(open_pnl, 2),
        "today_pnl": today_pnl,
        "roi_pct": roi_pct,
        "open": int(row["open_count"] or 0),
        "closed": int(row["closed_count"] or 0),
    }
