"""Settings for the Nifty index tester only."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
ENV_FILE = PROJECT_ROOT / ".env" if (PROJECT_ROOT / ".env").exists() else Path.cwd() / ".env"

# Fixed product rules. Not shared with the stock option tester.
MAX_BUDGET = 20_000.0
STRIKE_STEP = 50
ADX_MIN = 22.0
PREMIUM_STOP_PCT = 35.0
# Option-premium profit management (Nifty tester only).
PREMIUM_TRAIL_PCT = 1.0  # trailing SL sits 1% below the peak premium
PREMIUM_FIRST_LOCK_PCT = 1.0  # once premium reaches +1%, lock that floor
PREMIUM_TAKE_PCT = 8.0  # hard take-profit: bank a smaller premium gain
MARKET_OPEN_MINUTE = 9 * 60 + 15
FLAT_MINUTE = 15 * 60 + 15
ENTRY_WINDOWS = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60 + 15, 14 * 60 + 45))


def is_market_session(now) -> bool:
    """True Mon–Fri from cash F&O open (09:15) until the flat clock (15:15)."""
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return MARKET_OPEN_MINUTE <= minute < FLAT_MINUTE


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
    index_tester_db: str = str(PACKAGE_ROOT / "data" / "index_tester.db")


def get_settings() -> Settings:
    return Settings()
