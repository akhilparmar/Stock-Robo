"""SQLite store for the Nifty index tester. Not the stock-option trades database."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from index_tester.config import get_settings

IST = ZoneInfo("Asia/Kolkata")
_lock = threading.RLock()


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> str:
    return now_ist().date().isoformat()


def _path() -> Path:
    path = Path(get_settings().index_tester_db)
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
                last_error TEXT,
                last_bar TEXT,
                snapshot_json TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                side TEXT NOT NULL,
                strike REAL NOT NULL,
                expiry TEXT NOT NULL,
                trd_symbol TEXT NOT NULL,
                token TEXT NOT NULL,
                exchange_segment TEXT NOT NULL,
                lot_size INTEGER NOT NULL,
                buy_premium REAL NOT NULL,
                sell_premium REAL,
                debit REAL NOT NULL,
                index_at_buy REAL,
                index_at_sell REAL,
                index_stop REAL,
                index_target REAL,
                risk_distance REAL,
                reward_ratio REAL,
                market_regime TEXT,
                breakeven_moved INTEGER NOT NULL DEFAULT 0,
                trail_stage INTEGER NOT NULL DEFAULT 0,
                entry_reason TEXT,
                exit_reason TEXT,
                status TEXT NOT NULL,
                pnl REAL,
                opened_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bar_ts TEXT,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                reason_text TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(state)").fetchall()}
        if "paused" not in cols:
            conn.execute("ALTER TABLE state ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
        if "paused_date" not in cols:
            conn.execute("ALTER TABLE state ADD COLUMN paused_date TEXT")
        trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        trade_additions = {
            "index_target": "REAL",
            "risk_distance": "REAL",
            "reward_ratio": "REAL",
            "market_regime": "TEXT",
            "breakeven_moved": "INTEGER NOT NULL DEFAULT 0",
            "trail_stage": "INTEGER NOT NULL DEFAULT 0",
            "peak_premium": "REAL",
            "premium_stop": "REAL",
        }
        for name, sql_type in trade_additions.items():
            if name not in trade_cols:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}")
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


def enabled() -> bool:
    with _lock, connect() as conn:
        row = conn.execute("SELECT enabled FROM state WHERE id = 1").fetchone()
    return bool(row and row["enabled"])


def set_enabled(value: bool) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE state SET enabled = ?, updated_at = ? WHERE id = 1",
            (1 if value else 0, now_ist().isoformat(timespec="seconds")),
        )


def paused_today() -> bool:
    """Stop pauses paper auto for the rest of today only."""
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT paused, paused_date FROM state WHERE id = 1"
        ).fetchone()
    if not row or not row["paused"]:
        return False
    return str(row["paused_date"] or "") == today_ist()


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


def last_bar() -> str | None:
    with _lock, connect() as conn:
        row = conn.execute("SELECT last_bar FROM state WHERE id = 1").fetchone()
    if not row:
        return None
    return row["last_bar"]


def set_last_bar(bar_ts: str | None) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE state SET last_bar = ?, updated_at = ? WHERE id = 1",
            (bar_ts, now_ist().isoformat(timespec="seconds")),
        )


def save_snapshot(payload: str) -> None:
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE state SET snapshot_json = ?, updated_at = ? WHERE id = 1",
            (payload, now_ist().isoformat(timespec="seconds")),
        )


def load_snapshot() -> str | None:
    with _lock, connect() as conn:
        row = conn.execute("SELECT snapshot_json, last_error FROM state WHERE id = 1").fetchone()
    if not row:
        return None
    return row["snapshot_json"]


def state_row() -> dict[str, Any]:
    with _lock, connect() as conn:
        row = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
    return _row(row) or {}


def open_trade() -> dict[str, Any] | None:
    with _lock, connect() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _row(row)


def side_used(trade_date: str, side: str) -> bool:
    with _lock, connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM trades
            WHERE trade_date = ? AND side = ? AND status IN ('open', 'closed')
            LIMIT 1
            """,
            (trade_date, side),
        ).fetchone()
    return row is not None


