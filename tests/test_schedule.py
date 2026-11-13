import pandas as pd

from myquant.simulation.backtest import is_weekly_signal_day


def test_last_trade_day_of_week_is_signal_day() -> None:
    days = pd.Series(pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]))
    flags = is_weekly_signal_day(days)
    assert flags.tolist() == [False, False, False, False, True, True]

