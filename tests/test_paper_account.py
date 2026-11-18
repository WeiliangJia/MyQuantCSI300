import pandas as pd
import pytest

from myquant.simulation.paper import FEE_FIELDS, ResearchCosts2024, book_session, save_sessions


def test_account_restarts_and_reconciles_dividends_forced_exits_and_costs(tmp_path):
    days = ["2024-03-08", "2024-03-11", "2024-03-12", "2024-03-13"]
    stock = "688001.SH"
    market = pd.DataFrame({"open": [20.0], "close": [20.0], "suspended": [False], "isST": [0],
                           "adv_volume": [20_100], "up_limit": [24.0], "down_limit": [16.0]}, index=[stock])
    action = {"ts_code": stock, "cash": 0.1, "shares": 0, "pay_date": days[3],
              "payment_evidence": "fixture"}
    database = tmp_path / "account.sqlite3"
    costs = ResearchCosts2024()

    def step(day, previous):
        quotes = market.assign(suspended=day == days[2])
        return book_session(day, quotes, {stock} if day < days[2] else set(),
                            {stock: 0.5} if day == days[1] else None,
                            [action] if day == days[2] else [], previous, costs, 100_000,
                            15 if day == days[0] else 0, 0.1)

    first = save_sessions(database, "fixture", days[:2], step)
    purchase = first[1]["orders"][0]
    assert purchase["filled_quantity"] == 201
    assert purchase["status"] == "PARTIALLY_FILLED"
    assert sum(purchase[name] for name in FEE_FIELDS) == pytest.approx(purchase["fees"])
    assert purchase["half_spread"] + purchase["impact"] == pytest.approx(2.01)
    completed = save_sessions(database, "fixture", days, step)
    assert completed[:2] == first
    assert completed[0]["account"]["fixed_cost"] == 15
    assert sum(row["account"]["fixed_cost"] for row in completed) == 15
    assert completed[2]["account"]["receivable"] == 20.1
    assert completed[2]["orders"][0]["reason"] == "suspended_or_missing_open"
    closing = completed[3]
    assert closing["orders"][0]["forced_exit"] and closing["orders"][0]["filled_quantity"] == 201
    assert closing["account"]["dividend_tax"] == 2.01
    assert closing["account"]["receivable"] == 0
    assert all(row["account"]["cash_error"] == 0 for row in completed)
    assert save_sessions(database, "fixture", days, step) == completed
    with pytest.raises(ValueError, match="changed"):
        save_sessions(database, "different", days, step)
