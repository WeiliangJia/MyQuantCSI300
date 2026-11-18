from pathlib import Path

import pandas as pd


def test_weekly_momentum_excludes_unavailable_and_nonfinite_scores(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import backtest_mq300_pilot_2024 as pilot

    days = pd.bdate_range("2024-01-01", periods=66)
    market = pd.MultiIndex.from_product(
        [days, ["valid", "missing", "zero_denominator"]], names=pilot.KEY,
    ).to_frame(index=False).assign(adj_close=10.0, suspended=False, isST=0)
    signal_day = days[-2]
    denominator_day = days[days.get_loc(signal_day) - 60]
    denominator = market.trade_date.eq(denominator_day)
    market.loc[denominator & market.ts_code.eq("missing"), "adj_close"] = float("nan")
    market.loc[denominator & market.ts_code.eq("zero_denominator"), "adj_close"] = 0.0
    members = market[pilot.KEY].assign(available_at=market.trade_date)
    monkeypatch.setattr(pilot, "year_members", lambda days: members)

    schedule, ranked = pilot.weekly_scores(
        market, pd.DataFrame({"trade_date": days}), "momentum", start=days[0],
    )

    assert schedule.execution_date.iloc[0] < days[-1]
    assert ranked.signal_date.tolist() == [signal_day]
    assert ranked.execution_date.tolist() == [days[-1]]
    assert ranked.ts_code.tolist() == ["valid"]
    assert ranked.score.tolist() == [0.0]
