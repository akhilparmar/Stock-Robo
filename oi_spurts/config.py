"""Settings for the OI-spurts paper tester only."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
ENV_FILE = PROJECT_ROOT / ".env" if (PROJECT_ROOT / ".env").exists() else Path.cwd() / ".env"

TOP_N = 5
OI_TOP_N = 20
MAX_ENTRIES = 2
BODY_MIN = 0.45
VOLUME_MIN_RATIO = 0.70
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50
SMA_FAST = 20
RSI_PERIOD = 14
# Tighter than before after the 14-Oct fade day.
RSI_GAINER_MIN = 48.0
RSI_GAINER_MAX = 72.0
RSI_LOSER_MIN = 28.0
RSI_LOSER_MAX = 52.0
MIN_SETUP_SCORE = 40.0
MIN_OI_PCT = 2.0
# Stock stop cannot be wider than this % from entry.
MAX_STOCK_STOP_PCT = 1.25
# Option premium emergency exit.
PREMIUM_STOP_PCT = 25.0
ENTRY_MINUTE = 9 * 60 + 20
ENTRY_DEADLINE_MINUTE = 9 * 60 + 25
# If still underwater on the stock by this clock, flatten.
TIME_CUT_MINUTE = 11 * 60
MARKET_OPEN_MINUTE = 9 * 60 + 15
FLAT_MINUTE = 15 * 60 + 15


def is_market_session(now) -> bool:
    """True Mon–Fri from 09:15 until the 15:15 flat."""
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return MARKET_OPEN_MINUTE <= minute < FLAT_MINUTE


def clock_minute(now) -> int:
    return now.hour * 60 + now.minute


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    neo_access_token: str = ""
    neo_mobile: str = ""
    neo_ucc: str = ""
    neo_fin_key: str = "neotradeapi"
    login_base: str = "https://mis.kotaksecurities.com"
    oi_spurts_db: str = str(PACKAGE_ROOT / "data" / "oi_spurts.db")


def get_settings() -> Settings:
    return Settings()
