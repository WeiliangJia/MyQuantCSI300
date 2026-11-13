from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd


class TushareClient:
    """Thin, testable wrapper around Tushare Pro; imports the SDK only at runtime."""

    def __init__(self, token: str, pro_factory: Callable[[str], Any] | None = None) -> None:
        if not token:
            raise ValueError("TUSHARE_TOKEN is required for data synchronization")
        if pro_factory is None:
            import tushare as ts

            pro_factory = ts.pro_api
        self.pro = pro_factory(token)

    def fetch(self, endpoint: str, **params: Any) -> pd.DataFrame:
        method = getattr(self.pro, endpoint, None)
        if method is None:
            raise ValueError(f"Unsupported Tushare endpoint: {endpoint}")
        result = method(**{key: value for key, value in params.items() if value is not None})
        if not isinstance(result, pd.DataFrame):
            raise TypeError(f"Tushare endpoint {endpoint} did not return a DataFrame")
        return result.copy()

    def sync_bundle(self, trade_date: str, benchmark_code: str = "000300.SH") -> dict[str, pd.DataFrame]:
        """Fetch all day-frequency inputs used by the v1 pipeline.

        Financial statements are fetched separately by reporting period in production because
        Tushare quotas vary. This bundle intentionally covers data that changes each trade day.
        """
        return {
            "daily": self.fetch("daily", trade_date=trade_date),
            "daily_basic": self.fetch("daily_basic", trade_date=trade_date),
            "moneyflow": self.fetch("moneyflow", trade_date=trade_date),
            "stk_limit": self.fetch("stk_limit", trade_date=trade_date),
            "suspend_d": self.fetch("suspend_d", suspend_date=trade_date),
            "adj_factor": self.fetch("adj_factor", trade_date=trade_date),
            "index_weight": self.fetch("index_weight", index_code=benchmark_code, trade_date=trade_date),
            "index_daily": self.fetch("index_daily", ts_code=benchmark_code, trade_date=trade_date),
        }

    def reference_bundle(self, start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
        """Reference and corporate-action inputs, run on first backfill and then weekly."""
        return {
            "stock_basic": self.fetch(
                "stock_basic", exchange="", list_status="L",
                fields="ts_code,symbol,name,exchange,industry,list_date,delist_date",
            ),
            "trade_cal": self.fetch("trade_cal", exchange="SSE", start_date=start_date, end_date=end_date),
            "dividend": self.fetch("dividend", ann_date=start_date),
        }

    def financial_bundle(self, start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
        """Financial data is keyed by announcement date and normalized into point-in-time metrics."""
        return {
            "fina_indicator": self.fetch("fina_indicator", start_date=start_date, end_date=end_date),
            "income": self.fetch("income", start_date=start_date, end_date=end_date),
            "balancesheet": self.fetch("balancesheet", start_date=start_date, end_date=end_date),
            "cashflow": self.fetch("cashflow", start_date=start_date, end_date=end_date),
        }
