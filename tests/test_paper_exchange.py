import pandas as pd
import pytest

from myquant.research.benchmark_weights import monthly_equal_weight_benchmark
from myquant.simulation.paper import ResearchCosts2024, book_session


def test_unlisted_entitlement_is_valued_but_only_ordinary_shares_can_be_sold():
    old, new = "601059.SH", "601995.SH"
    previous = {
        "trade_date": "2026-10-16",
        "account": {"cash": 10000, "nav_cny": 35000},
        "receivables": [],
        "positions": [
            {"ts_code": code, "quantity": qty, "settled": qty, "unsettled_lots": []}
            for code, qty in ((old, 1001), (new, 300))
        ],
    }
    quotes = pd.DataFrame(
        {
            "open": [32.0],
            "close": [32.1],
            "adv_volume": [1_000_000],
            "down_limit": [29.0],
            "up_limit": [35.0],
            "suspended": [False],
            "isST": [0],
        },
        index=[new],
    )
    terms = {"old": old, "new": new, "ratio": 0.521}

    def session(day, prior, target, exchanges):
        return book_session(
            day, quotes, {new}, target, [], prior, ResearchCosts2024(), 10_000_000, 0, 0, exchanges
        )

    converted = session("2026-10-19", previous, {}, [{**terms, "kind": "convert"}])
    assert converted["actions"][0]["fraction_unallocated"] == "0.521"
    assert converted["orders"][0]["filled_quantity"] == 300
    assert converted["positions"][0]["locked_quantity"] == 521
    assert converted["account"]["market_value"] == pytest.approx(521 * 32.1)
    held = session("2026-10-20", converted, {}, [])
    assert held["orders"] == []
    listed = session("2026-10-29", held, None, [{**terms, "kind": "release"}])
    assert listed["positions"][0]["settled"] == 521
    assert listed["account"]["cash"] == converted["account"]["cash"]
    assert listed["account"]["nav_cny"] == held["account"]["nav_cny"]


def test_benchmark_values_locked_claim_before_rebalance():
    dates = ["2026-10-16", "2026-10-19", "2026-10-29"]
    members = pd.DataFrame({"trade_date": dates, "ts_code": ["A", "B", "B"]})
    bars = pd.DataFrame(
        [
            (dates[0], "A", 10, 10, False),
            (dates[1], "A", 11, 12, True),
            (dates[1], "B", 11, 12, False),
            (dates[2], "A", 13, 13, False),
            (dates[2], "B", 13, 13, False),
        ],
        columns=["trade_date", "ts_code", "adj_open", "adj_close", "mark_locked"],
    )
    status = bars.loc[bars.mark_locked, ["trade_date", "ts_code"]].assign(is_suspended=True)
    replay = monthly_equal_weight_benchmark(members, bars, status, expected_members=1)
    assert replay.audit.ready.all()
    assert replay.nav.nav.tolist() == pytest.approx([1, 1.2, 1.3])
    assert replay.weights.ts_code.tolist() == ["A", "A", "B"]
