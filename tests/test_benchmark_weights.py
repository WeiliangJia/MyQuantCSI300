import pandas as pd
import pytest
import numpy as np

from myquant.research.benchmark_weights import (
    EQUAL_WEIGHT_METHODOLOGY,
    EQUAL_WEIGHT_SOURCE,
    free_float_market_cap_weights,
    monthly_equal_weight_benchmark,
)


def _members(day: str, count: int = 3) -> pd.DataFrame:
    return pd.DataFrame([
        {"trade_date": day, "ts_code": f"{item:06d}.SZ", "is_member": True}
        for item in range(count)
    ])


def test_free_float_weights_are_pro_rata_and_sum_to_one() -> None:
    membership = _members("2026-07-24")
    valuations = pd.DataFrame([
        {"trade_date": "2026-07-24", "ts_code": "000000.SZ", "circ_mv": 10, "available_at": "2026-07-24"},
        {"trade_date": "2026-07-24", "ts_code": "000001.SZ", "circ_mv": 20, "available_at": "2026-07-24"},
        {"trade_date": "2026-07-24", "ts_code": "000002.SZ", "circ_mv": 30, "available_at": "2026-07-24"},
    ])
    result = free_float_market_cap_weights(membership, valuations, expected_members=3)
    assert result.audit.loc[0, "ready"]
    assert result.weights["weight"].sum() == 1.0
    assert result.weights.set_index("ts_code").loc["000002.SZ", "weight"] == 0.5
    assert set(result.weights["source"]) == {"free_float_market_cap"}


def test_free_float_weights_fail_closed_when_circulating_value_is_missing_or_future_known() -> None:
    membership = _members("2026-07-24")
    valuations = pd.DataFrame([
        {"trade_date": "2026-07-24", "ts_code": "000000.SZ", "circ_mv": 10, "available_at": "2026-07-24"},
        {"trade_date": "2026-07-24", "ts_code": "000001.SZ", "circ_mv": 20, "available_at": "2026-07-25"},
        {"trade_date": "2026-07-24", "ts_code": "000002.SZ", "circ_mv": 30, "available_at": "2026-07-24"},
    ])
    result = free_float_market_cap_weights(membership, valuations, expected_members=3)
    assert result.weights.empty
    assert result.audit.loc[0, "reason"] == "missing_or_nonpositive_circ_mv"


def test_free_float_weights_preserve_the_input_provenance() -> None:
    membership = _members("2026-07-24")
    valuations = pd.DataFrame([
        {"trade_date": "2026-07-24", "ts_code": f"{item:06d}.SZ", "circ_mv": item + 1,
         "source": "akshare_outstanding_share", "methodology": "close_times_outstanding_share_v1"}
        for item in range(3)
    ])
    result = free_float_market_cap_weights(membership, valuations, expected_members=3)
    assert set(result.weights["source"]) == {"akshare_outstanding_share"}
    assert set(result.weights["methodology"]) == {"close_times_outstanding_share_v1"}


def _daily_members(days: list[str], codes: tuple[str, ...] = ("000001.SZ", "000002.SZ")) -> pd.DataFrame:
    return pd.DataFrame([
        {"trade_date": day, "ts_code": code, "is_member": True}
        for day in days
        for code in codes
    ])


def _bar(day: str, code: str, open_price: float, close_price: float) -> dict[str, object]:
    return {
        "trade_date": day,
        "ts_code": code,
        "adj_open": open_price,
        "adj_close": close_price,
        "is_suspended": False,
        "available_at": day,
    }


