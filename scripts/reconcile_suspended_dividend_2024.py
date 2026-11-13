"""Reconcile the reviewed Huadian event without releasing annual NAV."""
from pathlib import Path

import numpy as np
import pandas as pd

from myquant.research.benchmark_weights import monthly_equal_weight_benchmark


def huadian_inputs(factors):
    code = "600027.SH"
    before, ex_date, resume = "2024-07-18", "2024-07-25", "2024-08-02"
    bars = factors.copy()
    stock = bars.loc[bars.ts_code.eq(code)].set_index("trade_date")
    prior, resumed = stock.loc[before], stock.loc[resume]
    # 2024-027: RMB 0.15, ex-date and payment July 25. 2024-033: resume August 2.
    cash = 0.15
    np.testing.assert_allclose(resumed.pre_close, prior.close - cash)
    np.testing.assert_allclose(resumed.factor_step, prior.close / resumed.pre_close)
    later = bars.ts_code.eq(code) & bars.trade_date.ge(resume)
    bars.loc[later, ["adj_open", "adj_close", "adj_factor"]] /= resumed.factor_step
    bars.loc[bars.ts_code.eq(code) & bars.trade_date.eq(resume), "factor_step"] = 1.0
    dividends = pd.DataFrame([{
        "trade_date": ex_date, "ts_code": code, "adjusted_cash_per_share": cash * prior.adj_factor,
    }])
    return bars, dividends


def main():
    folder = Path("data/exports/mq300-2024-simulated")
    code = "600027.SH"
    before, ex_date, resume = "2024-07-18", "2024-07-25", "2024-08-02"
    cash = 0.15
    factors = pd.read_json(folder / "factors.json", dtype={"trade_date": str})
    adjusted, dividends = huadian_inputs(factors)
    bars = adjusted.loc[adjusted.ts_code.eq(code) & adjusted.trade_date.isin([before, resume])].copy()
    prior, resumed = (bars.set_index("trade_date").loc[day] for day in (before, resume))
    bars.loc[bars.trade_date.eq(before), "adj_open"] = prior.adj_close
    status = pd.read_json(folder / "confirmed_suspensions.json", dtype={"trade_date": str})
    status = status.loc[status.ts_code.eq(code) & status.trade_date.between(before, resume)]
    days = sorted(set(status.trade_date) | {before, resume})
    members = pd.DataFrame({"trade_date": days, "ts_code": code})
    result = monthly_equal_weight_benchmark(
        members, bars, status, expected_members=1, suspended_dividends=dividends,
    )
    if not result.audit.ready.all() or len(result.nav) != len(days):
        raise ValueError("Incomplete event replay")
    nav = result.nav.copy()
    ex = nav.loc[nav.trade_date.astype(str).eq(ex_date)].iloc[0]
    last = nav.iloc[-1]
    np.testing.assert_allclose(nav.previous_nav + nav.overnight_pnl + nav.intraday_pnl, nav.nav)
    np.testing.assert_allclose(ex.dividend_cash, cash / prior.close)
    np.testing.assert_allclose(ex.nav, 1)
    np.testing.assert_allclose(last.dividend_cash, 0)
    # Pending monthly rebalance reinvests all available cash at the resume open.
    np.testing.assert_allclose(last.open_nav, (resumed.open + cash) / prior.close)
    np.testing.assert_allclose(last.nav, last.open_nav * resumed.close / resumed.open)
    nav["security_value"] = nav.nav - nav.cash
    nav.to_json(folder / "huadian_suspended_dividend_reconciliation.json",
                orient="records", date_format="iso", double_precision=15)
    print(nav.to_string(index=False))
    changes = factors.loc[factors.factor_step.ne(1)].copy()
    changes["period"] = changes.trade_date.str[:4]
    print("Factor changes by year:", changes.period.value_counts().to_dict())
    changes[["ts_code", "trade_date", "previous_trade_date", "previous_close", "pre_close",
             "factor_step", "archive_path"]].to_json(folder / "corporate_action_candidates.json",
                                                     orient="records", double_precision=15)


if __name__ == "__main__":
    main()
