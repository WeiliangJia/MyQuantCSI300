"""Run a resumable V1 research account against the audited 2024 MQ300.EW archive."""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backfill_mq300_paper_inputs import ROOT
from backtest_mq300_pilot_2024 import KEY, STRATEGY_NAMES, market_with_limits, weekly_scores
from backtest_mq300_round_one_2024 import corporate_actions, independent_quotes, supplement_exits
from reconcile_mq300_feeds_2024 import archived_bars
from verify_mq300_2024 import checked_replay, exchange_days, local_inputs, year_members

from myquant.simulation.paper import COST_FIELDS, ResearchCosts2024, book_session, fen, save_sessions


OUTPUT = Path("data/exports/mq300-v1-2024-paper")
START, FIRST_SIGNAL = "2024-03-11", "2024-03-08"
CAPITAL = 10_000_000.0
# Issuer notices resolve the five cash-event differences left by the vendor cross-check.
NOTICE_PAYMENTS = [
    ("300750.SZ", "2024-04-30", 5.028, "https://static.cninfo.com.cn/finalpage/2024-04-23/1219767154.PDF"),
    ("600803.SH", "2024-08-01", 0.91, "https://epaper.stcn.com/att/202407/26/ZQ26B048-XX_eBook.pdf"),
    ("601898.SH", "2024-08-20", 0.555, "https://www.chinacoalenergy.com/cms_files/jcms1/web2/site/attach/0/5c706ac8437542fc8b925e9dc0c84260.pdf"),
    ("000538.SZ", "2024-11-25", 1.213, "https://disc.static.szse.cn/disc/disk03/finalpage/2024-11-19/8d8c9376-bd08-4002-ae11-d2590e632b0f.PDF"),
    ("601985.SH", "2024-12-02", 0.02, "https://static.cninfo.com.cn/finalpage/2024-11-25/1221831318.PDF"),
]


def inputs(industry_path, payment_path):
    bars, status, dividends = local_inputs()
    days = exchange_days(bars)
    members = year_members(days)
    base = checked_replay(members, bars, status, suspended_dividends=dividends)
    benchmark = base.nav.rename(columns={"nav": "benchmark_nav", "open_nav": "benchmark_open"})
    benchmark["trade_date"] = pd.to_datetime(benchmark.trade_date)
    bars["trade_date"] = pd.to_datetime(bars.trade_date)
    quotes = archived_bars(bars, extra_fields=("isST",))
    independent = independent_quotes(set(quotes.ts_code))
    extra = supplement_exits(quotes, independent, set(quotes.ts_code), status)
    market = market_with_limits(pd.concat([quotes, extra], ignore_index=True), status)
    market["adv_volume"] = market.groupby("ts_code").volume.transform(
        lambda values: values.shift(1).rolling(20).mean()
    )
    # The suspended position is marked ex-dividend while the account books the cash entitlement.
    huadian = (market.ts_code.eq("600027.SH") & market.suspended
               & market.trade_date.between("2024-07-25", "2024-08-01"))
    market.loc[huadian, ["open", "close"]] -= 0.15
    warmup = bars.loc[bars.trade_date.lt("2024-01-01"), "trade_date"].unique()
    ranked = {}
    for strategy in STRATEGY_NAMES:
        schedule, scores = weekly_scores(market, benchmark, strategy, pd.Timestamp(START), warmup)
        ranked[strategy] = scores.set_index(["signal_date", "ts_code"]).score
    combined = pd.concat(ranked, axis=1, join="inner")
    combined = combined.groupby(level="signal_date").rank(pct=True).mean(axis=1).rename("score").reset_index()
    risk_prices = independent.assign(adjusted_close=lambda frame: frame.close * frame.sina_factor).pivot(
        index="trade_date", columns="ts_code", values="adjusted_close",
    ).reindex(pd.DatetimeIndex(warmup).union(days).sort_values())
    halted = status.assign(trade_date=lambda frame: pd.to_datetime(frame.trade_date), halted=True).pivot(
        index="trade_date", columns="ts_code", values="halted",
    ).reindex_like(risk_prices).eq(True)
    risk_prices = risk_prices.mask(risk_prices.isna() & halted, risk_prices.ffill())
    risk_returns = risk_prices.pct_change(fill_method=None)
    industry = pd.read_csv(industry_path, dtype=str)
    if industry.duplicated(["ts_code", "available_at"]).any() or industry.source_date.gt(industry.available_at).any():
        raise ValueError("Industry archive contains conflicting or future-dated classifications")
    actions = corporate_actions().copy()
    actions["pay_date"] = actions.trade_date.dt.strftime("%Y-%m-%d")
    actions["payment_evidence"] = "assumed_ex_date"
    actions["payment_source_url"] = ""
    if payment_path.is_file():
        confirmed = pd.read_csv(payment_path, dtype=str)
        previous_session = dict(zip(days[1:].strftime("%Y-%m-%d"), days[:-1].strftime("%Y-%m-%d")))
        confirmed = confirmed.loc[confirmed.trade_date.map(previous_session).eq(confirmed.record_date)]
        confirmed["trade_date"] = pd.to_datetime(confirmed.trade_date)
        actions = actions.merge(confirmed[KEY + ["pay_date"]], on=KEY, how="left", validate="one_to_one",
                                suffixes=("", "_verified"))
        verified = actions.pay_date_verified.notna()
        actions.loc[verified, "payment_evidence"] = "baostock_matched_terms"
        actions.loc[verified, "pay_date"] = actions.loc[verified, "pay_date_verified"]
        actions = actions.drop(columns="pay_date_verified")
    for code, day, amount, source in NOTICE_PAYMENTS:
        selected = actions.ts_code.eq(code) & actions.trade_date.eq(day)
        if selected.sum() != 1:
            raise ValueError(f"Expected one reviewed cash action for {code} on {day}")
        actions.loc[selected, "cash"] = amount
        actions.loc[selected, "pay_date"] = day
        actions.loc[selected, "payment_evidence"] = "issuer_notice"
        actions.loc[selected, "payment_source_url"] = source
    return benchmark, base.weights, market, members, schedule, combined, risk_returns, industry, actions