def test_nav_reconciles_overnight_rebalance_adjusted_prices_and_split_requests():
    days = ["2024-01-31", "2024-02-01", "2024-02-02"]
    a, b, c = "000001.SZ", "000002.SZ", "000003.SZ"
    members = pd.concat([_daily_members(days[:1]), _daily_members(days[1:], (a, c))])
    bars = pd.DataFrame([
        _bar(days[0], a, 10, 10), _bar(days[0], b, 10, 10),
        # B is sold at 12; C is bought at 10. A's raw opening price of 5
        # after a 2-for-1 split is represented by adjusted opening price 10.
        _bar(days[1], b, 12, 99), _bar(days[1], a, 10, 10), _bar(days[1], c, 10, 12),
        _bar(days[2], a, 999, 999), _bar(days[2], c, 12, 14),
    ])
    status = pd.DataFrame([{"trade_date": days[2], "ts_code": a, "is_suspended": True}])
    result = monthly_equal_weight_benchmark(members, bars, status, expected_members=2)
    np.testing.assert_allclose(result.nav["nav"], [1, 1.21, 1.32])
    np.testing.assert_allclose(result.nav["overnight_pnl"], [0, .10, 0])
    np.testing.assert_allclose(result.nav["intraday_pnl"], [0, .11, .11])
    np.testing.assert_allclose(
        result.nav.previous_nav + result.nav.overnight_pnl + result.nav.intraday_pnl,
        result.nav.nav,
    )
    prefix = monthly_equal_weight_benchmark(
        members.loc[members.trade_date <= days[1]], bars, status, expected_members=2,
    )
    pd.testing.assert_frame_equal(prefix.nav, result.nav.iloc[:2])
    second = monthly_equal_weight_benchmark(members, bars, status, expected_members=2)
    pd.testing.assert_frame_equal(pd.concat([prefix.nav, second.nav.iloc[2:]]), result.nav)
    # A missing exit open invalidates NAV even when all incoming members have quotes.
    missing = monthly_equal_weight_benchmark(members, bars.drop(index=2), status, expected_members=2)
    assert len(missing.nav) == 1


def test_suspended_dividend_offsets_mark_once_and_survives_resume_without_rebalance():
    days = ["2024-07-18", "2024-07-25", "2024-07-26", "2024-07-29"]
    code = "600027.SH"
    membership = _daily_members(days, (code,))
    bars = pd.DataFrame([_bar(days[0], code, 6.08, 6.08), _bar(days[3], code, 6.2, 5.54)])
    status = pd.DataFrame({"trade_date": days[1:3], "ts_code": code, "is_suspended": True})
    dividend = pd.DataFrame({
        "trade_date": [days[1]], "ts_code": code, "adjusted_cash_per_share": 0.15,
    })
    result = monthly_equal_weight_benchmark(
        membership, bars, status, expected_members=1, suspended_dividends=dividend,
    )
    assert result.audit.ready.all()
    nav = result.nav
    np.testing.assert_allclose(nav.nav, [1, 1, 1, (5.54 + 0.15) / 6.08])
    np.testing.assert_allclose(nav.dividend_cash, [0, 0.15 / 6.08, 0, 0])
    assert nav.cash.iloc[-1] == pytest.approx(0.15 / 6.08)
    assert nav.overnight_pnl.iloc[-1] == pytest.approx((6.2 - 5.93) / 6.08)


def test_equal_weight_rebalances_next_month_then_drifts_with_returns() -> None:
    days = ["2024-01-31", "2024-02-01", "2024-02-02"]
    membership = _daily_members(days)
    bars = pd.DataFrame([
        _bar("2024-01-31", "000001.SZ", 10, 10),
        _bar("2024-01-31", "000002.SZ", 20, 20),
        _bar("2024-02-01", "000001.SZ", 10, 11),
        _bar("2024-02-01", "000002.SZ", 20, 20),
        _bar("2024-02-02", "000001.SZ", 11, 12),
        _bar("2024-02-02", "000002.SZ", 20, 18),
    ])

    result = monthly_equal_weight_benchmark(membership, bars, expected_members=2)

    feb_1 = result.weights.loc[result.weights["trade_date"].eq(pd.Timestamp("2024-02-01").date())]
    feb_2 = result.weights.loc[result.weights["trade_date"].eq(pd.Timestamp("2024-02-02").date())]
    assert feb_1.set_index("ts_code").loc["000001.SZ", "weight"] == 11 / 21
    assert feb_2.set_index("ts_code").loc["000001.SZ", "weight"] == 4 / 7
    assert result.audit["rebalance_type"].tolist() == ["bootstrap", "monthly", "drift"]
    assert set(result.weights["source"]) == {EQUAL_WEIGHT_SOURCE}
    assert set(result.weights["methodology"]) == {EQUAL_WEIGHT_METHODOLOGY}


