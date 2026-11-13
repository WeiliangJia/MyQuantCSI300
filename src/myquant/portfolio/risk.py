from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str


def validate_target_weights(
    weights: pd.Series,
    industries: pd.Series,
    benchmark: pd.Series,
    min_weight: float = 0.005,
    max_weight: float = 0.04,
    expected_holdings: int = 50,
    max_sector_deviation: float = 0.05,
) -> list[RiskCheck]:
    weights = weights[weights > 1e-8]
    checks = [
        RiskCheck("weights_sum", abs(float(weights.sum()) - 1.0) < 1e-5, f"sum={weights.sum():.6f}"),
        RiskCheck("holdings", len(weights) == expected_holdings, f"count={len(weights)}"),
        RiskCheck("single_stock", bool(((weights >= min_weight - 1e-8) & (weights <= max_weight + 1e-8)).all()), "bounds"),
    ]
    for sector in industries.reindex(weights.index).fillna("UNKNOWN").unique():
        actual = float(weights[industries.reindex(weights.index).fillna("UNKNOWN") == sector].sum())
        base = float(benchmark[industries.reindex(benchmark.index).fillna("UNKNOWN") == sector].sum())
        checks.append(RiskCheck(f"sector:{sector}", abs(actual - base) <= max_sector_deviation + 1e-8, f"active={actual - base:.4f}"))
    return checks
