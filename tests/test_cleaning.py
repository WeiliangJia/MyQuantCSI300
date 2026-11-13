import pandas as pd

from myquant.processing.cleaning import (
    build_financial_snapshots,
    clean_daily_bars,
    rebuild_index_membership,
    replay_index_membership_events,
    reverse_index_membership,
)


def test_cleaner_prefers_baostock_and_logs_cross_source_difference() -> None:
    bars = pd.DataFrame([
        {"source": "akshare", "ts_code": "000001.SZ", "trade_date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10.2, "volume": 1000, "amount": 10000},
        {"source": "baostock", "ts_code": "000001.SZ", "trade_date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10.0, "volume": 1000, "amount": 10000},
    ])
    result = clean_daily_bars(bars)
    assert result.bars.iloc[0]["selected_source"] == "baostock"
    assert "source_close_disagreement" in result.issues["category"].tolist()


def test_cleaner_rejects_invalid_ohlc_and_marks_zero_volume() -> None:
    bars = pd.DataFrame([
        {"source": "baostock", "ts_code": "000001.SZ", "trade_date": "2024-01-02", "open": 10, "high": 9, "low": 8, "close": 10, "volume": 1000, "amount": 10000},
        {"source": "baostock", "ts_code": "000002.SZ", "trade_date": "2024-01-02", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 0, "amount": 0},
    ])
    result = clean_daily_bars(bars)
    assert result.bars["ts_code"].tolist() == ["000002.SZ"]
    assert bool(result.bars.iloc[0]["is_zero_volume"])
    assert result.bars.iloc[0]["flags"]["untradable"] is True
    assert "untradable_zero_volume" in result.issues["category"].tolist()
    assert "invalid_ohlc" in result.issues["category"].tolist()


def test_cleaner_treats_all_null_bar_as_untradable_placeholder() -> None:
    bars = pd.DataFrame([{
        "source": "baostock", "ts_code": "000022.SZ", "trade_date": "2018-12-26",
        "open": None, "high": None, "low": None, "close": None, "volume": None, "amount": None,
    }])
    result = clean_daily_bars(bars)
    assert result.bars.empty
    assert set(result.issues["category"]) == {"non_trading_placeholder", "untradable_missing_quote"}
    assert set(result.issues["severity"]) == {"INFO"}


def test_financial_snapshots_respect_available_at() -> None:
    observations = pd.DataFrame([
        {"ts_code": "000001.SZ", "metric": "roe", "value": 0.1, "announcement_date": "2024-04-30", "available_at": "2024-04-30"},
        {"ts_code": "000001.SZ", "metric": "roe", "value": 0.2, "announcement_date": "2024-08-30", "available_at": "2024-08-30"},
    ])
    snapshots = build_financial_snapshots(observations, pd.Series(["2024-05-01", "2024-09-01"]))
    assert snapshots.loc[snapshots["as_of_date"] == pd.Timestamp("2024-05-01").date(), "payload"].iloc[0]["roe"] == 0.1
    assert snapshots.loc[snapshots["as_of_date"] == pd.Timestamp("2024-09-01").date(), "payload"].iloc[0]["roe"] == 0.2


def test_membership_rebuild_applies_official_event_on_effective_date() -> None:
    baseline = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ"]})
    events = pd.DataFrame([{
        "index_code": "000300.SH", "ts_code": "000002.SZ", "action": "ADD",
        "effective_date": "2024-01-03", "announcement_date": "2023-12-29",
    }])
    snapshots = rebuild_index_membership(pd.Series(["2024-01-02", "2024-01-03"]), baseline, events)
    first_day = snapshots.loc[snapshots["trade_date"] == pd.Timestamp("2024-01-02").date(), "ts_code"].tolist()
    second_day = snapshots.loc[snapshots["trade_date"] == pd.Timestamp("2024-01-03").date(), "ts_code"].tolist()
    assert "000002.SZ" not in first_day
    assert "000002.SZ" in second_day


def test_membership_rebuild_applies_holiday_event_on_next_trade_date() -> None:
    baseline = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ"]})
    events = pd.DataFrame([{
        "index_code": "000300.SH", "ts_code": "000002.SZ", "action": "ADD",
        "effective_date": "2024-01-01", "announcement_date": "2023-12-29",
    }])
    snapshots = rebuild_index_membership(pd.Series(["2024-01-02", "2024-01-03"]), baseline, events)
    first_day = snapshots.loc[
        snapshots["trade_date"] == pd.Timestamp("2024-01-02").date(), "ts_code"
    ].tolist()
    assert "000002.SZ" in first_day


def test_reverse_membership_and_forward_replay_close_exactly() -> None:
    baseline = pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ", "600000.SH"]})
    current = pd.DataFrame({"ts_code": ["000002.SZ", "000003.SZ", "600000.SH"]})
    events = pd.DataFrame([
        {
            "index_code": "000300.SH", "ts_code": "000001.SZ", "action": "REMOVE",
            "effective_date": "2024-01-03", "announcement_date": "2023-12-29",
        },
        {
            "index_code": "000300.SH", "ts_code": "000003.SZ", "action": "ADD",
            "effective_date": "2024-01-03", "announcement_date": "2023-12-29",
        },
    ])
    log: list[str] = []
    rebuilt = reverse_index_membership(
        current, events, "2024-01-02", "2024-01-04", expected_members=3, progress=log.append,
    )
    assert set(rebuilt["ts_code"]) == set(baseline["ts_code"])
    assert log == [
        "[1/1] reverse effective_date=2024-01-03 undo_add=1 restore_remove=1 members=3"
    ]
    replayed = replay_index_membership_events(
        rebuilt, events, "2024-01-02", "2024-01-04", expected_members=3,
    )
    assert set(replayed["ts_code"]) == set(current["ts_code"])


def test_reverse_membership_fails_closed_on_missing_event_state() -> None:
    current = pd.DataFrame({"ts_code": ["000001.SZ", "000002.SZ", "600000.SH"]})
    events = pd.DataFrame([
        {
            "index_code": "000300.SH", "ts_code": "000001.SZ", "action": "REMOVE",
            "effective_date": "2024-01-03", "announcement_date": "2023-12-29",
        },
        {
            "index_code": "000300.SH", "ts_code": "000003.SZ", "action": "ADD",
            "effective_date": "2024-01-03", "announcement_date": "2023-12-29",
        },
    ])
    try:
        reverse_index_membership(current, events, "2024-01-02", "2024-01-04", expected_members=3)
    except ValueError as exc:
        assert "Cannot reverse event" in str(exc)
    else:
        raise AssertionError("Expected inconsistent current membership to fail closed")
