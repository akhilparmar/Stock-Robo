from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.slots import DEFAULT_SLOT, normalize_slot

IST = ZoneInfo("Asia/Kolkata")

_TRADES_DDL = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    slot TEXT NOT NULL DEFAULT 't0920',
    stock_symbol TEXT,
    option_type TEXT,
    strike REAL,
    expiry TEXT,
    stock_token TEXT,
    stock_trd_symbol TEXT,
    option_token TEXT,
    option_trd_symbol TEXT,
    qty INTEGER DEFAULT 1,
    status TEXT NOT NULL,
    buy_price REAL,
    sell_price REAL,
    stock_ltp_at_buy REAL,
    stock_ltp_at_sell REAL,
    pnl REAL,
    exit_reason TEXT,
    fail_reason TEXT,
    armed_at TEXT,
    bought_at TEXT,
    sold_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db() -> Any:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                slot TEXT NOT NULL DEFAULT 't0920',
                stock_symbol TEXT,
                option_type TEXT,
                strike REAL,
                expiry TEXT,
                stock_token TEXT,
                stock_trd_symbol TEXT,
                option_token TEXT,
                option_trd_symbol TEXT,
                qty INTEGER DEFAULT 1,
                status TEXT NOT NULL,
                buy_price REAL,
                sell_price REAL,
                stock_ltp_at_buy REAL,
                stock_ltp_at_sell REAL,
                pnl REAL,
                exit_reason TEXT,
                fail_reason TEXT,
                armed_at TEXT,
                bought_at TEXT,
                sold_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _migrate_trades_table(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "exit_reason" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN exit_reason TEXT")
        if "trail_active" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN trail_active INTEGER NOT NULL DEFAULT 0")
        if "trail_sl_pct" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN trail_sl_pct REAL")
        if "trail_target_pct" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN trail_target_pct REAL")
        _wipe_t0920_history_once(conn)


def _table_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    return (row["sql"] or "") if row else ""


def _migrate_trades_table(conn: sqlite3.Connection) -> None:
    """Ensure slot column exists and drop UNIQUE(trade_date, slot) for multi-trade."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "slot" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN slot TEXT NOT NULL DEFAULT 't0920'")

    create_sql = _table_sql(conn)
    compact = create_sql.replace(" ", "")
    needs_rebuild = "UNIQUE(trade_date,slot)" in compact or (
        "trade_dateTEXTNOTNULLUNIQUE" in compact or "trade_dateTEXTUNIQUE" in compact
    )
    if not needs_rebuild:
        conn.execute("UPDATE trades SET slot = ? WHERE slot IS NULL OR slot = ''", (DEFAULT_SLOT,))
        return

    conn.execute("ALTER TABLE trades RENAME TO trades_legacy")
    conn.execute(_TRADES_DDL)
    legacy_cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades_legacy)").fetchall()}
    slot_expr = "COALESCE(slot, 't0920')" if "slot" in legacy_cols else "'t0920'"
    exit_expr = "exit_reason" if "exit_reason" in legacy_cols else "NULL"
    conn.execute(
        f"""
        INSERT INTO trades (
            trade_date, slot, stock_symbol, option_type, strike, expiry,
            stock_token, stock_trd_symbol, option_token, option_trd_symbol,
            qty, status, buy_price, sell_price, stock_ltp_at_buy, stock_ltp_at_sell,
            pnl, exit_reason, fail_reason, armed_at, bought_at, sold_at, created_at, updated_at
        )
        SELECT
            trade_date, {slot_expr}, stock_symbol, option_type, strike, expiry,
            stock_token, stock_trd_symbol, option_token, option_trd_symbol,
            qty, status, buy_price, sell_price, stock_ltp_at_buy, stock_ltp_at_sell,
            pnl, {exit_expr}, fail_reason, armed_at, bought_at, sold_at, created_at, updated_at
        FROM trades_legacy
        """
    )
    conn.execute("DROP TABLE trades_legacy")


def _wipe_t0920_history_once(conn: sqlite3.Connection) -> None:
    """One-time purge of old timed 09:20 rows. Never touches t0940."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", ("wiped_t0920_v1",)
    ).fetchone()
    if row:
        return
    conn.execute("DELETE FROM trades WHERE slot = ?", ("t0920",))
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?)",
        ("wiped_t0920_v1", "1"),
    )


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def get_trade(trade_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return dict(row) if row else None


def get_trade_by_date(trade_date: str, slot: str | None = None) -> dict[str, Any] | None:
    """Latest trade for date+slot (timed tab / backward compat)."""
    slot_id = normalize_slot(slot)
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM trades
            WHERE trade_date = ? AND slot = ?
            ORDER BY id DESC LIMIT 1
            """,
            (trade_date, slot_id),
        ).fetchone()
        return dict(row) if row else None


def list_open_trades(slot: str | None = None) -> list[dict[str, Any]]:
    slot_id = normalize_slot(slot)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE slot = ? AND status = 'bought'
            ORDER BY id ASC
            """,
            (slot_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_armed_trades(slot: str | None = None) -> list[dict[str, Any]]:
    slot_id = normalize_slot(slot)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE slot = ? AND status = 'armed'
            ORDER BY id ASC
            """,
            (slot_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_today_trades(slot: str | None = None) -> list[dict[str, Any]]:
    slot_id = normalize_slot(slot)
    trade_date = today_ist()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE slot = ? AND trade_date = ?
            ORDER BY id DESC
            """,
            (slot_id, trade_date),
        ).fetchall()
        return [dict(r) for r in rows]


def list_trades(limit: int = 40, slot: str | None = None) -> list[dict[str, Any]]:
    slot_id = normalize_slot(slot)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE slot = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (slot_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def total_pnl(slot: str | None = None) -> float:
    slot_id = normalize_slot(slot)
    with db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) AS total
            FROM trades
            WHERE slot = ? AND status='completed' AND pnl IS NOT NULL
            """,
            (slot_id,),
        ).fetchone()
        return float(row["total"] if row else 0)


def today_pnl(slot: str | None = None) -> float:
    """Realized P&L for trades completed today (by trade_date or sold_at date IST)."""
    slot_id = normalize_slot(slot)
    day = today_ist()
    with db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) AS total
            FROM trades
            WHERE slot = ?
              AND status = 'completed'
              AND pnl IS NOT NULL
              AND (
                trade_date = ?
                OR substr(COALESCE(sold_at, ''), 1, 10) = ?
              )
            """,
            (slot_id, day, day),
        ).fetchone()
        return float(row["total"] if row else 0)