def insert_trade(payload: dict[str, Any]) -> dict[str, Any]:
    with _lock, connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                trade_date, side, strike, expiry, trd_symbol, token, exchange_segment,
                lot_size, buy_premium, debit, index_at_buy, index_stop, index_target,
                risk_distance, reward_ratio, market_regime, entry_reason, status, opened_at,
                peak_premium, premium_stop, trail_stage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 0)
            """,
            (
                payload["trade_date"],
                payload["side"],
                payload["strike"],
                payload["expiry"],
                payload["trd_symbol"],
                payload["token"],
                payload["exchange_segment"],
                payload["lot_size"],
                payload["buy_premium"],
                payload["debit"],
                payload.get("index_at_buy"),
                payload.get("index_stop"),
                payload.get("index_target"),
                payload.get("risk_distance"),
                payload.get("reward_ratio"),
                payload.get("market_regime"),
                payload.get("entry_reason"),
                payload["opened_at"],
                payload.get("peak_premium", payload["buy_premium"]),
                payload.get("premium_stop"),
            ),
        )
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def tighten_trailing_stop(
    trade_id: int,
    *,
    stop: float,
    stage: int,
) -> dict[str, Any] | None:
    """Legacy index-stop trail (unused by live premium trail)."""
    with _lock, connect() as conn:
        conn.execute(
            """
            UPDATE trades
            SET index_stop = ?, breakeven_moved = 1, trail_stage = ?
            WHERE id = ? AND status = 'open' AND COALESCE(trail_stage, 0) < ?
            """,
            (stop, stage, trade_id, stage),
        )
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return _row(row)


def update_premium_trail(
    trade_id: int,
    *,
    peak_premium: float,
    premium_stop: float | None,
    stage: int,
) -> dict[str, Any] | None:
    with _lock, connect() as conn:
        conn.execute(
            """
            UPDATE trades
            SET peak_premium = ?,
                premium_stop = ?,
                trail_stage = ?,
                breakeven_moved = CASE WHEN ? >= 1 THEN 1 ELSE breakeven_moved END
            WHERE id = ? AND status = 'open'
              AND (
                COALESCE(peak_premium, 0) < ?
                OR COALESCE(premium_stop, 0) < COALESCE(?, 0)
                OR COALESCE(trail_stage, 0) < ?
              )
            """,
            (
                peak_premium,
                premium_stop,
                stage,
                stage,
                trade_id,
                peak_premium,
                premium_stop,
                stage,
            ),
        )
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return _row(row)


def close_trade(
    trade_id: int,
    *,
    sell_premium: float,
    index_at_sell: float | None,
    exit_reason: str,
    pnl: float,
) -> dict[str, Any] | None:
    closed_at = now_ist().isoformat(timespec="seconds")
    with _lock, connect() as conn:
        conn.execute(
            """
            UPDATE trades
            SET sell_premium = ?, index_at_sell = ?, exit_reason = ?, pnl = ?,
                status = 'closed', closed_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (sell_premium, index_at_sell, exit_reason, pnl, closed_at, trade_id),
        )
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return _row(row)


def list_trades(limit: int = 30) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def score() -> dict[str, Any]:
    with _lock, connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                COALESCE(SUM(pnl), 0) AS net_pnl
            FROM trades
            WHERE status = 'closed'
            """
        ).fetchone()
    trades = int(row["trades"] or 0)
    wins = int(row["wins"] or 0)
    return {
        "trades": trades,
        "wins": wins,
        "win_rate": round(wins / trades, 4) if trades else None,
        "net_pnl": round(float(row["net_pnl"] or 0), 2),
    }


def insert_signal(bar_ts: str | None, action: str, reason: str, reason_text: str) -> None:
    with _lock, connect() as conn:
        conn.execute(
            """
            INSERT INTO signals (bar_ts, action, reason, reason_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (bar_ts, action, reason, reason_text, now_ist().isoformat(timespec="seconds")),
        )
        conn.execute(
            """
            DELETE FROM signals
            WHERE id NOT IN (SELECT id FROM signals ORDER BY id DESC LIMIT 200)
            """
        )


def list_signals(limit: int = 12) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
