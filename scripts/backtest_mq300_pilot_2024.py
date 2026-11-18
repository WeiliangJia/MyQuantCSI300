"""Run or compare the December 2024 strategy pilots against MQ300.EW."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

from myquant.simulation.backtest import WeeklyOpenBacktester, is_weekly_signal_day
from myquant.simulation.broker import PaperBroker
from reconcile_mq300_feeds_2024 import OHLC, archived_bars, fetch_pair
from verify_mq300_2024 import LOCAL_AUDIT, local_inputs, year_members


START, END = pd.Timestamp("2024-12-02"), pd.Timestamp("2024-12-31")
CAPITAL = 1_000_000
HOLDINGS = 20
KEY = ["trade_date", "ts_code"]
EXIT_MANIFEST = LOCAL_AUDIT / "strategy_exit_quotes_2024.json"
STRATEGY_NAMES = {
    "low_volatility": "低波动",
    "momentum": "中期动量",
    "reversal": "短期反转",
}


def exit_quotes(quotes, trading_days):
    if not EXIT_MANIFEST.is_file():
        raise ValueError("Run this script with --backfill-exits to prepare the exit quotes")
    sources = json.loads(EXIT_MANIFEST.read_text(encoding="utf-8"))
    frames = []
    for ticker, source in sources.items():
        path = Path(source["path"])
        if sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Exit quote archive differs from its manifest: {ticker}")
        tail = pd.read_csv(path, parse_dates=["trade_date"]).sort_values("trade_date")
        prior = quotes.loc[quotes.ts_code.eq(ticker)].sort_values("trade_date").iloc[-1]
        expected = trading_days.loc[trading_days.gt(prior.trade_date) & trading_days.le(END)]
        if not tail.ts_code.eq(ticker).all() or tail.trade_date.tolist() != expected.tolist():
            raise ValueError(f"Incomplete or unexpected exit quotes: {ticker}")
        # This period has no corporate actions for the supplemented names.
        previous_close = tail.close.shift(fill_value=prior.close)
        if not np.allclose(previous_close, tail.pre_close, atol=1e-8, rtol=0):
            raise ValueError(f"Review corporate action or missing exit quote: {ticker}")
        tail["adj_close"] = tail.close * prior.adj_factor
        frames.append(tail)
    return pd.concat(frames, ignore_index=True) if frames else quotes.iloc[:0]


def prepare_market(bars, status, warmup_start="2024-10-08", trading_days=None):
    quotes = bars.loc[bars.trade_date.ge(warmup_start)].copy()
    quotes["trade_date"] = pd.to_datetime(quotes.trade_date)
    quotes = archived_bars(quotes, extra_fields=("isST",))
    if trading_days is not None:
        quotes = pd.concat([quotes, exit_quotes(quotes, trading_days)], ignore_index=True)
    return market_with_limits(quotes, status.loc[status.trade_date.ge(warmup_start)])


def market_with_limits(quotes, status):
    quotes = quotes.copy()
    if not quotes.isST.isin([0, 1]).all():
        raise ValueError("Missing BaoStock ST status")
    board = quotes.ts_code.str.startswith(("300", "301", "688"))
    rate = np.where(board, 0.20, np.where(quotes.isST.eq(1), 0.05, 0.10))
    # Half-up to the 0.01 tick using integer cents for the reference price.
    cents = np.rint(quotes.pre_close.to_numpy() * 100).astype("int64")
    rate_pct = np.rint(rate * 100).astype("int64")
    quotes["up_limit"] = ((cents * (100 + rate_pct) + 50) // 100) / 100
    quotes["down_limit"] = ((cents * (100 - rate_pct) + 50) // 100) / 100
    halted = status[KEY].copy()
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


def weekly_scores(market, benchmark, strategy, start, warmup_days=()):
    days = benchmark.trade_date
    schedule = pd.DataFrame({
        "signal_date": days, "execution_date": days.shift(-1),
    }).loc[is_weekly_signal_day(days)]
    schedule = schedule.loc[schedule.execution_date.between(start, END)]
    members = year_members(days).rename(columns={"trade_date": "signal_date"})
    members["signal_date"] = pd.to_datetime(members.signal_date)
    members = members.loc[pd.to_datetime(members.available_at).le(members.signal_date)]
    history = pd.DatetimeIndex(warmup_days).union(pd.DatetimeIndex(days)).sort_values()
    closes = market.pivot(index="trade_date", columns="ts_code", values="adj_close").reindex(
        history.rename("trade_date"),
    )
    if strategy == "low_volatility":
        score = -closes.pct_change(fill_method=None).rolling(20).std()
    elif strategy == "momentum":
        score = closes.shift(5) / closes.shift(60) - 1
    elif strategy == "reversal":
        score = -(closes / closes.shift(5) - 1)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")
    scores = score.stack(future_stack=True).rename("score").reset_index()
    scores = scores.loc[np.isfinite(scores.score)]
    eligible = market.loc[~market.suspended & market.isST.eq(0), KEY]
    eligible = eligible.merge(scores, on=KEY, validate="one_to_one").rename(columns={"trade_date": "signal_date"})
    ranked = schedule.merge(members[["signal_date", "ts_code"]], on="signal_date").merge(
        eligible, on=["signal_date", "ts_code"], validate="one_to_one",
    )
    return schedule, ranked


def weekly_targets(market, benchmark, strategy="low_volatility", start=START, warmup_days=()):
    schedule, ranked = weekly_scores(market, benchmark, strategy, start, warmup_days)
    return select_targets(schedule, ranked)


def select_targets(schedule, ranked):
    selected = ranked.sort_values(
        ["signal_date", "score", "ts_code"], ascending=[True, False, True],
    ).groupby("signal_date").head(HOLDINGS)
    if not selected.groupby("signal_date").size().reindex(schedule.signal_date).eq(HOLDINGS).all():
        raise ValueError("Not enough observable candidates for every weekly signal")
    return selected.assign(
        target_weight=0.98 / HOLDINGS,
        eligible_count=selected.signal_date.map(ranked.groupby("signal_date").size()),
    )


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


def backfill_exits(market, benchmark):
    from myquant.data.archive import RawArchiver
    from myquant.data.free_adapters import BaoStockClient

    selected = set().union(*(weekly_targets(market, benchmark, name).ts_code for name in STRATEGY_NAMES))
    spans = market.loc[market.ts_code.isin(selected)].groupby("ts_code").trade_date.max()
    archive = RawArchiver(Path("data/raw"))
    client = BaoStockClient()
    sources = {}
    for ticker, last in spans.loc[spans.lt(END)].items():
        days = benchmark.trade_date.loc[benchmark.trade_date.gt(last) & benchmark.trade_date.le(END)]
        start, end = days.iloc[0].date().isoformat(), END.date().isoformat()
        reference, error = fetch_pair(ticker, offline=True)[0]
        if error:
            raise ValueError(f"Missing Tencent quote cache for {ticker}: {error['error']}")
        reference = reference.loc[reference.trade_date.isin(days)]
        bars = client.daily_bars(ticker, start, end).sort_values("trade_date")
        if not bars.ts_code.eq(ticker).all() or bars.trade_date.tolist() != days.dt.strftime("%Y-%m-%d").tolist():
            raise ValueError(f"BaoStock exit quotes do not cover the expected dates: {ticker}")
        compared = bars.assign(trade_date=pd.to_datetime(bars.trade_date)).merge(
            reference, on=KEY, validate="one_to_one", suffixes=("_baostock", "_tencent"),
        )
        if len(compared) != len(days) or not all(
            compared[f"{field}_baostock"].eq(compared[f"{field}_tencent"]).all() for field in OHLC
        ) or not (compared.volume_baostock - compared.volume_tencent).abs().le(50).all():
            raise ValueError(f"BaoStock and Tencent exit quotes differ: {ticker}")
        path, digest = archive.archive(
            "strategy_exit_daily", {"ts_code": ticker, "start": start, "end": end}, bars, source="baostock",
        )
        sources[ticker] = {"path": path.as_posix(), "sha256": digest}
        print(f"{ticker}: verified {len(bars)} exit quotes")
    temporary = EXIT_MANIFEST.with_suffix(".tmp")
    temporary.write_text(json.dumps(sources, indent=2), encoding="utf-8")
    temporary.replace(EXIT_MANIFEST)


def run_strategy(market, benchmark, targets, actions, start=START, costs=None, *,
                 cap_fill_at_limit=False, buffered_holdings=None, measure_risk=False):
    target_keys = pd.MultiIndex.from_frame(targets[["execution_date", "ts_code"]])
    if not target_keys.isin(pd.MultiIndex.from_frame(market[KEY])).all():
        raise ValueError("Missing next-open quote or confirmed suspension for a target")
    broker = PaperBroker(cash=CAPITAL, costs=costs)
    result = WeeklyOpenBacktester(
        broker, block_open_at_limit=True, cap_fill_at_limit=cap_fill_at_limit,
    ).run(market, targets, actions, buffered_holdings=buffered_holdings, measure_risk=measure_risk)
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

    baseline = benchmark.loc[benchmark.trade_date.between(start, END)]
    strategy = result.nav.loc[result.nav.trade_date.between(start, END)]
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
    return nav, fills


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--compare", action="store_true", help="Compare low volatility, momentum and reversal")
    mode.add_argument("--backfill-exits", action="store_true", help="Backfill and cross-check exit quotes")
    args = parser.parse_args()
    benchmark = pd.read_csv(LOCAL_AUDIT.parent / "mq300-2024-nav.csv", parse_dates=["trade_date"])
    bars, status, _ = local_inputs()
    market = prepare_market(
        bars, status, warmup_start="2024-08-01" if args.compare or args.backfill_exits else "2024-10-08",
        trading_days=benchmark.trade_date if args.compare else None,
    )
    if args.backfill_exits:
        backfill_exits(market, benchmark)
        return
    actions = december_dividends(market)
    if args.compare:
        export_comparison(market, benchmark, actions)
        return
    targets = weekly_targets(market, benchmark)
    nav, fills = run_strategy(market, benchmark, targets, actions)
    executed = fills.loc[fills.filled_quantity.gt(0)]

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


def export_comparison(market, benchmark, actions):
    daily_results, order_results, summary = [], [], []
    expected_schedule = None
    for strategy, name in STRATEGY_NAMES.items():
        targets = weekly_targets(market, benchmark, strategy)
        schedule = targets[["signal_date", "execution_date"]].drop_duplicates().reset_index(drop=True)
        if expected_schedule is None:
            expected_schedule = schedule
        else:
            pd.testing.assert_frame_equal(schedule, expected_schedule)
        nav, fills = run_strategy(market, benchmark, targets, actions)
        fills = fills.merge(
            market[[*KEY, "open"]].rename(columns={"open": "reference_open"}),
            on=KEY, how="left", validate="many_to_one",
        )
        executed = fills.filled_quantity.gt(0)
        adverse_move = (fills.price - fills.reference_open) * np.where(fills.side.eq("BUY"), 1, -1)
        fills["slippage_cost"] = (adverse_move * fills.filled_quantity).where(executed, 0.0)
        fills["trading_cost"] = fills.fees + fills.slippage_cost
        daily = fills.groupby("trade_date")[["notional", "fees", "slippage_cost", "trading_cost"]].sum()
        nav = nav.join(daily, on="trade_date")
        nav[daily.columns] = nav[daily.columns].fillna(0)
        nav["turnover_rate"] = nav.notional / nav.equity.shift(fill_value=CAPITAL)
        daily_results.append(nav.assign(strategy=strategy))
        order_results.append(fills.assign(strategy=strategy))

        last = nav.iloc[-1]
        summary.append({
            "strategy": strategy,
            "name": name,
            "strategy_return": last.strategy_nav - 1,
            "benchmark_return": last.benchmark_nav - 1,
            "excess_return": last.excess_return,
            "max_drawdown": nav.strategy_drawdown.min(),
            "benchmark_max_drawdown": nav.benchmark_drawdown.min(),
            "max_relative_drawdown": nav.relative_drawdown.min(),
            "turnover_rate": nav.turnover_rate.sum(),
            "turnover_after_initial_entry": nav.turnover_rate.iloc[1:].sum(),
            "fees": fills.fees.sum(),
            "slippage_cost": fills.slippage_cost.sum(),
            "trading_cost": fills.trading_cost.sum(),
            "orders": len(fills),
            "orders_with_fills": int(executed.sum()),
            "min_eligible": int(targets.eligible_count.min()),
            "max_eligible": int(targets.eligible_count.max()),
        })

    output = LOCAL_AUDIT.parent / "mq300-2024-factor-comparison"
    output.mkdir(exist_ok=True)
    pd.concat(daily_results, ignore_index=True).to_csv(output / "nav.csv", index=False)
    pd.concat(order_results, ignore_index=True).to_csv(output / "fills.csv", index=False)
    pd.DataFrame(summary).to_csv(output / "metrics.csv", index=False)
    report = [
        "# MQ300.EW 三策略对照", "",
        "区间为 2024-12-02 至 2024-12-31。三组以 100 万元起步，使用相同的周频信号日和次日开盘撮合，"
        "各选 20 只等权持有，目标总仓位为 98%。基准为已验收的 BaoStock 版 MQ300.EW，"
        "以区间首日开盘为 1。三组共用原始行情、停牌和分红输入及默认撮合、费用和滑点参数。", "",
        "为估值和卖出已调出指数的持仓，补取了 4 只股票 12 月 17 日至 31 日的 BaoStock 未复权行情，"
        "并与本地腾讯缓存核对。选股仍使用信号日成分。", "",
        "低波动使用 20 日复权收益标准差。动量比较信号日前第 5 和第 60 个交易日的复权收盘价。"
        "反转按最近 5 个交易日的复权收益从低到高排名。信号缺失的股票不参与对应策略排名。", "",
        "| 策略 | 收益 | 超额收益 | 最大相对回撤 | 双边换手（含建仓） | 双边换手（建仓后） | 成交成本（元） |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    report.extend(
        f"| {row['name']} | {row['strategy_return']:+.4%} | {row['excess_return'] * 100:+.4f} 个百分点 | "
        f"{row['max_relative_drawdown']:.4%} | {row['turnover_rate']:.2%} | "
        f"{row['turnover_after_initial_entry']:.2%} | {row['trading_cost']:,.2f} |"
        for row in summary
    )
    report.extend([
        "", "超额收益为策略累计收益减基准累计收益。相对回撤基于策略净值除以基准净值，再与此前峰值比较。",
        "双边换手为当日实际买卖成交额之和除以前一日收盘权益，首日分母使用初始资金，再按日累加。",
        "成交成本包含费用与相对开盘价的滑点，两项已通过成交价格和现金扣款计入策略净值。", "",
        "复跑命令为 `python scripts/backtest_mq300_pilot_2024.py --compare`。"
        "若本地补数清单缺失，先用 `--backfill-exits` 重建。",
    ])
    report = "\n".join(report) + "\n"
    (output / "summary.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
