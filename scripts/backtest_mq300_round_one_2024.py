"""Extend the three frozen MQ300 strategies and diagnose weekly factor ranks."""
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_mq300_pilot_2024 import (
    CAPITAL, END, HOLDINGS, KEY, STRATEGY_NAMES, market_with_limits, run_strategy,
    select_targets, weekly_scores,
)
from myquant.simulation.broker import CostModel
from reconcile_mq300_feeds_2024 import archived_bars, fetch_pair
from verify_mq300_2024 import LOCAL_AUDIT, local_inputs


OUTPUT = LOCAL_AUDIT.parent / "mq300-2024-round-one"

# Actual gross cash per original share, including combined special dividends.
CASH_CORRECTIONS = [
    ("300750.SZ", "2024-04-30", 5.028),
    ("601989.SH", "2024-08-01", 0.01),
    ("600837.SH", "2024-08-08", 0.10),
    ("601898.SH", "2024-08-20", 0.555),
    ("601059.SH", "2024-08-21", 0.046),
    ("600837.SH", "2024-10-18", 0.03),
    ("601059.SH", "2024-10-25", 0.037),
    ("002352.SZ", "2024-11-07", 1.40),
    ("300760.SZ", "2024-11-07", 1.65),
    ("000538.SZ", "2024-11-25", 1.213),
    ("601985.SH", "2024-12-02", 0.02),
]


def corporate_actions():
    paths = sorted(Path("data/raw/eastmoney/sharebonus_calendar").rglob("*.csv"))
    fields = ["SECUCODE", "EX_DIVIDEND_DATE", "REPORT_DATE", "PRETAX_BONUS_RMB", "BONUS_IT_RATIO"]
    raw = pd.concat([pd.read_csv(path, usecols=fields) for path in paths]).drop_duplicates()
    if len(raw) != 4774 or raw.duplicated(fields[:3]).any():
        raise ValueError("Incomplete or conflicting archived corporate-action calendar")
    raw["trade_date"] = pd.to_datetime(raw.EX_DIVIDEND_DATE).dt.normalize()
    events = raw.rename(columns={"SECUCODE": "ts_code"}).loc[raw.trade_date.dt.year.eq(2024)]
    events = events.assign(
        cash=events.PRETAX_BONUS_RMB.fillna(0) / 10,
        shares=events.BONUS_IT_RATIO.fillna(0) / 10,
    ).groupby(KEY)[["cash", "shares"]].sum()
    for ticker, day, amount in CASH_CORRECTIONS:
        key = (pd.Timestamp(day), ticker)
        if key not in events.index:
            events.loc[key, "shares"] = 0.0
        events.loc[key, "cash"] = amount
    return events.reset_index()


def independent_quotes(codes):
    frames = []
    for code in sorted(codes):
        (quotes, quote_error), (factors, factor_error) = fetch_pair(code, offline=True)
        if quote_error or factor_error:
            raise ValueError(f"Missing cached feed for {code}: {quote_error or factor_error}")
        quotes = pd.merge_asof(
            quotes.sort_values("trade_date"), factors.drop(columns="ts_code").sort_values("trade_date"),
            on="trade_date",
        )
        quotes["adjusted_open"] = quotes.open * quotes.sina_factor
        step = quotes.sina_factor / quotes.sina_factor.shift()
        quotes["pre_close"] = np.floor(quotes.close.shift() / step * 100 + 0.50000001) / 100
        quotes["factor_step"] = step
        frames.append(quotes)
    return pd.concat(frames, ignore_index=True)


def supplement_exits(quotes, independent, selected, status):
    last = quotes.sort_values("trade_date").groupby("ts_code").tail(1)[[*KEY, "isST"]]
    growth = independent.merge(last, on="ts_code", suffixes=("", "_last"), validate="many_to_one")
    growth = growth.loc[growth.ts_code.isin(selected) & growth.trade_date.gt(growth.trade_date_last)]
    covered_suspensions = pd.MultiIndex.from_frame(status.assign(
        trade_date=pd.to_datetime(status.trade_date),
    )[KEY])
    if pd.MultiIndex.from_frame(growth[KEY]).isin(covered_suspensions).any():
        raise ValueError("Supplemental quotes overlap confirmed full-day suspensions")
    return growth


