from __future__ import annotations

from typing import Any


# Shared exit rule: hard SL -5%. At +5% activate 1% trail ladder (no hard TP).
EXIT_PCT = 5.0
# After +EXIT_PCT profit: lock SL at that rung, raise target by TRAIL_STEP_PCT each time.
TRAIL_STEP_PCT = 1.0
# If neither hits by this IST time, square off at market LTP (paper).
EOD_EXIT_TIME = "15:10"

SLOTS: dict[str, dict[str, Any]] = {
    "t0920": {
        "id": "t0920",
        "label": "Live · multi",
        "title": "Live · multi",
        "entry_mode": "immediate",
        "exit_pct": EXIT_PCT,
        "eod_exit_time": EOD_EXIT_TIME,
    },
    "t0940": {
        "id": "t0940",
        "label": "09:40",
        "title": "09:40 entry",
        "entry_mode": "timed",
        "buy_time": "09:40",
        "exit_pct": EXIT_PCT,
        "eod_exit_time": EOD_EXIT_TIME,
    },
}

DEFAULT_SLOT = "t0920"


def normalize_slot(slot: str | None) -> str:
    key = (slot or DEFAULT_SLOT).strip().lower()
    if key not in SLOTS:
        raise ValueError(f"Unknown slot '{slot}'. Use: {', '.join(SLOTS)}")
    return key


def slot_meta(slot: str | None = None) -> dict[str, Any]:
    return dict(SLOTS[normalize_slot(slot)])


def all_slots() -> list[dict[str, Any]]:
    return [dict(v) for v in SLOTS.values()]


def is_immediate(slot: str | None = None) -> bool:
    return slot_meta(slot).get("entry_mode") == "immediate"


def is_timed(slot: str | None = None) -> bool:
    return slot_meta(slot).get("entry_mode") == "timed"


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.strip().split(":")
    return int(hour), int(minute)
