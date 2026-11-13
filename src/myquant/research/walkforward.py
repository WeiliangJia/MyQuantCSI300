from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    validation_start: pd.Timestamp
    validation_end: pd.Timestamp


def rolling_windows(
    trade_dates: pd.Series,
    train_years: int = 3,
    validation_months: int = 6,
    step_months: int = 6,
    final_holdout_start: str = "2023-01-01",
) -> list[WalkForwardWindow]:
    """Generate expanding chronological windows without touching the final blind holdout."""
    dates = pd.DatetimeIndex(pd.to_datetime(trade_dates).unique()).sort_values()
    holdout = pd.Timestamp(final_holdout_start)
    dates = dates[dates < holdout]
    if dates.empty:
        return []
    cursor = dates.min() + pd.DateOffset(years=train_years)
    windows: list[WalkForwardWindow] = []
    while cursor < dates.max():
        validation_end = min(cursor + pd.DateOffset(months=validation_months) - pd.Timedelta(days=1), dates.max())
        if validation_end <= cursor:
            break
        windows.append(WalkForwardWindow(dates.min(), cursor - pd.Timedelta(days=1), cursor, validation_end))
        cursor += pd.DateOffset(months=step_months)
    return windows