def test_equal_weight_carries_a_known_suspension_but_blocks_an_unknown_missing_price() -> None:
    days = ["2024-01-31", "2024-02-01", "2024-02-02"]
    membership = _daily_members(days)
    bars = pd.DataFrame([
        _bar("2024-01-31", "000001.SZ", 10, 10),
        _bar("2024-01-31", "000002.SZ", 20, 20),
        _bar("2024-02-01", "000002.SZ", 20, 22),
        _bar("2024-02-02", "000001.SZ", 10, 11),
    ])
    suspensions = pd.DataFrame([
        {"trade_date": "2024-02-01", "ts_code": "000001.SZ", "is_suspended": True}
    ])

    result = monthly_equal_weight_benchmark(
        membership, bars, suspensions, expected_members=2
    )

    assert result.audit.loc[1, "ready"]
    assert result.audit.loc[1, "suspended_count"] == 1
    assert not result.audit.loc[2, "ready"]
    assert result.audit.loc[2, "reason"] == "missing_unexplained_price"


def test_equal_weight_rebalances_when_membership_changes() -> None:
    membership = pd.concat([
        _daily_members(["2024-02-01"]),
        _daily_members(["2024-02-02"], ("000001.SZ", "000003.SZ")),
    ], ignore_index=True)
    bars = pd.DataFrame([
        _bar("2024-02-01", "000001.SZ", 10, 10),
        _bar("2024-02-01", "000002.SZ", 20, 20),
        _bar("2024-02-02", "000001.SZ", 10, 10),
        _bar("2024-02-02", "000002.SZ", 20, 20),
        _bar("2024-02-02", "000003.SZ", 30, 30),
    ])

    result = monthly_equal_weight_benchmark(membership, bars, expected_members=2)

    changed = result.weights.loc[result.weights["trade_date"].eq(pd.Timestamp("2024-02-02").date())]
    assert result.audit.loc[1, "rebalance_type"] == "membership"
    assert changed.set_index("ts_code")["weight"].to_dict() == {
        "000001.SZ": 0.5,
        "000003.SZ": 0.5,
    }


def test_suspension_retains_drifted_holding_and_retries_after_resumption() -> None:
    days = ["2024-01-31", "2024-02-01", "2024-02-02"]
    bars = pd.DataFrame([
        _bar(days[0], "000001.SZ", 10, 20),
        _bar(days[0], "000002.SZ", 10, 10),
        _bar(days[1], "000002.SZ", 10, 20),
        _bar(days[2], "000001.SZ", 20, 20),
        _bar(days[2], "000002.SZ", 20, 20),
    ])
    status = pd.DataFrame([{
        "trade_date": days[1], "ts_code": "000001.SZ", "is_suspended": True,
    }])
    result = monthly_equal_weight_benchmark(_daily_members(days), bars, status, expected_members=2)
    weights = result.weights.pivot(index="trade_date", columns="ts_code", values="weight")
    # Frozen holding has value 20; the other grows from 10 to 20, without trading.
    assert weights.loc[pd.Timestamp(days[1]).date(), "000001.SZ"] == pytest.approx(0.5)
    assert result.rebalances["executed"].tolist() == [True, False, True]
    assert result.rebalances.loc[1, "signal_date"] == pd.Timestamp(days[0]).date()
    assert result.rebalances.loc[1, "target_weights"] == {"000001.SZ": 0.5, "000002.SZ": 0.5}
    assert result.rebalances.loc[2, "rebalance_type"] == "retry"


def test_equal_weight_service_rejects_official_output_before_database_access() -> None:
    from myquant.processing.jobs import DataProcessingService

    class NoDatabase:
        def execute(self, *args):
            pytest.fail("Must validate output code before accessing the database")

    for code in ("000300.SH", "OTHER", "mq300.ew"):
        with pytest.raises(ValueError, match="MQ300.EW"):
            DataProcessingService(NoDatabase()).build_monthly_equal_weight_benchmark(
                "000300.SH", code, "2024-01-01", "2024-02-01"
            )


