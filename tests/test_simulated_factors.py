import pandas as pd
import pytest

from scripts.simulate_mq300_factors_2024 import KEY, SOURCE, backfill_tasks, simulate


def test_tasks_distinguish_exit_open_and_unconfirmed_suspension():
    days = ["2023-12-29", "2024-01-02"]
    members = pd.DataFrame([[days[0], "A"], [days[1], "B"]], columns=KEY)
    issues = pd.DataFrame([
        [days[0], "A", "no_positive_volume_no_suspension_evidence"],
        [days[1], "A", "missing_quote_no_suspension_evidence"],
        [days[1], "B", "prior_gap_blocks_factor_chain"],
    ], columns=KEY + ["reason"])
    tasks = backfill_tasks(issues, members, days)
    assert tasks.action.tolist() == ["verify_full_day_suspension", "backfill_quote"]
    assert tasks.warmup.tolist() == [True, False]
    assert tasks.exit_open_required.tolist() == [False, True]
    assert not tasks.suspension_confirmed.any()


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


def test_only_confirmed_suspension_bridges_a_missing_session():
    days = ["2024-01-02", "2024-01-03", "2024-01-04"]
    required = pd.DataFrame({"trade_date": days, "ts_code": "A"})
    quotes = required.iloc[[0, 2]].assign(
        open=10, close=10, pre_close=10, volume=100, archive_path="quotes.csv",
    )
    ledger, issues = simulate(required, quotes, {(days[1], "A")})
    assert issues.empty
    assert ledger.previous_trade_date.iloc[-1] == days[0]
    _, issues = simulate(required, quotes, {(days[2], "A")})
    assert issues.reason.tolist() == [
        "missing_quote_no_suspension_evidence", "suspension_quote_conflict",
    ]
