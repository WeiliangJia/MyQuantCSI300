import numpy as np
import pandas as pd
import pytest

from myquant.simulation.paper import ResearchCosts2024, book_session
from myquant.simulation.paper_day import signal_targets


def test_partial_step_uses_actual_holdings_and_preserves_risk_constraints():
    codes = pd.Index([f"{i:06d}.SZ" for i in range(100)])
    prices = pd.DataFrame({"isST": 0, "suspended": False}, index=codes)
    prices.loc[codes[0], "suspended"] = True
    held = pd.Series(0.0, index=codes)
    held.iloc[:50] = 0.98 / 50
    args = (
        pd.DataFrame({"ts_code": codes, "score": np.arange(100)}),
        pd.Series(0.01, index=codes),
        pd.DataFrame(0.0, index=range(60), columns=codes),
        pd.Series("sector", index=codes),
        prices,
        set(codes),
    )

    def account(weights):
        return {
            "account": {"nav_cny": 100_000},
            "positions": [
                {"ts_code": c, "market_value": w * 100_000} for c, w in weights.items() if w
            ],
            "has_entered": True,
        }

    assert signal_targets(*args, account(held))[0] is None
    for _ in range(2):
        target, audit = signal_targets(*args, account(held), staged=True)
        assert audit["decision"] == "approved"
        assert 0 < audit["step_fraction"] < 1
        target = pd.Series(target).reindex(codes, fill_value=0)
        delta = target - held
        assert max(delta.clip(lower=0).sum(), -delta.clip(upper=0).sum()) == pytest.approx(0.20)
        assert target.iloc[0] == pytest.approx(held.iloc[0])
        assert target.sum() == pytest.approx(0.98)
        held = target


def test_opening_budget_caps_actual_buys_and_excludes_forced_sale():
    old, new = "600001.SH", "000002.SZ"
    quotes = pd.DataFrame(
        {
            "open": 10.0,
            "close": 10.0,
            "suspended": False,
            "isST": 0,
            "adv_volume": 10_000_000,
            "down_limit": 9.0,
            "up_limit": 11.0,
        },
        index=[old, new],
    )
    previous = {
        "account": {"nav_cny": 100000, "cash": 50000},
        "positions": [{"ts_code": old, "settled": 5000, "unsettled_lots": []}],
        "receivables": [],
    }
    record = book_session(
        "2026-11-18",
        quotes,
        {new},
        {new: 0.98},
        [],
        previous,
        ResearchCosts2024(),
        100000,
        0,
        0,
        turnover_cap=0.20,
    )
    sale, purchase = record["orders"]
    assert sale["forced_exit"] and sale["filled_quantity"] == 5000
    assert not purchase["forced_exit"] and 0 < purchase["notional"] <= 20_000
    assert record["account"]["cash_error"] == 0
