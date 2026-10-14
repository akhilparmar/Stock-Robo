from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    neo_access_token: str = ""
    neo_mobile: str = ""
    neo_ucc: str = ""
    paper_mode: bool = True
    buy_time: str = "09:20"
    sell_time: str = "09:24"
    host: str = "0.0.0.0"
    port: int = 8010
    neo_fin_key: str = "neotradeapi"
    login_base: str = "https://mis.kotaksecurities.com"
    db_path: str = "data/trades.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
