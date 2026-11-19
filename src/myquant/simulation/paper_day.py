"""Book one V1 or V2 trading session from a dated input snapshot."""

import argparse
from datetime import date
from hashlib import sha256
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from myquant.simulation.paper import ResearchCosts2024, book_session, fen, save_one_session


TURNOVER_CAP = 0.20


def signal_targets(scores, reference, returns, industry, quotes, members, record, *, staged=False, explain=False):
    account = record["account"]
    current = pd.Series({row["ts_code"]: row["market_value"] / account["nav_cny"]
                         for row in record["positions"]}, dtype=float)
    reference = reference.astype(float)
    universe = reference.index.union(current.index)
    reference = reference.reindex(universe, fill_value=0.0)
    held = current.reindex(universe, fill_value=0.0)
    names = industry.reindex(universe)
    missing_industry = sorted(names.index[names.isna() & (reference.gt(0) | held.gt(0))])
    if missing_industry:
        return None, {"decision": "missing_industry", "missing_industry_count": len(missing_industry)}

    suspended = quotes.suspended.reindex(universe).eq(True) & held.gt(0)
    frozen = held.where(suspended, 0.0)
    for row in record["positions"]:
        if row.get("locked_quantity", 0):
            code = row["ts_code"]
            frozen.loc[code] = max(frozen.loc[code], row["locked_quantity"] * row["close"] / account["nav_cny"])
    eligible = universe.isin(members) & quotes.isST.reindex(universe).eq(0) & quotes.suspended.reindex(universe).eq(False)
    core = reference.where(eligible, 0.0)
    top = scores.loc[scores.ts_code.isin(universe[eligible])].sort_values(
        ["score", "ts_code"], ascending=[False, True],
    ).head(50).ts_code.tolist()
    if core.sum() <= 0 or len(top) != 50 or frozen.sum() >= 0.98:
        return None, {"decision": "insufficient_eligible_stocks", "ranked_available": len(top)}
    tilt = pd.Series(universe.isin(top), index=universe, dtype=float) / len(top)
    available = 0.98 - frozen.sum()
    base_sector = reference.groupby(names).sum()
    limit = reference.clip(lower=0.04)
    risk = returns.tail(60).reindex(columns=universe)
    mandatory = ~universe.isin(members)
    first_build = current.empty and not (staged and record.get("has_entered", False))
    attempts = []
    for enhancement in (0.20, 0.10, 0.0):
        proposed = frozen + available * ((1 - enhancement) * core / core.sum() + enhancement * tilt)
        if staged or explain:
            delta = (proposed - held).where(~mandatory, 0.0)
            buy = float(delta.clip(lower=0).sum())
            sell = float(-delta.clip(upper=0).sum())
            raw_turnover = max(buy, sell)
            fraction = 1.0
            if staged and not first_build and raw_turnover > TURNOVER_CAP:
                fraction = (TURNOVER_CAP - 1e-12) / raw_turnover
                proposed = (held + fraction * delta).where(~mandatory, proposed)
        active = proposed - reference
        material = active.index[active.abs().gt(1e-12)]
        active_returns = risk[material].dropna().dot(active.loc[material])
        tracking_error = float(active_returns.std(ddof=1) * np.sqrt(252)) if len(active_returns) >= 40 else None
        sector = float((proposed.groupby(names).sum() - base_sector).abs().max())
        change = (proposed - held).where(~mandatory, 0)
        turnover = float(max(change.clip(lower=0).sum(), -change.clip(upper=0).sum()))
        checks = {"enhancement": enhancement, "tracking_error": tracking_error, "risk_sessions": len(active_returns),
                  "industry_deviation": sector, "single_stock_excess": float((proposed - limit).max()),
                  "turnover": turnover, "first_build": first_build}
        if staged or explain:
            checks.update(full_buy=buy, full_sell=sell, full_turnover=raw_turnover, step_fraction=fraction)
        attempts.append(checks)
        if (tracking_error is not None and tracking_error <= 0.08 and sector <= 0.05
                and checks["single_stock_excess"] <= 1e-10 and (turnover <= TURNOVER_CAP or first_build)):
            return proposed.loc[proposed.gt(0)].to_dict(), {"decision": "approved", **checks, "attempts": attempts}
    return None, {"decision": "constraints_blocked", "attempts": attempts}


