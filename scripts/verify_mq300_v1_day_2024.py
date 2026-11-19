"""Feed the 2024 archive to the daily entry point and compare against the existing V1 journal."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pandas as pd

from backfill_mq300_paper_inputs import ROOT
from replay_mq300_v1_2024 import FIRST_SIGNAL, OUTPUT, inputs
from myquant.simulation.paper import COST_FIELDS
from myquant.simulation.paper_day import run_day


def packets():
    benchmark, weights, market, members, schedule, scores, returns, industry, actions = inputs(
        ROOT / "2024" / "industries.csv", ROOT / "2024" / "payments.csv",
    )
    quotes = market.groupby("trade_date")
    member_days = members.groupby("trade_date")
    weight_days = weights.groupby("trade_date")
    signal_days = set(schedule.signal_date)
    fields = ["ts_code", "open", "close", "suspended", "isST", "down_limit", "up_limit", "adv_volume"]
    calendar = list(benchmark.loc[benchmark.trade_date.ge(FIRST_SIGNAL)].itertuples())
    for index, row in enumerate(calendar):
        date = row.trade_date
        day = date.date().isoformat()
        daily_quotes = quotes.get_group(date)[fields].astype(object)
        distributions = actions.loc[actions.trade_date.eq(date)].drop(columns="trade_date").assign(ex_date=day)
        features = None
        if date in signal_days:
            history = returns.loc[:date].tail(60)
            names = industry.loc[industry.available_at.le(day)].sort_values("available_at").drop_duplicates(
                "ts_code", keep="last",
            ).set_index("ts_code").industry
            reference = weight_days.get_group(date.date()).set_index("ts_code").weight
            features = {
                "scores": scores.loc[scores.signal_date.eq(date)].set_index("ts_code").score.to_dict(),
                "weights": reference.to_dict(), "industry": names.to_dict(),
                "risk_returns": {"dates": history.index.strftime("%Y-%m-%d").tolist(),
                                 "codes": history.columns.tolist(),
                                 "values": history.astype(object).where(history.notna(), None).values.tolist()},
            }
        yield {
            "trade_date": day, "previous_trade_date": calendar[index - 1].trade_date.date().isoformat() if index else None,
            "next_trade_date": calendar[index + 1].trade_date.date().isoformat() if index + 1 < len(calendar) else "2025-01-02",
            "benchmark_open": row.benchmark_open, "benchmark_close": row.benchmark_nav,
            "members": member_days.get_group(date.date()).ts_code.tolist(),
            "quotes": daily_quotes.where(pd.notna(daily_quotes), None).to_dict("records"),
            "actions": distributions.to_dict("records"), "signal_inputs": features,
        }


def verify(reference, output):
    if reference.resolve() == (output / "verified.sqlite3").resolve():
        raise ValueError("The reference account must be separate from the verification output")
    output.mkdir(parents=True, exist_ok=True)
    snapshots = output / "inputs"
    snapshots.mkdir(exist_ok=True)
    orders = fills = days = signals = 0
    with closing(sqlite3.connect(reference.resolve().as_uri() + "?mode=ro", uri=True)) as baseline:
        with TemporaryDirectory(dir=output) as tmp:
            database = Path(tmp) / "daily.sqlite3"
            for packet in packets():
                day = packet["trade_date"]
                path = snapshots / f"{day}.json"
                path.write_text(json.dumps(packet, allow_nan=False, separators=(",", ":")), encoding="utf-8")
                record, created = run_day(database, path)
                duplicate, repeated = run_day(database, path)
                if not created or repeated or duplicate != record:
                    raise ValueError(f"{day}: duplicate input did not return the original session")
                saved = baseline.execute("SELECT payload FROM session WHERE trade_date=?", (day,)).fetchone()
                if not saved:
                    raise ValueError(f"{day}: missing reference session")
                expected = json.loads(saved[0])
                for field in ("orders", "positions", "actions", "receivables", "account"):
                    actual = record[field]
                    target = expected[field]
                    if field == "account" and day == FIRST_SIGNAL:
                        # The new account learns its benchmark opening base on the next session.
                        if actual["benchmark_nav"] != 1.0:
                            raise ValueError("Expected benchmark seed value of one")
                        actual = {key: value for key, value in actual.items() if key != "benchmark_nav"}
                        target = {key: value for key, value in target.items() if key != "benchmark_nav"}
                    if actual != target:
                        raise ValueError(f"{day}: {field} differs from the reference")
                if (record["signal"] or {}).get("weights") != (expected["signal"] or {}).get("weights"):
                    raise ValueError(f"{day}: frozen target weights differ from the reference")
                days += 1
                orders += len(record["orders"])
                fills += sum(order["filled_quantity"] > 0 for order in record["orders"])
                signals += int(record["signal"] is not None)
            with closing(sqlite3.connect(database)) as db:
                count = db.execute("SELECT COUNT(*) FROM session").fetchone()[0]
                if count != days or (days, signals, orders, fills) != (200, 43, 5429, 5346):
                    raise ValueError("Unexpected V1 session, signal, order or fill counts")
                last = record["account"]
                cost = 0.0
                booked_orders = booked_fills = 0
                for item, in db.execute("SELECT payload FROM session ORDER BY trade_date"):
                    saved = json.loads(item)
                    cost += saved["account"]["total_cost"]
                    booked_orders += len(saved["orders"])
                    booked_fills += sum(order["filled_quantity"] > 0 for order in saved["orders"])
                if (booked_orders, booked_fills) != (orders, fills):
                    raise ValueError("Repeated submissions changed the order or fill count")
            database.replace(output / "verified.sqlite3")
    report = {"sessions_including_seed": days, "duplicate_submissions": days, "additional_sessions": count - days,
              "additional_orders": booked_orders - orders, "additional_fills": booked_fills - fills,
              "signals": signals, "orders": orders, "orders_with_fills": fills,
              "total_cost_cny": round(cost, 2), "cost_fields_compared": COST_FIELDS, "ending_nav_cny": last["nav_cny"]}
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, default=OUTPUT / "account.sqlite3")
    parser.add_argument("--output", type=Path, default=Path("data/exports/mq300-v1-day-acceptance"))
    args = parser.parse_args()
    verify(args.reference, args.output)
