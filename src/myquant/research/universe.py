from __future__ import annotations

import pandas as pd

from myquant.universe_exclusions import load_excluded_ts_codes


def build_tradable_universe(
    constituents: pd.DataFrame,
    bars: pd.DataFrame,
    status: pd.DataFrame | None = None,
    min_history_days: int = 120,
    excluded_ts_codes: set[str] | None = None,
) -> pd.DataFrame:
    """Return today's investable constituents without silently using future data."""
    if constituents.empty or bars.empty:
        return pd.DataFrame(columns=["ts_code", "benchmark_weight"])
    today = pd.Timestamp(bars["trade_date"].max())
    exclusions = excluded_ts_codes if excluded_ts_codes is not None else load_excluded_ts_codes()
    members = constituents.loc[pd.to_datetime(constituents["trade_date"]) == today].copy()
    members = members.rename(columns={"weight": "benchmark_weight", "con_code": "ts_code"})
    counts = bars.loc[pd.to_datetime(bars["trade_date"]) <= today].groupby("ts_code").size()
    bar_columns = ["ts_code", "open", "high", "low", "close", "volume"]
    if "is_zero_volume" in bars.columns:
        bar_columns.append("is_zero_volume")
    current_bars = bars.loc[pd.to_datetime(bars["trade_date"]) == today, bar_columns]
    universe = members.merge(current_bars, on="ts_code", how="inner")
    universe = universe.loc[~universe["ts_code"].isin(exclusions)]
    universe = universe.loc[(universe["volume"].fillna(0) > 0) & universe["ts_code"].map(counts).ge(min_history_days)]
    if "is_zero_volume" in universe.columns:
        universe = universe.loc[~universe["is_zero_volume"].fillna(True)]
    if status is not None and not status.empty:
        active = status.loc[pd.to_datetime(status["trade_date"]) == today, ["ts_code", "is_suspended", "is_st"]]
        universe = universe.merge(active, on="ts_code", how="left")
        universe = universe.loc[~universe["is_suspended"].fillna(False) & ~universe["is_st"].fillna(False)]
    return universe.reset_index(drop=True)
