"""Compare an equal-percentile momentum and low-volatility signal with its components."""
import pandas as pd

from backtest_mq300_round_one_2024 import OUTPUT as ROUND_ONE
from backtest_mq300_round_one_2024 import prepare_round
from backtest_mq300_round_two_2024 import replay


OUTPUT = ROUND_ONE.parent / "mq300-2024-round-three"
STRATEGIES = {"momentum": "中期动量", "low_volatility": "低波动", "blend": "50% 动量＋50% 低波动"}


def main():
    market, benchmark, actions, start, expected_days, targets, _, _, _, _ = prepare_round(add_blend=True)
    keys = ["signal_date", "execution_date"]
    schedule = targets["momentum"][keys].drop_duplicates().reset_index(drop=True)
    runs = {}
    prior_file = ROUND_ONE / "nav.csv"
    prior = pd.read_csv(prior_file, parse_dates=["trade_date"], float_precision="round_trip") if prior_file.exists() else None
    for name in STRATEGIES:
        target = targets[name]
        pd.testing.assert_frame_equal(target[keys].drop_duplicates().reset_index(drop=True), schedule)
        nav, fills, monthly, metrics = replay(market, benchmark, actions, start, target, None)
        if nav.trade_date.tolist() != expected_days.tolist():
            raise ValueError(f"Incomplete backtest calendar: {name}")
        if prior is not None and name != "blend":
            old = prior.loc[prior.strategy.eq(name)].drop(columns="strategy").reset_index(drop=True)
            pd.testing.assert_frame_equal(nav[old.columns], old, check_exact=True)
        runs[name] = nav, fills, monthly, metrics

    OUTPUT.mkdir(exist_ok=True)
    for index, filename in enumerate(("nav", "fills", "performance")):
        pd.concat([run[index].assign(strategy=name) for name, run in runs.items()],
                  ignore_index=True).to_csv(OUTPUT / f"{filename}.csv", index=False)
    metrics = pd.DataFrame({name: run[3] for name, run in runs.items()}).T
    metrics.to_csv(OUTPUT / "metrics.csv", index_label="strategy")

    report = [
        "# MQ300.EW 第三轮因子组合对照", "",
        f"区间为 {start.date()} 至 {expected_days.iloc[-1].date()}，共 {len(expected_days)} 个交易日、"
        f"{len(schedule)} 次周频调仓。同期基准收益为 {metrics.benchmark_return.iloc[0]:+.2%}。",
        "三组各选 20 只等权、目标总仓位 98%，沿用相同的成本、次日开盘撮合和公司行动处理。", "",
        "每个信号日在动量与低波动均有效的股票中分别计算百分位。动量越强、波动越低，得分越高。"
        "并列原始分数使用平均名次，合成分数为两项百分位各占 50%，最终并列按证券代码排序。"
        "两个单因子沿用各自原有的有效股票池。", "",
        "| 策略 | 扣费后收益 | 扣费后超额 | 最大相对回撤 | 建仓后累计双边换手 | 成交成本 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, label in STRATEGIES.items():
        row = metrics.loc[name]
        report.append(
            f"| {label} | {row.net_return:+.2%} | {row.excess_return * 100:+.2f} 个百分点 | "
            f"{row.max_relative_drawdown:.2%} | {row.turnover_after_entry:.2%} | {row.trading_cost:,.2f} 元 |"
        )
    report.extend([
        "", "超额收益为策略收益减同期 MQ300.EW 收益。相对回撤基于策略净值除以基准净值。"
        "双边换手为买卖成交额之和除以前一日收盘权益后逐日累加。"
        "净值明细保留首日建仓换手，表中换手从建仓后起算。", "",
        "复跑命令为 `python scripts/backtest_mq300_round_three_2024.py`。",
    ])
    report = "\n".join(report) + "\n"
    (OUTPUT / "summary.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