def delete_trades_for_date(trade_date: str, slot: str | None = None) -> int:
    slot_id = normalize_slot(slot)
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM trades WHERE trade_date = ? AND slot = ?",
            (trade_date, slot_id),
        )
        return cur.rowcount


def insert_armed_trade(payload: dict[str, Any]) -> dict[str, Any]:
    """Always insert a new armed row (immediate multi-trade)."""
    slot_id = normalize_slot(payload.get("slot"))
    now = now_ist_iso()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                trade_date, slot, stock_symbol, option_type, strike, expiry,
                stock_token, stock_trd_symbol, option_token, option_trd_symbol,
                qty, status, armed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'armed', ?, ?, ?)
            """,
            (
                payload["trade_date"],
                slot_id,
                payload["stock_symbol"],
                payload["option_type"],
                payload["strike"],
                payload["expiry"],
                payload["stock_token"],
                payload["stock_trd_symbol"],
                payload["option_token"],
                payload["option_trd_symbol"],
                payload.get("qty", 1),
                now,
                now,
                now,
            ),
        )
        trade_id = int(cur.lastrowid)
    trade = get_trade(trade_id)
    assert trade is not None
    return trade


def upsert_armed_trade(payload: dict[str, Any]) -> dict[str, Any]:
    """Timed slot: one logical trade per day (upsert armed / reject if already bought)."""
    trade_date = payload["trade_date"]
    slot_id = normalize_slot(payload.get("slot"))
    existing = get_trade_by_date(trade_date, slot_id)
    now = now_ist_iso()
    if existing and existing["status"] in {"bought", "sold", "completed"}:
        raise ValueError("Today already has a trade in progress or completed for this tab.")
    with db() as conn:
        if existing:
            conn.execute(
                """
                UPDATE trades SET
                    stock_symbol=?, option_type=?, strike=?, expiry=?,
                    stock_token=?, stock_trd_symbol=?, option_token=?, option_trd_symbol=?,
                    qty=?, status='armed', fail_reason=NULL, armed_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    payload["stock_symbol"],
                    payload["option_type"],
                    payload["strike"],
                    payload["expiry"],
                    payload["stock_token"],
                    payload["stock_trd_symbol"],
                    payload["option_token"],
                    payload["option_trd_symbol"],
                    payload.get("qty", 1),
                    now,
                    now,
                    existing["id"],
                ),
            )
            trade_id = int(existing["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO trades (
                    trade_date, slot, stock_symbol, option_type, strike, expiry,
                    stock_token, stock_trd_symbol, option_token, option_trd_symbol,
                    qty, status, armed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'armed', ?, ?, ?)
                """,
                (
                    trade_date,
                    slot_id,
                    payload["stock_symbol"],
                    payload["option_type"],
                    payload["strike"],
                    payload["expiry"],
                    payload["stock_token"],
                    payload["stock_trd_symbol"],
                    payload["option_token"],
                    payload["option_trd_symbol"],
                    payload.get("qty", 1),
                    now,
                    now,
                    now,
                ),
            )
            trade_id = int(cur.lastrowid)
    trade = get_trade(trade_id)
    assert trade is not None
    return trade


def mark_bought(
    trade_id: int,
    buy_price: float,
    stock_ltp: float | None,
) -> dict[str, Any] | None:
    now = now_ist_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE trades SET status='bought', buy_price=?, stock_ltp_at_buy=?,
                bought_at=?, updated_at=?, fail_reason=NULL,
                trail_active=0, trail_sl_pct=NULL, trail_target_pct=NULL
            WHERE id=? AND status='armed'
            """,
            (buy_price, stock_ltp, now, now, trade_id),
        )
    return get_trade(trade_id)


