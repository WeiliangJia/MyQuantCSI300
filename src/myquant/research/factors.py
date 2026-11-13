from __future__ import annotations

import numpy as np
import pandas as pd


FACTOR_GROUPS = {
    "book_to_price": "value",
    "earnings_yield": "value",
    "roe": "quality",
    "cashflow_to_assets": "quality",
    "momentum_60": "momentum",
    "reversal_5": "momentum",
    "low_volatility_60": "low_risk",
    "turnover_stability": "liquidity",
    "money_flow_intensity": "liquidity",
}


def _mad_clip(series: pd.Series, n_mad: float = 5.0) -> pd.Series:
    median = series.median()
    mad = (series - median).abs().median()
    if pd.isna(mad) or mad == 0:
        return series
    return series.clip(median - n_mad * 1.4826 * mad, median + n_mad * 1.4826 * mad)


def _zscore(series: pd.Series) -> pd.Series:
    std = series.std(ddof=0)
    if pd.isna(std) or std == 0:
        return pd.Series(0.0, index=series.index)
    return (series - series.mean()) / std


def standardize_cross_section(frame: pd.DataFrame, factor_columns: list[str]) -> pd.DataFrame:
    """MAD winsorize then industry-neutralize and size-neutralize each factor."""
    result = frame.copy()
    industry = result.get("industry", pd.Series("UNKNOWN", index=result.index)).fillna("UNKNOWN")
    size = np.log(result.get("total_mv", pd.Series(np.nan, index=result.index)).clip(lower=1))
    for column in factor_columns:
        clipped = result.groupby(industry, group_keys=False)[column].apply(_mad_clip, include_groups=False)
        standardized = clipped.groupby(industry, group_keys=False).transform(_zscore)
        valid = standardized.notna() & size.notna()
        if valid.sum() >= 10:
            x = np.column_stack([np.ones(valid.sum()), size.loc[valid]])
            beta = np.linalg.lstsq(x, standardized.loc[valid], rcond=None)[0]
            standardized.loc[valid] = standardized.loc[valid] - x @ beta
        result[column] = _zscore(standardized).fillna(0.0)
    return result


class DailyFactorEngine:
    """Computes only daily, point-in-time factors from explicitly supplied inputs."""

    def compute(
        self,
        bars: pd.DataFrame,
        daily_basic: pd.DataFrame,
        financials: pd.DataFrame,
        moneyflow: pd.DataFrame,
        industry: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        prices = bars.copy()
        prices["trade_date"] = pd.to_datetime(prices["trade_date"])
        prices = prices.sort_values(["ts_code", "trade_date"])
        close = prices.groupby("ts_code")["close"]
        returns = close.pct_change()
        prices["momentum_60"] = close.pct_change(60)
        prices["reversal_5"] = -close.pct_change(5)
        prices["low_volatility_60"] = -returns.groupby(prices["ts_code"]).rolling(60).std().reset_index(level=0, drop=True)
        snapshot = prices.groupby("ts_code", as_index=False).tail(1)[["ts_code", "trade_date", "momentum_60", "reversal_5", "low_volatility_60"]]

        basic = daily_basic.copy()
        basic["trade_date"] = pd.to_datetime(basic["trade_date"])
        basic = basic.sort_values(["ts_code", "trade_date"])
        basic["turnover_stability"] = -basic.groupby("ts_code")["turnover_rate"].transform(
            lambda values: values.rolling(20, min_periods=5).std()
        )
        basic = basic.groupby("ts_code", as_index=False).tail(1)
        basic["book_to_price"] = 1 / basic["pb"].replace(0, np.nan)
        basic["earnings_yield"] = 1 / basic["pe_ttm"].replace(0, np.nan)
        columns = ["ts_code", "book_to_price", "earnings_yield", "turnover_stability", "total_mv"]
        result = snapshot.merge(basic[columns], on="ts_code", how="left")

        fin = financials.copy()
        if not fin.empty:
            fin["available_at"] = pd.to_datetime(fin["available_at"])
            fin = fin.loc[fin["available_at"] <= snapshot["trade_date"].max()]
            fin = fin.sort_values(["ts_code", "metric", "available_at"]).groupby(["ts_code", "metric"], as_index=False).tail(1)
            pivot = fin.pivot(index="ts_code", columns="metric", values="value").reset_index()
            result = result.merge(pivot, on="ts_code", how="left")
        result["roe"] = result.get("roe", pd.Series(np.nan, index=result.index))
        result["cashflow_to_assets"] = result.get("cashflow_to_assets", pd.Series(np.nan, index=result.index))

        flow = moneyflow.copy()
        if not flow.empty:
            flow["trade_date"] = pd.to_datetime(flow["trade_date"])
            flow = flow.sort_values(["ts_code", "trade_date"]).groupby("ts_code", as_index=False).tail(1)
            flow["money_flow_intensity"] = flow["net_mf_amount"] / flow["amount"].replace(0, np.nan)
            result = result.merge(flow[["ts_code", "money_flow_intensity"]], on="ts_code", how="left")
        else:
            result["money_flow_intensity"] = np.nan
        if industry is not None and not industry.empty:
            result = result.merge(industry[["ts_code", "industry"]], on="ts_code", how="left")
        result["industry"] = result.get("industry", pd.Series("UNKNOWN", index=result.index)).fillna("UNKNOWN")
        return standardize_cross_section(result, list(FACTOR_GROUPS))


def combine_factor_groups(factors: pd.DataFrame, factor_weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Equal-risk group blend by default; research can replace weights with trailing ICIR."""
    result = factors.copy()
    weights = factor_weights or {group: 1.0 for group in set(FACTOR_GROUPS.values())}
    group_scores: list[pd.Series] = []
    for group in sorted(set(FACTOR_GROUPS.values())):
        columns = [name for name, category in FACTOR_GROUPS.items() if category == group and name in result]
        if columns:
            group_scores.append(result[columns].mean(axis=1) * weights.get(group, 0.0))
    result["alpha"] = pd.concat(group_scores, axis=1).mean(axis=1) if group_scores else 0.0
    return result