def signal_targets(scores, reference, returns, industry, quotes, members, record):
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
    attempts = []
    for enhancement in (0.20, 0.10, 0.0):
        proposed = frozen + available * ((1 - enhancement) * core / core.sum() + enhancement * tilt)
        active = proposed - reference
        material = active.index[active.abs().gt(1e-12)]
        active_returns = risk[material].dropna().dot(active.loc[material])
        tracking_error = float(active_returns.std(ddof=1) * np.sqrt(252)) if len(active_returns) >= 40 else None
        sector = float((proposed.groupby(names).sum() - base_sector).abs().max())
        change = (proposed - held).where(~mandatory, 0)
        turnover = float(max(change.clip(lower=0).sum(), -change.clip(upper=0).sum()))
        checks = {"enhancement": enhancement, "tracking_error": tracking_error, "risk_sessions": len(active_returns),
                  "industry_deviation": sector, "single_stock_excess": float((proposed - limit).max()),
                  "turnover": turnover, "first_build": current.empty}
        attempts.append(checks)
        if (tracking_error is not None and tracking_error <= 0.08 and sector <= 0.05
                and checks["single_stock_excess"] <= 1e-10 and (turnover <= 0.20 or current.empty)):
            return proposed.loc[proposed.gt(0)].to_dict(), {"decision": "approved", **checks, "attempts": attempts}
    return None, {"decision": "constraints_blocked", "attempts": attempts}