def diagnostic(ranked, schedule, quotes, benchmark):
    schedule = schedule.assign(label_end=schedule.execution_date.shift(-1))
    rows = ranked.merge(schedule[["signal_date", "label_end"]], on="signal_date", validate="many_to_one")
    opens = quotes[KEY + ["adjusted_open"]]
    for date, price in [("execution_date", "entry_open"), ("label_end", "exit_open")]:
        rows = rows.merge(opens.rename(columns={"trade_date": date, "adjusted_open": price}),
                          on=[date, "ts_code"], how="left", validate="many_to_one")
    bench = benchmark.set_index("trade_date").open_nav_baostock
    rows["benchmark_return"] = rows.label_end.map(bench) / rows.execution_date.map(bench) - 1
    rows["stock_return"] = rows.exit_open / rows.entry_open - 1
    rows["forward_excess_return"] = rows.stock_return - rows.benchmark_return
    weekly = []
    for day, group in rows.groupby("signal_date"):
        valid = group.dropna(subset=["forward_excess_return"])
        weekly.append({
            "signal_date": day, "execution_date": group.execution_date.iloc[0],
            "label_end": group.label_end.iloc[0], "eligible": len(group), "labeled": len(valid),
            "coverage": len(valid) / len(group),
            "rank_ic": valid.score.corr(valid.forward_excess_return, method="spearman") if len(valid) > 1 else np.nan,
        })
    return rows, pd.DataFrame(weekly)


def performance(nav, fills, gross):
    nav = nav.merge(gross[["trade_date", "strategy_nav"]].rename(
        columns={"strategy_nav": "gross_nav"},
    ), on="trade_date", validate="one_to_one")
    daily = fills.groupby("trade_date")[["notional", "fees", "slippage_cost"]].sum()
    nav = nav.join(daily, on="trade_date")
    nav[daily.columns] = nav[daily.columns].fillna(0)
    nav["trading_cost"] = nav.fees + nav.slippage_cost
    nav["turnover_rate"] = nav.notional / nav.equity.shift(fill_value=CAPITAL)
    groups = [("total", nav)] + list(nav.groupby(nav.trade_date.dt.strftime("%Y-%m")))
    stats = []
    for period, rows in groups:
        start = nav.loc[nav.trade_date.lt(rows.trade_date.iloc[0])]
        base = start.iloc[-1] if not start.empty else dict(strategy_nav=1, benchmark_nav=1, gross_nav=1, relative_nav=1)
        last = rows.iloc[-1]
        net_return = last.strategy_nav / base["strategy_nav"] - 1
        benchmark_return = last.benchmark_nav / base["benchmark_nav"] - 1
        gross_return = last.gross_nav / base["gross_nav"] - 1
        relative = rows.relative_nav / base["relative_nav"]
        stats.append({
            "period": period, "sessions": len(rows), "net_return": net_return,
            "gross_return": gross_return, "benchmark_return": benchmark_return,
            "excess_return": net_return - benchmark_return,
            "gross_excess_return": gross_return - benchmark_return,
            "max_relative_drawdown": (relative / relative.cummax().clip(lower=1) - 1).min(),
            "turnover": rows.turnover_rate.sum(), "fees": rows.fees.sum(),
            "slippage": rows.slippage_cost.sum(), "trading_cost": rows.trading_cost.sum(),
        })
    return nav, pd.DataFrame(stats)


def slippage_costs(market, fills):
    fills = fills.merge(market[KEY + ["open"]], on=KEY, validate="many_to_one")
    move = (fills.price - fills.open) * np.where(fills.side.eq("BUY"), 1, -1)
    return fills.assign(
        slippage_cost=(move * fills.filled_quantity).where(fills.filled_quantity.gt(0), 0.0),
    )


