"""Transparent, fail-closed benchmark-weight proxies for research.

The CSI 300's published methodology uses free-float adjusted market value.
``circ_mv`` is the available daily proxy in the project's valuation feed.  It
does not replace licensed index weights, so every output is explicitly marked
as modelled and cannot satisfy the official certification gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import pandas as pd


FREE_FLOAT_MARKET_CAP_SOURCE = "free_float_market_cap"
FREE_FLOAT_MARKET_CAP_METHODOLOGY = "circ_mv_pro_rata_v1"
EQUAL_WEIGHT_SOURCE = "myquant_equal_weight"
EQUAL_WEIGHT_METHODOLOGY = "monthly_equal_weight_drift_v1"


@dataclass(frozen=True)
class FreeFloatWeightBuild:
    weights: pd.DataFrame
    audit: pd.DataFrame


@dataclass(frozen=True)
class EqualWeightBenchmarkBuild:
    weights: pd.DataFrame
    audit: pd.DataFrame
    rebalances: pd.DataFrame
    nav: pd.DataFrame


def monthly_equal_weight_benchmark(
    membership: pd.DataFrame,
    bars: pd.DataFrame,
    suspensions: pd.DataFrame | None = None,
    *,
    expected_members: int = 300,
    suspended_dividends: pd.DataFrame | None = None,
) -> EqualWeightBenchmarkBuild:
    """Build end-of-day weights for a monthly equal-weight simulated market.

    Signals are dated at the preceding close; bootstrap has no signal date.
    A suspension defers the entire rebalance, retaining old holdings until retry.
    Unexplained missing prices block the day and invalidate subsequent drift.
    Suspended dividends use cash per adjusted share on the same scale as bars.
    Subsequent bars must exclude those dividends from their adjustment factor.
    """
    required_membership = {"trade_date", "ts_code"}
    required_bars = {"trade_date", "ts_code", "adj_open", "adj_close"}
    if missing := required_membership.difference(membership.columns):
        raise ValueError(f"Membership data missing columns: {sorted(missing)}")
    if missing := required_bars.difference(bars.columns):
        raise ValueError(f"Clean bars missing columns: {sorted(missing)}")

    members = membership.copy()
    members["trade_date"] = pd.to_datetime(members["trade_date"]).dt.normalize()
    if "is_member" in members:
        members = members.loc[members["is_member"].astype(bool)]
    if "available_at" in members:
        members["available_at"] = pd.to_datetime(members["available_at"]).dt.normalize()
    members = members.drop_duplicates(["trade_date", "ts_code"])

    prices = bars.copy()
    prices["trade_date"] = pd.to_datetime(prices["trade_date"]).dt.normalize()
    prices["adj_open"] = pd.to_numeric(prices["adj_open"], errors="coerce")
    prices["adj_close"] = pd.to_numeric(prices["adj_close"], errors="coerce")
    if "available_at" in prices:
        prices["available_at"] = pd.to_datetime(prices["available_at"]).dt.normalize()
        prices = prices.loc[prices["available_at"] <= prices["trade_date"]]
    prices = prices.drop_duplicates(["trade_date", "ts_code"], keep="last")
    price_rows = {
        (row.trade_date, row.ts_code): row
        for row in prices.itertuples(index=False)
    }

    suspended = set()
    if "is_suspended" in prices:
        suspended.update(
            (row.trade_date, row.ts_code)
            for row in prices.loc[prices["is_suspended"].fillna(False).astype(bool)].itertuples(index=False)
        )
    if suspensions is not None and not suspensions.empty:
        required_suspensions = {"trade_date", "ts_code", "is_suspended"}
        if missing := required_suspensions.difference(suspensions.columns):
            raise ValueError(f"Suspension data missing columns: {sorted(missing)}")
        status = suspensions.copy()
        status["trade_date"] = pd.to_datetime(status["trade_date"]).dt.normalize()
        if "available_at" in status:
            status["available_at"] = pd.to_datetime(status["available_at"]).dt.normalize()
            status = status.loc[status["available_at"] <= status["trade_date"]]
        suspended.update(
            (row.trade_date, row.ts_code)
            for row in status.loc[status["is_suspended"].fillna(False).astype(bool)].itertuples(index=False)
        )

    dividends = {}
    if suspended_dividends is not None:
        for row in suspended_dividends.itertuples(index=False):
            key = (pd.Timestamp(row.trade_date).normalize(), row.ts_code)
            amount = float(row.adjusted_cash_per_share)
            if key not in suspended or key in dividends or not isfinite(amount) or amount <= 0:
                raise ValueError(f"Invalid suspended dividend: {key}")
            dividends[key] = amount

    weight_columns = ["trade_date", "ts_code", "weight", "source", "methodology"]
    audit_columns = [
        "trade_date",
        "member_count",
        "valid_price_count",
        "suspended_count",
        "rebalance_type",
        "ready",
        "reason",
        "missing_price_count",
    ]
    weight_rows: list[dict[str, object]] = []
    audit_rows: list[dict[str, object]] = []
    rebalance_rows: list[dict[str, object]] = []
    previous_date: pd.Timestamp | None = None
    previous_members: set[str] | None = None
    previous_weights: pd.Series | None = None
    last_closes: dict[str, float] = {}
    pending_rebalance = False
    cash_weight = 0.0
    nav = 1.0
    nav_rows = []

    for trade_date, daily_members in members.groupby("trade_date", sort=True):
        codes = sorted(daily_members["ts_code"].unique())
        current_members = set(codes)
        member_count = len(current_members)
        first_date = previous_date is None
        monthly_rebalance = previous_date is not None and trade_date.to_period("M") != previous_date.to_period("M")
        membership_rebalance = previous_members is not None and current_members != previous_members
        rebalance = first_date or monthly_rebalance or membership_rebalance or pending_rebalance
        intended_codes = codes.copy()
        held_codes = set(previous_weights.index) if previous_weights is not None else set()
        blocked_rebalance = rebalance and any(
            (trade_date, code) in suspended for code in current_members | held_codes
        )
        pending_rebalance = blocked_rebalance
        if blocked_rebalance and previous_weights is not None:
            codes = sorted(held_codes)
        if first_date:
            rebalance_type = "bootstrap"
        elif monthly_rebalance and membership_rebalance:
            rebalance_type = "monthly_and_membership"
        elif monthly_rebalance:
            rebalance_type = "monthly"
        elif membership_rebalance:
            rebalance_type = "membership"
        elif rebalance:
            rebalance_type = "retry"
        else:
            rebalance_type = "drift"

        missing_prices: list[str] = []
        valid_price_count = 0
        overnight = 1.0
        executes = rebalance and not blocked_rebalance
        open_factors = {}
        dividend_weight = 0.0
        for code in held_codes:
            row = price_rows.get((trade_date, code))
            if (trade_date, code) in suspended:
                open_factors[code] = 1.0
                if amount := dividends.get((trade_date, code)):
                    mark = last_closes.get(code, 0.0)
                    if not 0 < amount < mark:
                        raise ValueError(f"Dividend exceeds or lacks prior mark: {trade_date}, {code}")
                    last_closes[code] = mark - amount
                    dividend_weight += float(previous_weights[code]) * amount / mark
                    open_factors[code] = (mark - amount) / mark
            elif row is not None and isfinite(row.adj_open) and row.adj_open > 0 and code in last_closes:
                open_factors[code] = row.adj_open / last_closes[code]
            else:
                missing_prices.append(code)
        if previous_weights is not None and not missing_prices:
            overnight = float(previous_weights.dot(pd.Series(open_factors).reindex(previous_weights.index)))
            overnight += cash_weight + dividend_weight
        unknown_signal = (
            membership_rebalance and "available_at" in daily_members
            and (daily_members["available_at"].isna()
                 | (daily_members["available_at"] > previous_date)).any()
        )
        if unknown_signal:
            reason = "membership_not_known_at_signal"
            previous_weights = None
        elif blocked_rebalance and previous_weights is None:
            reason = "suspended_bootstrap"
        elif member_count != expected_members:
            reason = "incomplete_membership"
            previous_weights = None
        elif not rebalance and previous_weights is None:
            reason = "missing_prior_weights"
        else:
            open_cash_weight = 0.0 if executes else (cash_weight + dividend_weight) / overnight
            base = (
                pd.Series(1.0 / expected_members, index=codes, dtype=float)
                if rebalance and not blocked_rebalance
                else previous_weights.reindex(codes) * pd.Series(open_factors).reindex(codes) / overnight
            )
            factors = pd.Series(1.0, index=codes, dtype=float)
            for code in codes:
                row = price_rows.get((trade_date, code))
                day_suspended = (trade_date, code) in suspended
                valid_open = row is not None and isfinite(row.adj_open) and row.adj_open > 0
                valid_close = row is not None and isfinite(row.adj_close) and row.adj_close > 0
                if valid_close:
                    valid_price_count += 1
                if day_suspended:
                    if code not in last_closes and not valid_close:
                        missing_prices.append(code)
                    continue
                if not valid_open or not valid_close:
                    missing_prices.append(code)
                else:
                    factors.loc[code] = float(row.adj_close) / float(row.adj_open)

            if missing_prices:
                reason = "missing_unexplained_price"
                previous_weights = None
            else:
                drifted = base * factors
                total = float(drifted.sum()) + open_cash_weight
                if total <= 0 or not isfinite(total):
                    reason = "invalid_weight_sum"
                    previous_weights = None
                else:
                    previous_weights = drifted / total
                    cash_weight = open_cash_weight / total
                    reason = "ready"
                    if nav is not None:
                        open_nav = nav * overnight
                        close_nav = open_nav * total
                        nav_rows.append({
                            "trade_date": trade_date.date(), "previous_nav": nav,
                            "open_nav": open_nav, "overnight_pnl": open_nav - nav,
                            "intraday_pnl": close_nav - open_nav, "nav": close_nav,
                            "dividend_cash": nav * dividend_weight,
                            "cash": close_nav * cash_weight,
                            "rebalanced": executes,
                        })
                        nav = close_nav
                    weight_rows.extend({
                        "trade_date": trade_date.date(),
                        "ts_code": code,
                        "weight": float(previous_weights.loc[code]),
                        "source": EQUAL_WEIGHT_SOURCE,
                        "methodology": EQUAL_WEIGHT_METHODOLOGY,
                    } for code in codes)

        if reason != "ready":
            nav = None
        if rebalance:
            rebalance_rows.append({
                "signal_date": previous_date.date() if previous_date is not None else None,
                "execution_date": trade_date.date(),
                "rebalance_type": rebalance_type,
                "target_weights": {code: 1.0 / expected_members for code in intended_codes},
                "executed": reason == "ready" and not blocked_rebalance,
                "reason": "suspended_rebalance_deferred" if blocked_rebalance else reason,
            })

        for code in codes:
            row = price_rows.get((trade_date, code))
            if (trade_date, code) not in suspended and row is not None and pd.notna(row.adj_close) and float(row.adj_close) > 0:
                last_closes[code] = float(row.adj_close)
        audit_rows.append({
            "trade_date": trade_date,
            "member_count": member_count,
            "valid_price_count": valid_price_count,
            "suspended_count": sum((trade_date, code) in suspended for code in codes),
            "rebalance_type": rebalance_type,
            "ready": reason == "ready",
            "reason": reason,
            "missing_price_count": len(set(missing_prices)),
        })
        previous_date = trade_date
        previous_members = current_members

    return EqualWeightBenchmarkBuild(
        weights=pd.DataFrame(weight_rows, columns=weight_columns),
        audit=pd.DataFrame(audit_rows, columns=audit_columns),
        rebalances=pd.DataFrame(rebalance_rows),
        nav=pd.DataFrame(nav_rows, columns=[
            "trade_date", "previous_nav", "open_nav", "overnight_pnl", "intraday_pnl", "nav",
            "rebalanced", "dividend_cash", "cash",
        ]),
    )


def free_float_market_cap_weights(
    membership: pd.DataFrame,
    valuations: pd.DataFrame,
    *,
    expected_members: int = 300,
) -> FreeFloatWeightBuild:
    """Calculate daily pro-rata weights from point-in-time circulating value.

    A day is written only if all expected constituents have a positive
    ``circ_mv`` observation that was available on or before that trade date.
    This makes incomplete vendor coverage visible in the audit table instead
    of silently normalising the partial subset to 100%.
    """
    required_membership = {"trade_date", "ts_code"}
    required_valuations = {"trade_date", "ts_code", "circ_mv"}
    if missing := required_membership.difference(membership.columns):
        raise ValueError(f"Membership data missing columns: {sorted(missing)}")
    if missing := required_valuations.difference(valuations.columns):
        raise ValueError(f"Valuation data missing columns: {sorted(missing)}")

    members = membership.copy()
    members["trade_date"] = pd.to_datetime(members["trade_date"]).dt.normalize()
    if "is_member" in members:
        members = members.loc[members["is_member"].astype(bool)]
    members = members[["trade_date", "ts_code"]].drop_duplicates()

    values = valuations.copy()
    values["trade_date"] = pd.to_datetime(values["trade_date"]).dt.normalize()
    values["circ_mv"] = pd.to_numeric(values["circ_mv"], errors="coerce")
    if "available_at" in values:
        values["available_at"] = pd.to_datetime(values["available_at"]).dt.normalize()
        values = values.loc[values["available_at"] <= values["trade_date"]]
    provenance_columns = [column for column in ("source", "methodology") if column in values]
    values = values[["trade_date", "ts_code", "circ_mv", *provenance_columns]].drop_duplicates(
        ["trade_date", "ts_code"], keep="last"
    )

    joined = members.merge(values, on=["trade_date", "ts_code"], how="left")
    audit_rows: list[dict[str, object]] = []
    weight_rows: list[dict[str, object]] = []
    for trade_date, group in joined.groupby("trade_date", sort=True):
        member_count = int(group["ts_code"].nunique())
        valid = group.loc[group["circ_mv"].gt(0)].copy()
        valid_count = int(valid["ts_code"].nunique())
        market_value = float(valid["circ_mv"].sum())
        ready = member_count == expected_members and valid_count == expected_members and market_value > 0
        if member_count != expected_members:
            reason = "incomplete_membership"
        elif valid_count != expected_members:
            reason = "missing_or_nonpositive_circ_mv"
        else:
            reason = "ready"
        audit_rows.append({
            "trade_date": trade_date,
            "member_count": member_count,
            "valid_circ_mv_count": valid_count,
            "free_float_market_value": market_value,
            "ready": ready,
            "reason": reason,
        })
        if ready:
            valid["weight"] = valid["circ_mv"] / market_value
            weight_rows.extend({
                "trade_date": trade_date.date(),
                "ts_code": row.ts_code,
                "weight": float(row.weight),
                "source": getattr(row, "source", FREE_FLOAT_MARKET_CAP_SOURCE),
                "methodology": getattr(row, "methodology", FREE_FLOAT_MARKET_CAP_METHODOLOGY),
            } for row in valid.itertuples(index=False))
    return FreeFloatWeightBuild(
        weights=pd.DataFrame(weight_rows),
        audit=pd.DataFrame(audit_rows),
    )
