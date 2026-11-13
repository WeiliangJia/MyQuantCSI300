import numpy as np
import pandas as pd

from myquant.research.factors import DailyFactorEngine, combine_factor_groups


def test_daily_factor_engine_outputs_alpha_without_future_financials() -> None:
    codes = [f"{i:06d}.SZ" for i in range(12)]
    dates = pd.bdate_range("2024-01-01", periods=70)
    bars = pd.DataFrame(
        [
            {"ts_code": code, "trade_date": date, "close": 10 + i + j * 0.03}
            for i, code in enumerate(codes) for j, date in enumerate(dates)
        ]
    )
    basic = pd.DataFrame(
        [
            {"ts_code": code, "trade_date": date, "pb": 1 + i / 10, "pe_ttm": 10 + i,
             "turnover_rate": 1 + (j % 5), "total_mv": 1e6 + i * 1e5}
            for i, code in enumerate(codes) for j, date in enumerate(dates)
        ]
    )
    moneyflow = pd.DataFrame(
        [{"ts_code": code, "trade_date": dates[-1], "net_mf_amount": 100 + i, "amount": 1000} for i, code in enumerate(codes)]
    )
    financials = pd.DataFrame(
        [{"ts_code": code, "metric": "roe", "value": 0.1, "available_at": "2024-03-31"} for code in codes]
    )
    factor_frame = DailyFactorEngine().compute(bars, basic, financials, moneyflow)
    combined = combine_factor_groups(factor_frame)
    assert len(combined) == len(codes)
    assert np.isfinite(combined["alpha"]).all()
