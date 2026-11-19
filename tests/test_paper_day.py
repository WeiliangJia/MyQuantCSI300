from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from myquant.simulation.paper_day import run_day


def write_packet(directory, day, previous, following):
    members = [f"{i:06d}.SZ" for i in range(1, 301)]
    packet = {
        "trade_date": day, "previous_trade_date": previous, "next_trade_date": following,
        "benchmark_open": 1.0, "benchmark_close": 1.01, "members": members,
        "quotes": [{"ts_code": code, "open": 10, "close": 10, "suspended": False, "isST": 0,
                    "adv_volume": 100_000, "up_limit": 11, "down_limit": 9} for code in members],
        "actions": [], "signal_inputs": None,
    }
    path = directory / f"{day}.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path


def test_concurrent_day_is_committed_once_and_revisions_are_rejected(tmp_path):
    db = tmp_path / "daily.db"
    path = write_packet(tmp_path, "2024-03-05", None, "2024-03-06")
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(lambda _: run_day(db, path), range(2))
    assert a[0] == b[0]
    assert a[1] != b[1]
    with pytest.raises(ValueError, match="settings changed"):
        run_day(db, path, version="v2")
    revised = json.loads(path.read_text())
    revised["benchmark_close"] = 1.02
    path.write_text(json.dumps(revised))
    with pytest.raises(ValueError, match="input differs"):
        run_day(db, path)


def test_missing_day_and_failed_booking_can_be_retried(tmp_path):
    db = tmp_path / "daily.db"
    seed = write_packet(tmp_path, "2024-03-05", None, "2024-03-06")
    today = write_packet(tmp_path, "2024-03-06", "2024-03-05", "2024-03-07")
    later = write_packet(tmp_path, "2024-03-07", "2024-03-05", "2024-03-08")
    with pytest.raises(ValueError, match="previous trading day"):
        run_day(db, today, monthly_cost=15)
    run_day(db, seed)
    with pytest.raises(ValueError, match="expected 2024-03-06"):
        run_day(db, later)
    data = today.read_text()
    today.write_text(data.replace('"suspended": false', '"suspended": null'))
    with pytest.raises(ValueError, match="suspension status"):
        run_day(db, today)
    today.write_text(data)
    assert run_day(db, today)[1]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 2