def _iso(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError(f"Expected an ISO trading date, got {value!r}")
    return value


def run_day(database, path, *, monthly_cost=0.0, dividend_tax_rate=0.0, version="v1"):
    """Read one normalized close-of-day input; return the journal and whether it was inserted."""
    packet = json.loads(Path(path).read_bytes())
    day = _iso(packet["trade_date"])
    prior_day = packet["previous_trade_date"]
    next_day = _iso(packet["next_trade_date"])
    if next_day <= day or (prior_day is not None and _iso(prior_day) >= day):
        raise ValueError("Invalid trading calendar sequence")
    if not math.isfinite(monthly_cost) or monthly_cost < 0 or not 0 <= dividend_tax_rate <= 1:
        raise ValueError("Invalid fixed cost or dividend tax rate")
    capital = 10_000_000.0
    costs = ResearchCosts2024()
    monthly_cost = fen(monthly_cost)
    if version not in ("v1", "v2"):
        raise ValueError("Unknown strategy version")
    profile = f"mq300-{version}-day/1:costs-2024:{monthly_cost}:{float(dividend_tax_rate)}"
    digest = sha256(json.dumps(packet, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def step(day, previous):
        if prior_day != (previous["trade_date"] if previous else None):
            raise ValueError(f"{day}: previous trading day is missing or inconsistent")
        if previous and previous["next_trade_date"] != day:
            raise ValueError(f"{day}: expected {previous['next_trade_date']}")
        members = set(packet["members"])
        if len(members) != 300 or len(packet["members"]) != 300:
            raise ValueError("Expected 300 distinct CSI300 constituents")
        quotes = pd.DataFrame(packet["quotes"]).set_index("ts_code")
        if (not quotes.index.is_unique or not members.issubset(quotes.index)
                or not quotes.suspended.map(lambda x: isinstance(x, bool)).all()):
            raise ValueError("Incomplete member quotes or suspension status")
        for field in ("open", "close", "adv_volume", "isST", "up_limit", "down_limit"):
            quotes[field] = pd.to_numeric(quotes[field], errors="raise")
        active = quotes.loc[~quotes.suspended]
        if (not np.isfinite(active[["open", "close", "up_limit", "down_limit"]]).all().all()
                or active[["open", "close", "down_limit"]].le(0).any().any()
                or active.up_limit.lt(active.down_limit).any() or not active.isST.isin([0, 1]).all()):
            raise ValueError("Invalid tradable quote, price limits or ST flag")
        opening, close = packet["benchmark_open"], packet["benchmark_close"]
        if not all(math.isfinite(value) and value > 0 for value in (opening, close)):
            raise ValueError("Invalid benchmark NAV")
        actions = packet["actions"]
        if len({a["ts_code"] for a in actions}) != len(actions):
            raise ValueError("Expected consolidated company actions by security and ex-date")
        for action in actions:
            if (_iso(action["ex_date"]) != day or _iso(action["pay_date"]) < day
                    or not all(math.isfinite(action[k]) and action[k] >= 0 for k in ("cash", "shares"))):
                raise ValueError("Invalid dated corporate action")
        prior_signal = previous["signal"] if previous else None
        target = prior_signal["weights"] if prior_signal else None
        base = previous["benchmark_base"] if previous else None
        first_execution = previous is not None and base is None
        if first_execution:
            base = opening
        fixed = monthly_cost if previous and (first_execution or day[:7] != prior_day[:7]) else 0
        entered = bool(previous and previous.get("has_entered"))
        record = book_session(day, quotes, members, target, actions, previous, costs, capital, fixed, dividend_tax_rate,
                              exchanges=packet.get("exchanges", []),
                              turnover_cap=TURNOVER_CAP if version == "v2" and entered else None)
        if version == "v2":
            record["has_entered"] = entered or record["account"]["buy_notional"] > 0
        record["account"]["strategy_nav"] = record["account"]["nav_cny"] / capital
        record["account"]["benchmark_nav"] = close / base if base is not None else 1.0
        record.update(next_trade_date=next_day, benchmark_base=base, signal=None)
        features = packet["signal_inputs"]
        if features is not None:
            if date.fromisoformat(day).isocalendar()[:2] == date.fromisoformat(next_day).isocalendar()[:2]:
                raise ValueError("Weekly signal requires the final trading session of its week")
            history = features["risk_returns"]
            dates = [_iso(d) for d in history["dates"]]
            if (not dates or dates[-1] != day or dates != sorted(set(dates)) or len(dates) > 60
                    or len(set(history["codes"])) != len(history["codes"])):
                raise ValueError("Risk window must be unique, chronological and end on the signal date")
            returns = pd.DataFrame(history["values"], columns=history["codes"], index=dates, dtype=float)
            scores = pd.Series(features["scores"], dtype=float).rename_axis("ts_code").reset_index(name="score")
            reference = pd.Series(features["weights"], dtype=float)
            if (not np.isfinite(scores.score).all() or not np.isfinite(reference).all()
                    or reference.lt(0).any() or not 0 < reference.sum() <= 1.000001):
                raise ValueError("Invalid signal scores or benchmark weights")
            weights, audit = signal_targets(scores, reference, returns, pd.Series(features["industry"]),
                                             quotes, members, record, staged=version == "v2")
            record["signal"] = {"signal_date": day, "execution_date": next_day, "weights": weights, **audit}
        elif date.fromisoformat(day).isocalendar()[:2] != date.fromisoformat(next_day).isocalendar()[:2]:
            raise ValueError("Weekly signal inputs are missing")
        return record

    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    return save_one_session(database, profile, day, step, digest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="One dated JSON market and signal snapshot")
    parser.add_argument("--database", type=Path, required=True, help="Separate SQLite account per strategy version")
    parser.add_argument("--version", choices=("v1", "v2"), default="v1")
    parser.add_argument("--monthly-cost", type=float, default=0.0)
    parser.add_argument("--dividend-tax-rate", type=float, default=0.0)
    args = parser.parse_args(argv)
    record, created = run_day(args.database, args.input, monthly_cost=args.monthly_cost,
                              dividend_tax_rate=args.dividend_tax_rate, version=args.version)
    print(json.dumps({"trade_date": record["trade_date"], "status": "committed" if created else "already_committed",
                      "new_orders": len(record["orders"]) if created else 0, "nav_cny": record["account"]["nav_cny"]}))


if __name__ == "__main__":
    main()
