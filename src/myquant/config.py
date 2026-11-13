from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Secrets are read from the environment, never the database."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tushare_token: str = ""
    database_url: str = "postgresql+psycopg://myquant:myquant@localhost:5432/myquant"
    raw_archive_path: Path = Path("data/raw")
    benchmark_code: str = "000300.SH"
    initial_capital: float = 10_000_000.0
    target_holdings: int = 50
    max_weight: float = 0.04
    min_weight: float = 0.005
    max_one_way_turnover: float = 0.20
    tracking_error_low: float = 0.06
    tracking_error_high: float = 0.08
    max_adv_participation: float = 0.01
    buy_slippage_bps: float = 5.0
    sell_slippage_bps: float = 5.0
    commission_bps: float = 3.0
    minimum_commission: float = 5.0
    sell_stamp_duty_bps: float = 5.0
    sh_transfer_bps: float = 0.01


def get_settings() -> Settings:
    return Settings()

