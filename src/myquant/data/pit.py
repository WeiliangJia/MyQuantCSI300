from __future__ import annotations

import pandas as pd


def as_of(frame: pd.DataFrame, as_of_date: str | pd.Timestamp, keys: list[str]) -> pd.DataFrame:
    """Return the most recent point-in-time record per business key.

    Inputs must include `available_at`; records known after the requested date are excluded.
    """
    if "available_at" not in frame.columns:
        raise ValueError("Point-in-time data must include available_at")
    result = frame.copy()
    result["available_at"] = pd.to_datetime(result["available_at"])
    cutoff = pd.Timestamp(as_of_date)
    result = result.loc[result["available_at"] <= cutoff]
    if result.empty:
        return result
    order = [*keys, "available_at"]
    return result.sort_values(order).groupby(keys, as_index=False).tail(1).copy()
