import pandas as pd

from myquant.simulation.backtest import WeeklyOpenBacktester
from myquant.simulation.broker import PaperBroker


def test_buffer_keeps_actual_holdings_after_a_rejected_entry():
    a, b, c = "000001.SZ", "000002.SZ", "000003.SZ"
    first, second = "2024-03-11", "2024-03-18"
    market = pd.DataFrame([
        {"trade_date": day, "ts_code": code, "open": 10, "close": 10,
         "adv_volume": 1_000_000, "suspended": (day, code) == (first, a)}
        for day in (first, second) for code in (a, b, c)
    ])
    targets = pd.DataFrame([
        {"execution_date": day, "ts_code": code, "target_weight": 0.49}
        for day, ranking in ((first, (a, b, c)), (second, (c, a, b))) for code in ranking
    ])
    broker = PaperBroker(100_000)
    result = WeeklyOpenBacktester(broker).run(
        market, targets, buffered_holdings=2, measure_risk=True,
    )
    assert result.fills.loc[result.fills.ts_code.eq(a), "status"].tolist() == ["REJECTED"]
    assert result.nav.retained_targets.tolist() == [0, 1]
    assert broker.position_quantity(b) > 0
    assert broker.position_quantity(c) > 0
