from __future__ import annotations

import numpy as np
import pandas as pd


def performance_report(nav: pd.DataFrame, benchmark: pd.DataFrame) -> dict[str, float]:
    joined = nav.merge(benchmark[["trade_date", "close"]], on="trade_date", how="inner").sort_values("trade_date")
    strategy_returns = joined["nav"].pct_change().dropna()
    benchmark_returns = joined["close"].pct_change().dropna()
    active = strategy_returns - benchmark_returns
    years = len(strategy_returns) / 252
    total = joined["nav"].iloc[-1] / joined["nav"].iloc[0] if len(joined) > 1 else 1.0
    annualized_return = total ** (1 / years) - 1 if years > 0 else 0.0
    tracking_error = float(active.std(ddof=1) * np.sqrt(252)) if len(active) > 1 else 0.0
    info_ratio = float(active.mean() / active.std(ddof=1) * np.sqrt(252)) if active.std(ddof=1) else 0.0
    relative_nav = (1 + active).cumprod()
    relative_drawdown = relative_nav / relative_nav.cummax() - 1
    return {
        "annualized_return": float(annualized_return),
        "annualized_excess_return": float(active.mean() * 252),
        "tracking_error": tracking_error,
        "information_ratio": info_ratio,
        "max_relative_drawdown": float(relative_drawdown.min()) if not relative_drawdown.empty else 0.0,
    }
