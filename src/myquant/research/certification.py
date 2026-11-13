"""Evidence gates for point-in-time CSI 300 research.

The research stack must never turn a partial constituent history into an
apparently valid backtest.  A date is certified only when the complete 300-name
member set and its 300 benchmark weights are available from an approved source.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


OFFICIAL_MEMBERSHIP_SOURCES = frozenset({"official_manifest", "official_license"})
OFFICIAL_WEIGHT_SOURCES = frozenset({"official_license"})
MODELED_FREE_FLOAT_WEIGHT_SOURCE = "free_float_market_cap"


@dataclass(frozen=True)
class CertifiedRange:
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    trading_days: int


def membership_coverage(
    snapshots: pd.DataFrame,
    weights: pd.DataFrame,
    *,
    expected_members: int = 300,
) -> pd.DataFrame:
    """Return daily evidence coverage for the intersection of supplied dates."""
    columns = ["trade_date", "member_count", "weight_count", "weight_sum", "certified", "reason"]
    if snapshots.empty and weights.empty:
        return pd.DataFrame(columns=columns)

    members = snapshots.copy()
    if not members.empty:
        members["trade_date"] = pd.to_datetime(members["trade_date"]).dt.normalize()
        members = members.loc[members.get("is_member", True).astype(bool)]
        if "source" in members:
            members = members.loc[members["source"].isin(OFFICIAL_MEMBERSHIP_SOURCES)]
        member_counts = members.groupby("trade_date")["ts_code"].nunique().rename("member_count")
    else:
        member_counts = pd.Series(dtype="int64", name="member_count")

    benchmark = weights.copy()
    if not benchmark.empty:
        benchmark["trade_date"] = pd.to_datetime(benchmark["trade_date"]).dt.normalize()
        if "source" in benchmark:
            benchmark = benchmark.loc[benchmark["source"].isin(OFFICIAL_WEIGHT_SOURCES)]
        valid_weights = benchmark.loc[pd.to_numeric(benchmark["weight"], errors="coerce").gt(0)]
        weight_counts = valid_weights.groupby("trade_date")["ts_code"].nunique().rename("weight_count")
        weight_sums = valid_weights.groupby("trade_date")["weight"].sum().rename("weight_sum")
    else:
        weight_counts = pd.Series(dtype="int64", name="weight_count")
        weight_sums = pd.Series(dtype="float64", name="weight_sum")

    index = member_counts.index.union(weight_counts.index).sort_values()
    coverage = pd.DataFrame(index=index).join(member_counts).join(weight_counts).join(weight_sums).fillna(
        {"member_count": 0, "weight_count": 0, "weight_sum": 0.0}
    )
    coverage["member_count"] = coverage["member_count"].astype(int)
    coverage["weight_count"] = coverage["weight_count"].astype(int)
    coverage["certified"] = (
        coverage["member_count"].eq(expected_members)
        & coverage["weight_count"].eq(expected_members)
        & coverage["weight_sum"].between(0.995, 1.005)
    )
    coverage["reason"] = "certified"
    coverage.loc[coverage["member_count"].ne(expected_members), "reason"] = "incomplete_official_membership"
    coverage.loc[
        coverage["member_count"].eq(expected_members) & coverage["weight_count"].ne(expected_members), "reason"
    ] = "missing_or_incomplete_official_weights"
    coverage.loc[
        coverage["member_count"].eq(expected_members)
        & coverage["weight_count"].eq(expected_members)
        & ~coverage["weight_sum"].between(0.995, 1.005),
        "reason",
    ] = "invalid_benchmark_weight_sum"
    return coverage.reset_index(names="trade_date")[columns]


def certified_ranges(coverage: pd.DataFrame, *, min_trading_days: int = 1) -> list[CertifiedRange]:
    """Find contiguous runs of certified trading dates in an already ordered calendar."""
    if coverage.empty:
        return []
    data = coverage.copy().sort_values("trade_date").reset_index(drop=True)
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    groups = (data["certified"].ne(data["certified"].shift())).cumsum()
    ranges: list[CertifiedRange] = []
    for _, block in data.loc[data["certified"]].groupby(groups[data["certified"]]):
        if len(block) >= min_trading_days:
            ranges.append(CertifiedRange(block["trade_date"].iloc[0], block["trade_date"].iloc[-1], len(block)))
    return ranges


def require_certified_range(
    coverage: pd.DataFrame, start_date: str, end_date: str, *, min_trading_days: int = 1
) -> CertifiedRange:
    """Fail closed unless every requested trading date passed the evidence gate."""
    start, end = pd.Timestamp(start_date).normalize(), pd.Timestamp(end_date).normalize()
    selected = coverage.loc[
        pd.to_datetime(coverage["trade_date"]).between(start, end)
    ].copy()
    if selected.empty or len(selected) < min_trading_days or not selected["certified"].all():
        reasons = selected.loc[~selected["certified"], "reason"].value_counts().to_dict()
        raise ValueError(
            "Certified CSI 300 research range unavailable; import complete official membership and "
            f"daily benchmark weights first. blockers={reasons or {'no_certified_trading_dates': 1}}"
        )
    return CertifiedRange(start, end, len(selected))