def test_suspended_removed_member_is_held_until_it_can_be_sold() -> None:
    days = ["2024-02-01", "2024-02-02", "2024-02-05"]
    membership = pd.concat([
        _daily_members([days[0]]),
        _daily_members(days[1:], ("000001.SZ", "000003.SZ")),
    ], ignore_index=True)
    bars = pd.DataFrame([
        _bar(day, code, 10, 10)
        for day in days for code in ("000001.SZ", "000002.SZ", "000003.SZ")
    ])
    status = pd.DataFrame([{
        "trade_date": days[1], "ts_code": "000002.SZ", "is_suspended": True,
    }])
    result = monthly_equal_weight_benchmark(membership, bars, status, expected_members=2)
    deferred = result.weights.loc[result.weights["trade_date"].eq(pd.Timestamp(days[1]).date())]
    resumed = result.weights.loc[result.weights["trade_date"].eq(pd.Timestamp(days[2]).date())]
    assert set(deferred["ts_code"]) == {"000001.SZ", "000002.SZ"}
    assert set(resumed["ts_code"]) == {"000001.SZ", "000003.SZ"}


def test_rebalance_targets_do_not_depend_on_execution_close() -> None:
    days = ["2024-01-31", "2024-02-01"]
    bars = pd.DataFrame([_bar(day, code, 10, 10) for day in days
                         for code in ("000001.SZ", "000002.SZ")])
    before = monthly_equal_weight_benchmark(_daily_members(days), bars, expected_members=2)
    bars.loc[2, "adj_close"] = 100
    after = monthly_equal_weight_benchmark(_daily_members(days), bars, expected_members=2)
    pd.testing.assert_frame_equal(before.rebalances, after.rebalances)
    assert not before.weights.equals(after.weights)


def test_same_day_membership_information_cannot_authorize_open_rebalance() -> None:
    days = ["2024-02-01", "2024-02-02"]
    membership = pd.concat([
        _daily_members([days[0]]),
        _daily_members([days[1]], ("000001.SZ", "000003.SZ")),
    ], ignore_index=True)
    membership["available_at"] = membership["trade_date"]
    bars = pd.DataFrame([_bar(day, code, 10, 10) for day in days
                         for code in ("000001.SZ", "000002.SZ", "000003.SZ")])
    result = monthly_equal_weight_benchmark(membership, bars, expected_members=2)
    assert result.audit.loc[1, "reason"] == "membership_not_known_at_signal"
    assert not result.rebalances.loc[1, "executed"]


def test_service_persists_targets_and_weights_in_one_commit(monkeypatch) -> None:
    from myquant.models import (
        BenchmarkRebalance, CleanDailyBar, IndexConstituent, IndexMembershipSnapshot,
    )
    from myquant.processing import jobs

    days = ["2024-01-31", "2024-02-01"]
    codes = tuple(f"{i:06d}.SZ" for i in range(300))
    members = _daily_members(days, codes)
    members["index_code"] = "000300.SH"
    bars = pd.DataFrame([_bar(day, code, 10, 10) for day in days for code in codes])
    frames = {IndexMembershipSnapshot: members, CleanDailyBar: bars}
    monkeypatch.setattr(jobs, "_frame", lambda session, model, *args: frames.get(model, pd.DataFrame()))

    class Session:
        commits = 0

        def execute(self, statement):
            assert self.commits == 0

        def commit(self):
            self.commits += 1

    session = Session()
    service = jobs.DataProcessingService(session)
    writes = {}
    monkeypatch.setattr(service, "_upsert", lambda model, rows, keys, **kwargs: writes.update({model: rows}))
    service.build_monthly_equal_weight_benchmark("000300.SH", "MQ300.EW", days[1], days[1])
    assert session.commits == 1
    assert len(writes[IndexConstituent]) == 300
    record = writes[BenchmarkRebalance][0]
    assert record["signal_date"] == pd.Timestamp(days[0]).date()
    assert record["execution_date"] == pd.Timestamp(days[1]).date()
    assert record["executed"]
    assert len(record["target_weights"]) == 300
