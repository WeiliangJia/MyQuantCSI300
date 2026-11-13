import pandas as pd

from myquant.data.quality import check_daily_bars


def test_ohlc_quality_detects_invalid_range() -> None:
    frame = pd.DataFrame(
        {
            "ts_code": ["000001.SZ"], "trade_date": ["2024-01-02"],
            "open": [10.0], "high": [9.0], "low": [8.0], "close": [10.0], "vol": [100],
        }
    )
    results = {result.name: result for result in check_daily_bars(frame)}
    assert not results["daily_bar_ohlc"].passed

