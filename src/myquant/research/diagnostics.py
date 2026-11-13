from __future__ import annotations

import pandas as pd


def forward_returns(bars: pd.DataFrame, horizon: int) -> pd.DataFrame:
    data = bars[["ts_code", "trade_date", "close"]].copy().sort_values(["ts_code", "trade_date"])
    data["forward_return"] = data.groupby("ts_code")["close"].shift(-horizon) / data["close"] - 1
    return data.drop(columns="close")


def information_coefficient(factors: pd.DataFrame, bars: pd.DataFrame, factor: str, horizon: int) -> pd.DataFrame:
    returns = forward_returns(bars, horizon)
    joined = factors.merge(returns, on=["ts_code", "trade_date"], how="inner").dropna(subset=[factor, "forward_return"])
    ic = (
        joined.groupby("trade_date")
        .apply(lambda group: group[factor].corr(group["forward_return"], method="spearman"), include_groups=False)
        .rename("ic")
        .reset_index()
    )
    ic["factor"] = factor
    ic["horizon"] = horizon
    return ic


def ic_summary(ic: pd.DataFrame) -> dict[str, float]:
    mean = float(ic["ic"].mean())
    std = float(ic["ic"].std(ddof=1))
    return {"mean_ic": mean, "icir": mean / std if std else 0.0, "observations": float(ic["ic"].count())}
