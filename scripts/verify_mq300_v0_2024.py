"""Rebuild the local MQ300.EW V0 reports and verify the 2024 reference results."""

import numpy as np
import pandas as pd

import backtest_mq300_round_one_2024 as round_one
import backtest_mq300_round_three_2024 as round_three
import backtest_mq300_round_two_2024 as round_two
from verify_mq300_2024 import LOCAL_AUDIT, export_annual_nav

from myquant.simulation.backtest import is_weekly_signal_day


def main():
    export_annual_nav()
    annual = pd.read_csv(LOCAL_AUDIT.parent / "mq300-2024-nav.csv", parse_dates=["trade_date"])
    dates = annual.trade_date
    if len(dates) != 242 or dates.dt.strftime("%Y-%m-%d").iloc[[0, -1]].tolist() != [
        "2024-01-02", "2024-12-31",
    ]:
        raise ValueError("V0 annual benchmark calendar differs from the 2024 reference")
    np.testing.assert_allclose(
        annual[["nav_baostock", "nav_tencent_sina"]].iloc[-1],
        [1.157612514, 1.157607979], rtol=0, atol=1e-9,
        err_msg="V0 year-end NAV (BaoStock and Tencent/Sina)",
    )

    evaluation = dates.ge("2024-03-11")
    evaluation_dates = dates.loc[evaluation].tolist()
    execution_dates = dates.shift(-1).loc[is_weekly_signal_day(dates)].dropna()
    execution_dates = execution_dates.loc[execution_dates.ge("2024-03-11")].tolist()
    if len(evaluation_dates) != 199 or len(execution_dates) != 43:
        raise ValueError("V0 comparison requires 199 trading days and 43 weekly rebalances")

    # Net return and trading cost from the accepted three rounds, independent of the new reports.
    rounds = (
        (round_one, "performance.csv", "strategy", {
            "low_volatility": (0.21534267, 33916.33),
            "momentum": (-0.00822819, 26554.51),
            "reversal": (0.14015933, 79830.43),
        }),
        (round_two, "metrics.csv", "variant", {"buffer_top30": (0.14661229, 76579.42)}),
        (round_three, "metrics.csv", "strategy", {"blend": (0.12062717, 35555.21)}),
    )
    for replay, metrics_file, key, reference in rounds:
        replay.main()
        stats = pd.read_csv(replay.OUTPUT / metrics_file).query("period == 'total'").set_index(key)
        nav = pd.read_csv(replay.OUTPUT / "nav.csv", parse_dates=["trade_date"])
        orders = pd.read_csv(replay.OUTPUT / "fills.csv", parse_dates=["trade_date"])
        for name, (net_return, trading_cost) in reference.items():
            daily = nav.loc[nav[key].eq(name)]
            if daily.trade_date.tolist() != evaluation_dates:
                raise ValueError(f"{name}: V0 evaluation dates differ from the reference")
            order_dates = orders.loc[orders[key].eq(name), "trade_date"].drop_duplicates().tolist()
            if order_dates != execution_dates:
                raise ValueError(f"{name}: V0 rebalance dates differ from the reference")
            np.testing.assert_allclose(
                [daily.strategy_nav.iloc[-1] - 1, stats.at[name, "net_return"]],
                net_return, rtol=0, atol=1e-8, err_msg=f"{name}: V0 net return",
            )
            np.testing.assert_allclose(
                stats.at[name, "trading_cost"], trading_cost,
                rtol=0, atol=0.005, err_msg=f"{name}: V0 trading cost",
            )

    print("V0 verified: 242 benchmark days, 199 strategy days, 43 rebalances, 5 strategies.")


if __name__ == "__main__":
    main()
