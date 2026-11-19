"""Compare V2 and the verified V1 account on identical archived daily packets."""

import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from rehearse_mq300_recent import check_day
from myquant.simulation.paper_day import TURNOVER_CAP, run_day, signal_targets


def diagnose(packet, record, previous_top):
    f = packet["signal_inputs"]
    quotes = pd.DataFrame(packet["quotes"]).set_index("ts_code")
    scores = pd.Series(f["scores"], dtype=float).rename_axis("ts_code").reset_index(name="score")
    risk = f["risk_returns"]
    _, audit = signal_targets(
        scores,
        pd.Series(f["weights"]),
        pd.DataFrame(risk["values"], index=risk["dates"], columns=risk["codes"], dtype=float),
        pd.Series(f["industry"]),
        quotes,
        set(packet["members"]),
        record,
        explain=True,
    )
    original = record["signal"]
    if audit["decision"] != original["decision"]:
        raise ValueError("V1 diagnosis did not reproduce the saved decision")
    attempts = audit.get("attempts", [])
    if [a["turnover"] for a in attempts] != [a["turnover"] for a in original.get("attempts", [])]:
        raise ValueError("V1 candidate turnover differs from the stored signal")
    eligible = (
        set(f["weights"])
        & set(packet["members"])
        & set(quotes.index[~quotes.suspended & quotes.isST.eq(0)])
    )
    top = set(
        scores.loc[scores.ts_code.isin(eligible)]
        .sort_values(["score", "ts_code"], ascending=[False, True])
        .head(50)
        .ts_code
    )
    acct = record["account"]
    return top, [
        {
            "signal_date": packet["trade_date"],
            "decision": audit["decision"],
            "cash_weight": acct["cash"] / acct["nav_cny"],
            "top50_entries": len(top - previous_top),
            **attempt,
        }
        for attempt in attempts
    ]


def daily_metrics(record):
    a = record["account"]
    orders = record["orders"]
    prior = a["previous_nav_cny"]
    discretionary = [o for o in orders if not o["forced_exit"]]
    amounts = [
        sum(o["notional"] for o in discretionary if o["side"] == side) for side in ("BUY", "SELL")
    ]
    return {
        "trade_date": record["trade_date"],
        "nav": a["strategy_nav"],
        "benchmark": a["benchmark_nav"],
        "cost": a["total_cost"],
        "cash": a["cash"],
        "orders": len(orders),
        "one_way": max(a["buy_notional"], a["sell_notional"]) / prior,
        "two_way": (a["buy_notional"] + a["sell_notional"]) / prior,
        "discretionary_one_way": max(amounts) / prior,
        "forced_notional": sum(o["notional"] for o in orders if o["forced_exit"]),
    }


def summarize(daily, signals):
    strategy = daily.nav / daily.nav.shift(fill_value=1) - 1
    benchmark = daily.benchmark / daily.benchmark.shift(fill_value=1) - 1
    active = strategy - benchmark
    entry = daily.loc[daily.two_way.gt(0), "trade_date"].iloc[0]
    after = daily.loc[daily.trade_date.gt(entry)]
    return {
        "net_return": daily.nav.iloc[-1] - 1,
        "benchmark_return": daily.benchmark.iloc[-1] - 1,
        "net_excess": daily.nav.iloc[-1] - daily.benchmark.iloc[-1],
        "tracking_error": float(active.std(ddof=1) * np.sqrt(252)),
        "one_way_after_entry": after.one_way.sum(),
        "two_way_after_entry": after.two_way.sum(),
        "two_way_including_entry": daily.two_way.sum(),
        "max_discretionary_one_way_after_entry": after.discretionary_one_way.max(),
        "stepped_signals": sum(
            s.get("step_fraction", 1.0) < 1 for s in signals if s["decision"] == "approved"
        ),
        "cost_cny": round(daily.cost.sum(), 2),
        "approved": sum(s["decision"] == "approved" for s in signals),
        "signal_count": len(signals),
        "orders": int(daily.orders.sum()),
    }


def compare(source, output):
    reference = source / "verified.sqlite3"
    output.mkdir(parents=True, exist_ok=True)
    diagnosis, risk_rows = [], []
    daily = {"v1": [], "v2": []}
    signals = {"v1": [], "v2": []}
    prior_v2 = None
    pool, top = set(), set()
    with (
        closing(sqlite3.connect(reference.resolve().as_uri() + "?mode=ro", uri=True)) as baseline,
        TemporaryDirectory(dir=output) as tmp,
    ):
        database = Path(tmp) / "v2.sqlite3"
        for date, payload in baseline.execute(
            "SELECT trade_date,payload FROM session ORDER BY trade_date"
        ):
            path = source / "inputs" / f"{date}.json"
            packet = json.loads(path.read_bytes())
            v1 = json.loads(payload)
            digest = sha256(
                json.dumps(packet, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
            if date != packet["trade_date"] or v1["input_sha256"] != digest:
                raise ValueError(f"{date}: input differs from the V1 baseline")
            v2, created = run_day(database, path, version="v2")
            if not created or v2["account"]["benchmark_nav"] != v1["account"]["benchmark_nav"]:
                raise ValueError(f"{date}: comparison accounts are not aligned")
            check_day(packet, v2, prior_v2, pool)
            if prior_v2 is not None:
                for version, record in (("v1", v1), ("v2", v2)):
                    daily[version].append(daily_metrics(record))
            if v1["signal"]:
                top, rows = diagnose(packet, v1, top)
                diagnosis.extend(rows)
                for version, record in (("v1", v1), ("v2", v2)):
                    signals[version].append(record["signal"])
                s = v2["signal"]
                risk_rows.extend(
                    {"signal_date": date, "decision": s["decision"], **a}
                    for a in s.get("attempts", [])
                )
            prior_v2, pool = v2, set(packet["members"])
        frames = {k: pd.DataFrame(v) for k, v in daily.items()}
        if len(frames["v1"]) != 120:
            raise ValueError("Expected the 120-session comparison window")
        stats = {k: summarize(frame, signals[k]) for k, frame in frames.items()}
        if stats["v2"]["max_discretionary_one_way_after_entry"] > TURNOVER_CAP + 1e-10:
            raise ValueError("V2 executed discretionary turnover exceeded 20%")
        report = {
            "start": daily["v1"][0]["trade_date"],
            "end": daily["v1"][-1]["trade_date"],
            "sessions": 120,
            **stats,
        }
        pd.concat([frame.assign(version=k) for k, frame in frames.items()]).to_csv(
            Path(tmp) / "daily.csv", index=False
        )
        pd.DataFrame(diagnosis).to_csv(Path(tmp) / "v1_turnover_diagnosis.csv", index=False)
        pd.DataFrame(risk_rows).to_csv(Path(tmp) / "v2_decisions.csv", index=False)
        (Path(tmp) / "comparison.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        for file in Path(tmp).iterdir():
            file.replace(output / file.name)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path("data/exports/mq300-v1-rehearsal-2026-11-18")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/exports/mq300-v2-comparison-2026-11-18")
    )
    args = parser.parse_args()
    compare(args.source, args.output)