def export(output, journal, settings):
    account = pd.DataFrame([{"trade_date": row["trade_date"], **row["account"]}
                            for row in journal if row["trade_date"] >= START])
    account.to_csv(output / "account.csv", index=False)
    for field in ("positions", "orders", "actions"):
        entries = [{"trade_date": row["trade_date"], **entry} for row in journal for entry in row[field]]
        pd.DataFrame(entries).to_csv(output / f"{field}.csv", index=False)
    decisions = [row["signal"] for row in journal if row["signal"]]
    pd.DataFrame([{k: v for k, v in row.items() if k not in {"weights", "attempts"}} for row in decisions]).to_csv(
        output / "risk_checks.csv", index=False,
    )
    pd.DataFrame([{"signal_date": signal["signal_date"], "execution_date": signal["execution_date"],
                   "ts_code": code, "target_weight": weight}
                  for signal in decisions for code, weight in (signal["weights"] or {}).items()]).to_csv(
        output / "targets.csv", index=False,
    )
    if account.empty:
        return
    totals = {name: round(float(account[name].sum()), 2) for name in (*COST_FIELDS, "total_cost")}
    fills = [order for row in journal for order in row["orders"]]
    paid = [action for row in journal for action in row["actions"] if action["event"] == "dividend_paid"]
    strategy_return = float(account.nav_cny.iloc[-1] / CAPITAL - 1)
    benchmark_return = float(account.benchmark_nav.iloc[-1] - 1)
    active = account.strategy_nav.pct_change() - account.benchmark_nav.pct_change()
    relative = account.strategy_nav / account.benchmark_nav
    report = {
        "mode": "historical_replay", "start": account.trade_date.iloc[0], "end": account.trade_date.iloc[-1],
        "sessions": len(account), "initial_cash": CAPITAL, "ending_nav": float(account.nav_cny.iloc[-1]),
        "return": strategy_return, "benchmark_return": benchmark_return, "excess_return": strategy_return - benchmark_return,
        "tracking_error": float(active.std() * np.sqrt(252)) if active.notna().sum() >= 2 else None,
        "max_relative_drawdown": float((relative / relative.cummax().clip(lower=1) - 1).min()),
        "signals": len(decisions), "approved_signals": sum(row["decision"] == "approved" for row in decisions),
        "orders": len(fills), "filled_orders": sum(bool(order["filled_quantity"]) for order in fills),
        "rejected_orders": sum(order["status"] == "REJECTED" for order in fills),
        "forced_exit_orders": sum(order["forced_exit"] for order in fills),
        "dividend_payments_with_evidence": sum(row["payment_evidence"] != "assumed_ex_date" for row in paid),
        "dividend_payments_assuming_ex_date": sum(row["payment_evidence"] == "assumed_ex_date" for row in paid),
        "max_cash_error": float(account.cash_error.abs().max()), "costs_cny": totals, **settings,
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--industry", type=Path, default=ROOT / "2024" / "industries.csv")
    parser.add_argument("--payments", type=Path, default=ROOT / "2024" / "payments.csv")
    parser.add_argument("--through", default="2024-12-31")
    parser.add_argument("--monthly-cost", type=float, default=0.0)
    parser.add_argument("--dividend-tax-rate", type=float, default=0.0)
    args = parser.parse_args()
    if not np.isfinite(args.monthly_cost) or args.monthly_cost < 0 or not 0 <= args.dividend_tax_rate <= 1:
        parser.error("Expected a nonnegative monthly cost and a dividend tax rate in [0, 1]")
    through = pd.Timestamp(args.through).date().isoformat()
    benchmark, weights, market, members, schedule, scores, returns, industry, actions = inputs(args.industry, args.payments)
    costs = ResearchCosts2024()
    settings = {"monthly_cost": fen(args.monthly_cost), "dividend_tax_rate": args.dividend_tax_rate,
                "payment_timing": "matched_payout_date_else_ex_date_assumption",
                "cost_model": {key: value for key, value in asdict(costs).items()
                               if key not in {"sh_transfer_bps", "buy_slippage_bps", "sell_slippage_bps"}}}
    fingerprint = sha256(json.dumps(settings, sort_keys=True).encode())
    point_in_time_members = members.drop(columns="generated_at", errors="ignore")
    for table in (benchmark, weights, market, point_in_time_members, schedule, scores, returns, industry, actions):
        fingerprint.update(pd.util.hash_pandas_object(table).values.tobytes())
    fingerprint.update(b"mq300-v1-replay-2024")
    evaluation = benchmark.loc[benchmark.trade_date.ge(FIRST_SIGNAL)].copy()
    first_open = benchmark.loc[benchmark.trade_date.eq(START), "benchmark_open"].iloc[0]
    evaluation["trade_date"] = evaluation.trade_date.dt.strftime("%Y-%m-%d")
    calendar = evaluation.set_index("trade_date")
    day_quotes = {day.date().isoformat(): group.set_index("ts_code") for day, group in market.groupby("trade_date")}
    day_members = {str(day)[:10]: set(group.ts_code) for day, group in members.groupby("trade_date")}
    day_weights = {str(day)[:10]: group.set_index("ts_code").weight for day, group in weights.groupby("trade_date")}
    day_actions = {day.date().isoformat(): group.to_dict("records") for day, group in actions.groupby("trade_date")}
    next_execution = dict(zip(schedule.signal_date.dt.strftime("%Y-%m-%d"), schedule.execution_date.dt.strftime("%Y-%m-%d")))
    days = calendar.index[calendar.index <= through].tolist()
    if not days:
        parser.error(f"--through must be on or after {FIRST_SIGNAL}")

    def step(day, previous):
        signal = previous["signal"] if previous else None
        target = signal["weights"] if signal and signal["execution_date"] == day else None
        month_open = day == START or (day > START and previous["trade_date"][:7] != day[:7])
        fixed = settings["monthly_cost"] if month_open else 0.0
        record = book_session(day, day_quotes[day], day_members[day], target, day_actions.get(day, []),
                              previous, costs, CAPITAL, fixed, args.dividend_tax_rate)
        record["account"]["benchmark_nav"] = float(calendar.at[day, "benchmark_nav"] / first_open)
        record["account"]["strategy_nav"] = record["account"]["nav_cny"] / CAPITAL
        record["signal"] = None
        if day in next_execution:
            industry_at_close = industry.loc[industry.available_at.le(day)].sort_values("available_at").drop_duplicates(
                "ts_code", keep="last",
            ).set_index("ts_code").industry
            proposed, audit = signal_targets(scores.loc[scores.signal_date.eq(day)], day_weights[day], returns.loc[:day],
                                              industry_at_close, day_quotes[day], day_members[day], record)
            record["signal"] = {"signal_date": day, "execution_date": next_execution[day], "weights": proposed, **audit}
        return record

    args.output.mkdir(parents=True, exist_ok=True)
    entries = save_sessions(args.output / "account.sqlite3", fingerprint.hexdigest(), days, step)
    export(args.output, entries, settings)


if __name__ == "__main__":
    main()
