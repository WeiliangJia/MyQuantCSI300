import pandas as pd
import pytest

from scripts.simulate_mq300_factors_2024 import KEY, SOURCE, simulate


def test_factor_uses_reference_close_but_never_crosses_missing_quote():
    days = ["2023-12-29", "2024-01-02", "2024-01-03", "2024-01-04"]
    required = pd.DataFrame({"trade_date": days, "ts_code": "A"})
    quotes = required.iloc[[0, 1, 3]].assign(
        open=[20, 10.2, 11], close=[20, 11, 11], pre_close=[20, 10, 11],
        volume=100, archive_path="test.csv",
    )
    ledger, issues = simulate(required, quotes)
    assert ledger.adj_factor.tolist() == [1, 2]
    assert ledger.adj_open.iloc[1] / ledger.adj_close.iloc[0] - 1 == pytest.approx(.02)
    assert set(ledger.source) == {SOURCE}
    assert issues.reason.tolist() == [
        "missing_quote_no_suspension_evidence", "prior_gap_blocks_factor_chain",
    ]


def test_archive_conflict_is_not_resolved_by_file_order():
    required = pd.DataFrame([["2024-01-02", "A"], ["2024-01-02", "B"]], columns=KEY)
    quotes = required.assign(open=10, close=10, pre_close=10, volume=100, archive_path="one.csv")
    alternative = quotes.iloc[:1].assign(close=11, archive_path="two.csv")
    ledger, issues = simulate(required, pd.concat([quotes, quotes, alternative]))
    assert ledger.ts_code.tolist() == ["B"]
    assert issues.reason.tolist() == ["conflicting_archive_quotes"]
