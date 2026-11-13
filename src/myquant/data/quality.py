from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    details: dict[str, int | float | str]


def check_daily_bars(frame: pd.DataFrame) -> list[CheckResult]:
    required = {"ts_code", "trade_date", "open", "high", "low", "close"}
    missing = required.difference(frame.columns)
    if missing:
        return [CheckResult("daily_bar_columns", False, {"missing": ",".join(sorted(missing))})]
    duplicate_count = int(frame.duplicated(["ts_code", "trade_date"]).sum())
    invalid_prices = int(
        ((frame["low"] > frame[["open", "close", "high"]].min(axis=1))
         | (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
         | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)).sum()
    )
    volume_column = "volume" if "volume" in frame.columns else "vol"
    invalid_volume = int((frame.get(volume_column, pd.Series(0, index=frame.index)).fillna(0) < 0).sum())
    return [
        CheckResult("daily_bar_primary_key", duplicate_count == 0, {"duplicates": duplicate_count}),
        CheckResult("daily_bar_ohlc", invalid_prices == 0, {"invalid_rows": invalid_prices}),
        CheckResult("daily_bar_volume", invalid_volume == 0, {"invalid_rows": invalid_volume}),
    ]


def check_index_weights(frame: pd.DataFrame, tolerance: float = 0.03) -> CheckResult:
    if frame.empty:
        return CheckResult("index_weight_coverage", False, {"reason": "no constituents"})
    weight_col = "weight" if "weight" in frame.columns else "weight_pct"
    if weight_col not in frame:
        return CheckResult("index_weight_columns", False, {"reason": "weight column missing"})
    total = float(frame[weight_col].sum())
    normalized_total = total / 100 if total > 2 else total
    return CheckResult(
        "index_weight_sum", bool(np.isclose(normalized_total, 1.0, atol=tolerance)), {"sum": total}
    )


def require_passing(results: list[CheckResult]) -> None:
    failures = [result for result in results if not result.passed]
    if failures:
        message = "; ".join(f"{result.name}: {result.details}" for result in failures)
        raise ValueError(f"Data quality check failed: {message}")
