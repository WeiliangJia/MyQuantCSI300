"""Compare short-term reversal with and without a ten-rank holding buffer."""
import math

import pandas as pd

from backtest_mq300_pilot_2024 import HOLDINGS, run_strategy
from backtest_mq300_round_one_2024 import OUTPUT as ROUND_ONE
from backtest_mq300_round_one_2024 import performance, prepare_round, slippage_costs
from myquant.simulation.broker import CostModel


OUTPUT = ROUND_ONE.parent / "mq300-2024-round-two"
RETENTION_RANK = 30


def replay(market, benchmark, actions, start, targets, buffered_holdings):
    options = dict(start=start, cap_fill_at_limit=True, buffered_holdings=buffered_holdings)
    nav, fills = run_strategy(market, benchmark, targets, actions, measure_risk=True, **options)
    gross, _ = run_strategy(
        market, benchmark, targets, actions, costs=CostModel(0, 0, 0, 0, 0, 0), **options,
    )
    fills = slippage_costs(market, fills)
    nav, stats = performance(nav, fills, gross)
    active = nav.strategy_daily_return - nav.benchmark_daily_return
    overview = {
        **stats.iloc[0].to_dict(),
        "turnover_after_entry": nav.turnover_rate.iloc[1:].sum(),
        "tracking_error": active.std(ddof=1) * math.sqrt(252),
        "mean_max_stock_weight": nav.max_stock_weight.mean(),
        "peak_stock_weight": nav.max_stock_weight.max(),
        "mean_top_five_weight": nav.top_five_weight.mean(),
        "peak_top_five_weight": nav.top_five_weight.max(),
        "mean_holdings": nav.holdings.mean(),
        "orders": len(fills),
        "orders_with_fills": int(fills.filled_quantity.gt(0).sum()),
        "quantity_fill_rate": fills.filled_quantity.sum() / fills.requested_quantity.sum(),
    }
    return nav, fills, stats, overview


def main():
    market, benchmark, actions, start, expected_days, targets, _, _, _, buffered = prepare_round(
        retention_rank=RETENTION_RANK,
    )
    original = targets["reversal"]
    schedule = ["signal_date", "execution_date"]
    pd.testing.assert_frame_equal(
        original[schedule].drop_duplicates().reset_index(drop=True),
        buffered[schedule].drop_duplicates().reset_index(drop=True),
    )
    baseline = replay(market, benchmark, actions, start, original, None)
    buffer = replay(market, benchmark, actions, start, buffered, HOLDINGS)
    for nav, _, _, _ in (baseline, buffer):
        if nav.trade_date.tolist() != expected_days.tolist():
            raise ValueError("Replay does not cover the common evaluation dates")

    prior_file = ROUND_ONE / "nav.csv"
    if prior_file.exists():
        prior = pd.read_csv(prior_file, parse_dates=["trade_date"], float_precision="round_trip")
        prior = prior.loc[prior.strategy.eq("reversal")].drop(columns="strategy").reset_index(drop=True)
        pd.testing.assert_frame_equal(baseline[0][prior.columns], prior, check_exact=True)

    runs = {"baseline": baseline, "buffer_top30": buffer}
    OUTPUT.mkdir(exist_ok=True)
    for index, name in enumerate(("nav", "fills", "performance")):
        pd.concat([run[index].assign(variant=variant) for variant, run in runs.items()],
                  ignore_index=True).to_csv(OUTPUT / f"{name}.csv", index=False)
    metrics = pd.DataFrame({variant: run[3] for variant, run in runs.items()}).T
    metrics.to_csv(OUTPUT / "metrics.csv", index_label="variant")
    a, b = baseline[3], buffer[3]
    lines = [
        "# MQ300.EW 第二轮：短期反转持仓缓冲", "",
        f"区间为 {start.date()} 至 {expected_days.iloc[-1].date()}，共 {len(expected_days)} 个交易日。"
        f"共 {original.execution_date.nunique()} 次周频调仓，基准收益 {a['benchmark_return']:+.2%}。",
        "两组均使用 5 日反转。原版按信号日前 20 名调仓。缓冲版优先保留排名仍在前 30 的实际持仓，"
        "空位按信号排名补到 20 只，等权目标仓位仍为 98%。两组沿用同一行情、基准、调仓日和成交规则。", "",
        "| 指标 | 原版 | 前 30 保留 |", "| --- | ---: | ---: |",
    ]
    for key, label, fmt in (
        ("gross_return", "扣费前收益", "+.2%"), ("net_return", "扣费后收益", "+.2%"),
        ("excess_return", "扣费后超额", "+.2%"),
        ("max_relative_drawdown", "最大相对回撤", ".2%"),
        ("turnover_after_entry", "建仓后双边累计换手", ".2%"),
        ("trading_cost", "成交成本（元）", ",.2f"),
        ("tracking_error", "实现跟踪误差（年化）", ".2%"),
        ("peak_stock_weight", "日末最大单股权重峰值", ".2%"),
        ("mean_top_five_weight", "日末前五大持仓平均权重", ".2%"),
        ("quantity_fill_rate", "申报股数成交率", ".2%"),
    ):
        lines.append(f"| {label} | {a[key]:{fmt}} | {b[key]:{fmt}} |")
    lines.extend([
        "", "持仓集中度按实际日末市值除以账户权益计算。跟踪误差按每日策略与基准收益之差的样本标准差年化。"
        "集中度和跟踪误差用来观察缓冲影响。", "",
        "复跑命令为 `python scripts/backtest_mq300_round_two_2024.py`。",
    ])
    (OUTPUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