def update_trail(
    trade_id: int,
    *,
    trail_active: bool,
    trail_sl_pct: float,
    trail_target_pct: float,
) -> dict[str, Any] | None:
    """Persist trailing ladder levels for an open bought trade."""
    now = now_ist_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE trades
            SET trail_active=?, trail_sl_pct=?, trail_target_pct=?, updated_at=?
            WHERE id=? AND status='bought'
            """,
            (
                1 if trail_active else 0,
                float(trail_sl_pct),
                float(trail_target_pct),
                now,
                trade_id,
            ),
        )
    return get_trade(trade_id)


def mark_sold(
    trade_id: int,
    sell_price: float,
    stock_ltp: float | None,
    pnl: float,
    exit_reason: str | None = None,
) -> dict[str, Any] | None:
    now = now_ist_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE trades SET status='completed', sell_price=?, stock_ltp_at_sell=?,
                pnl=?, exit_reason=?, sold_at=?, updated_at=?, fail_reason=NULL
            WHERE id=? AND status='bought'
            """,
            (sell_price, stock_ltp, pnl, exit_reason, now, now, trade_id),
        )
    return get_trade(trade_id)


def mark_failed(trade_id: int, reason: str) -> dict[str, Any] | None:
    """Fail only armed (not yet bought) rows. Never abandon an open bought position."""
    now = now_ist_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE trades SET status='failed', fail_reason=?, updated_at=?
            WHERE id=? AND status='armed'
            """,
            (reason, now, trade_id),
        )
    return get_trade(trade_id)


def note_sell_error(trade_id: int, reason: str) -> dict[str, Any] | None:
    """Keep status=bought so TP/SL/EOD can retry after a transient sell error."""
    now = now_ist_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE trades SET fail_reason=?, updated_at=?
            WHERE id=? AND status='bought'
            """,
            (reason, now, trade_id),
        )
    return get_trade(trade_id)


def list_incomplete_exits(slot: str | None = None) -> list[dict[str, Any]]:
    """Bought opens + failed rows that still have a buy fill but no sell (orphans)."""
    slot_id = normalize_slot(slot)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE slot = ?
              AND buy_price IS NOT NULL
              AND sell_price IS NULL
              AND status IN ('bought', 'failed')
            ORDER BY id ASC
            """,
            (slot_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def reopen_incomplete_exits(slot: str | None = None) -> int:
    """Turn orphan failed-with-buy rows back to bought so exits can retry."""
    slot_id = normalize_slot(slot)
    now = now_ist_iso()
    with db() as conn:
        cur = conn.execute(
            """
            UPDATE trades SET status='bought', updated_at=?
            WHERE slot = ?
              AND status='failed'
              AND buy_price IS NOT NULL
              AND sell_price IS NULL
            """,
            (now, slot_id),
        )
        return int(cur.rowcount or 0)


def disarm_today(slot: str | None = None) -> dict[str, Any] | None:
    """Cancel today's armed trade for timed slot (latest armed row)."""
    slot_id = normalize_slot(slot)
    trade_date = today_ist()
    trade = get_trade_by_date(trade_date, slot_id)
    if not trade:
        return None
    if trade["status"] != "armed":
        raise ValueError("Can only disarm an armed (not yet bought) trade.")
    now = now_ist_iso()
    with db() as conn:
        conn.execute(
            "UPDATE trades SET status='cancelled', updated_at=? WHERE id=?",
            (now, trade["id"]),
        )
    return get_trade(int(trade["id"]))