def write_summary(tables, start, sessions, supplemental):
    perf = tables["performance"]
    weeks = tables["weekly_ic"].dropna(subset=["label_end"])
    lines = [
        "# MQ300.EW 第一轮策略诊断", "",
        f"共同执行区间为 {start.date()} 至 {END.date()}，共 {sessions} 日。",
        "三条策略沿用 20 只股票等权、98% 目标仓位和周频次日开盘撮合。"
        "基准使用已验收的 MQ300.EW，以共同执行首日开盘归一。", "",
        "| 策略 | 扣费前收益 | 扣费后收益 | 扣费后超额 | 最大相对回撤 | 双边换手 | 成交成本 | 周均 Rank IC | 标签覆盖 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, display in STRATEGY_NAMES.items():
        row = perf.loc[perf.period.eq("total") & perf.strategy.eq(name)].iloc[0]
        ic = weeks.loc[weeks.strategy.eq(name)]
        coverage = ic.labeled.sum() / ic.eligible.sum()
        lines.append(
            f"| {display} | {row.gross_return:+.2%} | {row.net_return:+.2%} | "
            f"{row.excess_return * 100:+.2f} 个百分点 | {row.max_relative_drawdown:.2%} | "
            f"{row.turnover:.2%} | {row.trading_cost:,.2f} 元 | {ic.rank_ic.mean():+.4f} | {coverage:.2%} |"
        )
    lines.extend([
        "", f"同期基准收益为 {perf.benchmark_return.iloc[0]:+.2%}。"
        f"完整开盘到开盘标签涵盖 {weeks.signal_date.nunique()} 个周窗口。"
        "最后一次调仓没有区间内的下一执行日，保留回测结果并将该周标签留空。"
        "标签使用腾讯开盘和新浪因子，首尾缺开盘价时留空。每周按各策略有效股票计算 Spearman Rank IC。",
        "", "扣费前收益来自关闭佣金、税费和滑点后的独立回放，持股数量会随现金变化。"
        "换手为每日买卖成交额之和除以前一日收盘权益后累加，含初始建仓。"
        "分月相对回撤从上月末的相对净值起算。", "",
        f"补充行情包含 {len(supplemental):,} 条调出后可用的腾讯未复权报价，"
        "仅用于持仓估值和退出撮合。补充报价的参考价由腾讯前收和新浪因子推算，ST 沿用 BaoStock 最后一条记录。"
        "现金按税前金额于除息日入账，送转按除权日增加持股，零碎股向下取整。"
        "触及涨跌停的方向继续拒单，计入滑点后的模拟成交价限制在涨跌停价之间。", "",
        "分月与全期指标见 `performance.csv`，周频诊断见 `weekly_ic.csv`。"
        "复跑命令为 `python scripts/backtest_mq300_round_one_2024.py`。",
    ])
    (OUTPUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def percentile_blend(momentum, low_volatility):
    keys = ["signal_date", "execution_date", "ts_code"]
    common = momentum[keys + ["score"]].merge(
        low_volatility[keys + ["score"]], on=keys, validate="one_to_one",
        suffixes=("_momentum", "_low_volatility"),
    )
    percentiles = common.groupby("signal_date")[["score_momentum", "score_low_volatility"]].rank(
        method="average", pct=True,
    )
    return common.assign(score=percentiles.mean(axis=1))


def prepare_round(retention_rank=None, *, add_blend=False):
    benchmark = pd.read_csv(LOCAL_AUDIT.parent / "mq300-2024-nav.csv", parse_dates=["trade_date"])
    bars, status, _ = local_inputs()
    bars["trade_date"] = pd.to_datetime(bars.trade_date)
    quotes = archived_bars(bars, extra_fields=("isST",))
    base_market = market_with_limits(quotes, status)
    warmup = sorted(bars.loc[bars.trade_date.lt("2024-01-01"), "trade_date"].unique())
    calendars = {name: weekly_scores(base_market, benchmark, name, benchmark.trade_date.iloc[0], warmup)
                 for name in STRATEGY_NAMES}
    scores = {name: ranked for name, (_, ranked) in calendars.items()}
    execution_days = calendars["momentum"][0].execution_date
    capacity = pd.concat({name: frame.groupby("execution_date").size() for name, frame in scores.items()}, axis=1)
    capacity = capacity.reindex(execution_days)
    viable = capacity.loc[capacity.ge(HOLDINGS).all(axis=1)]
    start = viable.index.min()
    expected_days = benchmark.trade_date.loc[benchmark.trade_date.between(start, END)]
    counts = capacity.loc[capacity.index >= start]
    if not counts.ge(HOLDINGS).all().all() or pd.isna(start):
        raise ValueError("Incomplete common weekly signal coverage")
    scores = {name: frame.loc[frame.execution_date.ge(start)] for name, frame in scores.items()}
    if add_blend:
        scores["blend"] = percentile_blend(scores["momentum"], scores["low_volatility"])
    targets = {name: select_targets(frame[["signal_date", "execution_date"]].drop_duplicates(), frame)
               for name, frame in scores.items()}

    selected = set().union(*(frame.ts_code for frame in targets.values()))
    buffered = None
    if retention_rank is not None:
        buffered = scores["reversal"].sort_values(
            ["signal_date", "score", "ts_code"], ascending=[True, False, True],
        ).groupby("signal_date").head(retention_rank).assign(target_weight=0.98 / HOLDINGS)
        selected.update(buffered.ts_code)
    independent = independent_quotes(set(quotes.ts_code))
    extra = supplement_exits(quotes, independent, selected, status)
    market = market_with_limits(pd.concat([quotes, extra], ignore_index=True), status)
    events = corporate_actions()
    changes = market.loc[market.trade_date.between(start, END) & market.ts_code.isin(selected)
                         & market.factor_step.notna() & ~np.isclose(market.factor_step, 1, atol=1e-10, rtol=0), KEY]
    unmatched = changes.merge(events[KEY], on=KEY, how="left", indicator=True)
    if unmatched._merge.eq("left_only").any():
        raise ValueError(f"Unreconciled corporate actions:\n{unmatched.loc[unmatched._merge.eq('left_only')]}")
    events = events.loc[events.trade_date.between(start, END) & events.ts_code.isin(selected)]
    dividends = events.loc[events.cash.gt(0), [*KEY, "cash"]].rename(columns={"cash": "cash_per_share"})
    distributions = events.loc[events.shares.gt(0), [*KEY, "shares"]].rename(columns={"shares": "shares_per_share"})
    actions = pd.concat([dividends.assign(action_type="cash_dividend"),
                         distributions.assign(action_type="stock_distribution")], ignore_index=True)
    actions = actions.rename(columns={"trade_date": "ex_date"})
    huadian = market.ts_code.eq("600027.SH") & market.suspended & market.trade_date.ge("2024-07-25") & market.trade_date.lt("2024-08-02")
    market.loc[huadian, ["open", "close"]] -= 0.15
    return market, benchmark, actions, start, expected_days, targets, scores, independent, extra, buffered


def main():
    market, benchmark, actions, start, expected_days, targets, scores, independent, extra, _ = prepare_round()
    results = {name: [] for name in ("nav", "fills", "performance", "weekly_ic", "labels", "targets")}
    for name, target in targets.items():
        nav, fills = run_strategy(market, benchmark, target, actions, start=start, cap_fill_at_limit=True)
        gross, _ = run_strategy(
            market, benchmark, target, actions, start=start, cap_fill_at_limit=True,
            costs=CostModel(0, 0, 0, 0, 0, 0),
        )
        fills = slippage_costs(market, fills)
        nav, stats = performance(nav, fills, gross)
        ranked = scores[name]
        weeks = target[["signal_date", "execution_date"]].drop_duplicates().sort_values("execution_date")
        labels, ic = diagnostic(ranked, weeks, independent, benchmark)
        assert nav.trade_date.tolist() == expected_days.tolist()
        for output, frame in zip(results, (nav, fills, stats, ic, labels, target)):
            results[output].append(frame.assign(strategy=name))
        print(name, len(nav), len(weeks), len(fills), stats.iloc[0].to_dict(), flush=True)
    OUTPUT.mkdir(exist_ok=True)
    tables = {name: pd.concat(frames, ignore_index=True) for name, frames in results.items()}
    for name, frame in tables.items():
        frame.to_csv(OUTPUT / f"{name}.csv", index=False)
    extra[KEY + ["open", "close", "volume"]].to_csv(OUTPUT / "supplemental_quotes.csv", index=False)
    actions.to_csv(OUTPUT / "corporate_actions.csv", index=False)
    write_summary(tables, start, len(expected_days), extra)


if __name__ == "__main__":
    main()
