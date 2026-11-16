"""Run the December 2024 low-volatility pilot against the accepted MQ300.EW NAV."""
import numpy as np
import pandas as pd

from myquant.simulation.backtest import WeeklyOpenBacktester, is_weekly_signal_day
from myquant.simulation.broker import PaperBroker
from reconcile_mq300_feeds_2024 import archived_bars
from verify_mq300_2024 import LOCAL_AUDIT, local_inputs, year_members


START, END = pd.Timestamp("2024-12-02"), pd.Timestamp("2024-12-31")
CAPITAL = 1_000_000
HOLDINGS = 20
KEY = ["trade_date", "ts_code"]


def prepare_market(bars, status):
    quotes = bars.loc[bars.trade_date.ge("2024-10-08")].copy()
    quotes["trade_date"] = pd.to_datetime(quotes.trade_date)
    quotes = archived_bars(quotes, extra_fields=("isST",))
    if not quotes.isST.isin([0, 1]).all():
        raise ValueError("Missing BaoStock ST status")
    board = quotes.ts_code.str.startswith(("300", "301", "688"))
    rate = np.where(board, 0.20, np.where(quotes.isST.eq(1), 0.05, 0.10))
    # Half-up to the 0.01 tick using integer cents for the reference price.
    cents = np.rint(quotes.pre_close.to_numpy() * 100).astype("int64")
    rate_pct = np.rint(rate * 100).astype("int64")
    quotes["up_limit"] = ((cents * (100 + rate_pct) + 50) // 100) / 100
    quotes["down_limit"] = ((cents * (100 - rate_pct) + 50) // 100) / 100
    halted = status.loc[status.trade_date.ge("2024-10-08"), KEY].copy()
    halted["trade_date"] = pd.to_datetime(halted.trade_date)
    market = pd.concat(
        [quotes.assign(suspended=False), halted.assign(suspended=True, volume=0)], ignore_index=True,
    )
    market = market.sort_values(["ts_code", "trade_date"])
    if market.duplicated(KEY).any():
        raise ValueError("Duplicate quote or suspension key")
    carried = market.groupby("ts_code").close.ffill()
    market.loc[market.suspended, "open"] = carried
    market.loc[market.suspended, "close"] = carried
    return market


def weekly_targets(market, benchmark):
    days = benchmark.trade_date
    schedule = pd.DataFrame({
        "signal_date": days, "execution_date": days.shift(-1),
    }).loc[is_weekly_signal_day(days)]
    schedule = schedule.loc[schedule.execution_date.between(START, END)]
    members = year_members(days).rename(columns={"trade_date": "signal_date"})
    members["signal_date"] = pd.to_datetime(members.signal_date)
    members = members.loc[pd.to_datetime(members.available_at).le(members.signal_date)]
    closes = market.pivot(index="trade_date", columns="ts_code", values="adj_close").reindex(days)
    risk = closes.pct_change(fill_method=None).rolling(20).std().stack().rename("volatility").reset_index()
    eligible = market.loc[~market.suspended & market.isST.eq(0), KEY]
    eligible = eligible.merge(risk, on=KEY, validate="one_to_one").rename(columns={"trade_date": "signal_date"})
    ranked = schedule.merge(members[["signal_date", "ts_code"]], on="signal_date").merge(
        eligible, on=["signal_date", "ts_code"], validate="one_to_one",
    )
    selected = ranked.sort_values(["signal_date", "volatility", "ts_code"]).groupby("signal_date").head(HOLDINGS)
    if not selected.groupby("signal_date").size().reindex(schedule.signal_date).eq(HOLDINGS).all():
        raise ValueError("Not enough observable candidates for every weekly signal")
    return selected.assign(target_weight=0.98 / HOLDINGS)


def december_dividends(market):
    calendar = pd.read_json(LOCAL_AUDIT / "independent_dividend_calendar.json", convert_dates=False)
    calendar["trade_date"] = pd.to_datetime(calendar.trade_date)
    events = calendar.loc[calendar.trade_date.between(START, END)].copy()
    if events.BONUS_IT_RATIO.fillna(0).ne(0).any():
        raise ValueError("The pilot cash-dividend replay requires a window without stock distributions")
    events["cash_per_share"] = events.PRETAX_BONUS_RMB / 10
    # The implementation notice (2024-082) fills the reviewed calendar omission.
    nuclear = {"ts_code": "601985.SH", "trade_date": START, "cash_per_share": 0.02}
    events = pd.concat([events[[*KEY, "cash_per_share"]], pd.DataFrame([nuclear])], ignore_index=True)
    factor_events = market.loc[
        market.trade_date.between(START, END) & market.factor_step.notna() & market.factor_step.ne(1), KEY
    ]
    if events.duplicated(KEY).any() or not events.cash_per_share.gt(0).all() or not pd.MultiIndex.from_frame(
        factor_events
    ).isin(pd.MultiIndex.from_frame(events[KEY])).all():
        raise ValueError("Incomplete or invalid December company-action inputs")
    return events.rename(columns={"trade_date": "ex_date"}).assign(action_type="cash_dividend")


def main():
    benchmark = pd.read_csv(LOCAL_AUDIT.parent / "mq300-2024-nav.csv", parse_dates=["trade_date"])
    bars, status, _ = local_inputs()
    market = prepare_market(bars, status)
    targets = weekly_targets(market, benchmark)
    actions = december_dividends(market)
    broker = PaperBroker(cash=CAPITAL)
    result = WeeklyOpenBacktester(broker, block_open_at_limit=True).run(market, targets, actions)
    fills = result.fills.assign(trade_date=lambda frame: pd.to_datetime(frame.trade_date)).merge(
        targets[["execution_date", "signal_date"]].drop_duplicates().rename(
            columns={"execution_date": "trade_date"},
        ), on="trade_date", how="left", validate="many_to_one",
    )
    fills["notional"] = fills.filled_quantity * fills.price.fillna(0)
    executed = fills.loc[fills.filled_quantity.gt(0)]
    bounds = executed.merge(market[[*KEY, "up_limit", "down_limit"]], on=KEY, validate="many_to_one")
    if executed.empty or not bounds.price.between(bounds.down_limit, bounds.up_limit).all():
        raise ValueError("Missing executions or simulated price outside the price band")
    if not fills.signal_date.lt(fills.trade_date).all():
        raise ValueError("Execution must follow its weekly signal")

    baseline = benchmark.loc[benchmark.trade_date.between(START, END)]
    strategy = result.nav.loc[result.nav.trade_date.between(START, END)]
    nav = strategy.merge(baseline[["trade_date", "nav_baostock"]], on="trade_date", validate="one_to_one")
    if not nav.trade_date.equals(baseline.trade_date.reset_index(drop=True)) or (nav.cash < -1e-8).any():
        raise ValueError("Inconsistent benchmark calendar or negative cash")
    nav["equity"] = nav.pop("nav")
    nav["strategy_nav"] = nav.equity / CAPITAL
    nav["benchmark_nav"] = nav.pop("nav_baostock") / baseline.open_nav_baostock.iloc[0]
    nav["relative_nav"] = nav.strategy_nav / nav.benchmark_nav
    nav["excess_return"] = nav.strategy_nav - nav.benchmark_nav
    for name in ("strategy", "benchmark", "relative"):
        series = nav[f"{name}_nav"]
        nav[f"{name}_daily_return"] = series / series.shift(fill_value=1) - 1
        nav[f"{name}_drawdown"] = series / series.cummax().clip(lower=1) - 1

    first, last = nav.iloc[0], nav.iloc[-1]
    report = f"""# MQ300.EW 策略试跑

区间为 {first.trade_date.date()} 至 {last.trade_date.date()}，共 {len(nav)} 个交易日。
以 100 万元现金起步，每周最后一个交易日收盘按过去 20 个交易日复权收益波动率从低到高选取 20 只当日成分股，剔除当日停牌和 ST 股票。下一交易日开盘以合计 98% 仓位等权调仓。成交受整手、T+1、可用现金和过去 20 条日线平均成交量的 1% 上限约束。
撮合采用 BaoStock 未复权价格，模型按开盘触及涨跌停价拒单，税前现金分红在除息日入账。使用项目默认佣金、印花税和滑点。基准使用已验收的 BaoStock 版 MQ300.EW，以区间首日开盘重新归一。

| 指标 | 策略 | MQ300.EW |
| --- | ---: | ---: |
| 区间收益 | {last.strategy_nav - 1:.4%} | {last.benchmark_nav - 1:.4%} |
| 最大回撤 | {nav.strategy_drawdown.min():.4%} | {nav.benchmark_drawdown.min():.4%} |

- 策略收益减基准收益为 {last.excess_return * 100:+.4f} 个百分点。
- 相对净值期末收益为 {last.relative_nav - 1:+.4%}，最大相对回撤为 {nav.relative_drawdown.min():.4%}。
- 共 {targets.execution_date.nunique()} 次周频调仓，{len(executed)} 笔有成交，{fills.status.eq("REJECTED").sum()} 笔拒单，成交费用合计 {fills.fees.sum():,.2f} 元。

相对净值为策略净值除以基准净值。回撤使用日末净值并计入起始基值 1。未完成的订单在下一次周频调仓时按最新目标重新计算。结果反映本月试跑区间。

复跑命令为 `python scripts/backtest_mq300_pilot_2024.py`。
"""
    output = LOCAL_AUDIT.parent / "mq300-2024-pilot"
    output.mkdir(exist_ok=True)
    nav.to_csv(output / "nav.csv", index=False)
    fills.to_csv(output / "fills.csv", index=False)
    (output / "summary.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
