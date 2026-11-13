import pandas as pd

from myquant.research.universe import build_tradable_universe


def test_tradable_universe_applies_explicit_exclusion() -> None:
    date = "2024-01-02"
    constituents = pd.DataFrame({"trade_date": [date, date], "ts_code": ["000001.SZ", "000651.SZ"], "weight": [0.5, 0.5]})
    bars = pd.DataFrame({
        "trade_date": [date, date], "ts_code": ["000001.SZ", "000651.SZ"],
        "open": [10.0, 10.0], "high": [10.0, 10.0], "low": [10.0, 10.0], "close": [10.0, 10.0], "volume": [1000, 1000],
    })

    result = build_tradable_universe(constituents, bars, min_history_days=1, excluded_ts_codes={"000651.SZ"})

    assert result["ts_code"].tolist() == ["000001.SZ"]
