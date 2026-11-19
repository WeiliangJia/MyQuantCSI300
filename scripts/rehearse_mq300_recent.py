"""Build and exercise daily V1 packets from an audited window and supplemental BaoStock bars."""

import argparse
import json
from pathlib import Path
from collections import Counter
from decimal import Decimal
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
import requests

from backfill_mq300_paper_inputs import BAR_FIELDS, KEY, ROOT, cached, provider
from myquant.data.free_adapters import normalize_baostock_daily, to_baostock_code, to_ts_code
from myquant.simulation.paper_day import run_day
from myquant.research.benchmark_weights import monthly_equal_weight_benchmark
from myquant.simulation.paper import COST_FIELDS, fen


def supplement(archive, output, end, bs):
    coverage = json.loads((archive / "coverage.json").read_text())
    raw = output / "source"
    start = (pd.Timestamp(coverage["start"]) - pd.Timedelta(days=115)).date().isoformat()
    cal = cached(
        raw / "calendar.csv",
        bs,
        "query_trade_dates",
        start,
        (pd.Timestamp(end) + pd.Timedelta(days=8)).date().isoformat(),
    )
    days = cal.loc[cal.is_trading_day.eq("1"), "calendar_date"].sort_values().tolist()
    members = pd.read_csv(archive / "membership_vendor.csv")
    for day in days:
        if coverage["end"] < day <= end:
            rows = cached(raw / f"members-{day}.csv", bs, "query_hs300_stocks", date=day)
            if len(rows) != 300 or rows.code.nunique() != 300 or rows.updateDate.gt(day).any():
                raise ValueError(f"Invalid membership snapshot {day}")
            members = pd.concat(
                [members, rows.assign(trade_date=day, ts_code=rows.code.map(to_ts_code))],
                ignore_index=True,
            )
    frames = []
    for i, code in enumerate(sorted(set(members.ts_code)), 1):
        bars = cached(
            raw / "bars" / f"{code}.csv",
            bs,
            "query_history_k_data_plus",
            to_baostock_code(code),
            BAR_FIELDS,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="3",
        )
        frame = normalize_baostock_daily(bars)
        if frame.empty or frame.trade_date.duplicated().any() or not frame.ts_code.eq(code).all():
            raise ValueError(f"Invalid quotes for {code}")
        frames.append(frame)
        if i % 80 == 0:
            print(f"Read {i} symbol histories", flush=True)
    return days, members, pd.concat(frames, ignore_index=True)


def company_actions(archive, output, end, symbols, days, *, online=False):
    east = pd.read_csv(archive / "dividend_calendar_scoped.csv")
    archived_end = json.loads((archive / "coverage.json").read_text())["end"]
    for day in days:
        if archived_end < day <= end:
            path = output / "source" / f"dividends-{day}.json"
            if not path.is_file() and online:
                from audit_mq300_actions_2024 import URL

                query = {
                    "reportName": "RPT_SHAREBONUS_DET",
                    "columns": "SECUCODE,EX_DIVIDEND_DATE,REPORT_DATE,PRETAX_BONUS_RMB,BONUS_IT_RATIO,EQUITY_RECORD_DATE",
                    "pageSize": 500,
                    "pageNumber": 1,
                    "source": "WEB",
                    "client": "WEB",
                    "filter": f"(EX_DIVIDEND_DATE='{day}')",
                }
                reply = requests.get(URL, params=query, timeout=25)
                reply.raise_for_status()
                response = reply.json()
            else:
                response = json.loads(path.read_text(encoding="utf-8"))
            result = response["result"]
            if not response["success"] or result["count"] != len(result["data"]):
                raise ValueError(f"Incomplete ex-date calendar on {day}")
            if not path.exists():
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            extra = pd.DataFrame(result["data"]).rename(
                columns={"SECUCODE": "ts_code", "EX_DIVIDEND_DATE": "trade_date"}
            )
            if not extra.empty:
                extra["trade_date"] = extra.trade_date.str[:10]
                if not extra.trade_date.eq(day).all():
                    raise ValueError(f"Unexpected event dates for {day}")
                east = pd.concat([east, extra.loc[extra.ts_code.isin(symbols)]], ignore_index=True)
    east = east.drop_duplicates(KEY + ["REPORT_DATE"])
    east = east.assign(
        cash=east.PRETAX_BONUS_RMB.fillna(0) / 10,
        shares=east.BONUS_IT_RATIO.fillna(0) / 10,
        record_date=east.EQUITY_RECORD_DATE.str[:10],
    )
    terms = east.groupby(KEY).agg(
        cash=("cash", "sum"), shares=("shares", "sum"), record_date=("record_date", "first")
    )
    terms["pay_date"] = terms.index.get_level_values("trade_date")
    terms["payment_evidence"] = "assumed_ex_date"
    bao = pd.read_csv(archive / "dividend_calendar_baostock.csv")
    bao = bao.assign(
        cash=bao.dividCashPsBeforeTax.fillna(0),
        shares=bao.dividStocksPs.fillna(0) + bao.dividReserveToStockPs.fillna(0),
    )
    for key, rows in bao.groupby(KEY):
        if key not in terms.index:
            unique = rows.drop_duplicates(["cash", "shares", "dividRegistDate", "dividPayDate"])
            terms.loc[key, :] = [
                unique.cash.sum(),
                unique.shares.sum(),
                unique.dividRegistDate.iloc[0],
                key[0],
                "assumed_ex_date",
            ]
        match = rows.loc[
            np.isclose(rows.cash, terms.at[key, "cash"], atol=1e-6, rtol=0)
            & rows.dividRegistDate.eq(terms.at[key, "record_date"])
        ]
        pay = match.dividPayDate.dropna().unique()
        if len(pay) == 1:
            terms.loc[key, ["pay_date", "payment_evidence"]] = [pay[0], "baostock_matched_terms"]
    for row in pd.read_csv(archive / "issuer_cash_evidence.csv").itertuples():
        key = (row.trade_date, row.ts_code)
        terms.loc[key, ["cash", "shares"]] = [row.cash_per_share, row.shares_per_share]
        if pd.notna(row.record_date):
            terms.at[key, "record_date"] = row.record_date
        if pd.notna(row.pay_date):
            terms.loc[key, ["pay_date", "payment_evidence"]] = [row.pay_date, row.source_url]
    extra_evidence = output / "source" / "payment_evidence.csv"
    if extra_evidence.is_file():
        for row in pd.read_csv(extra_evidence).itertuples():
            key = (row.ex_date, row.ts_code)
            if (
                not np.isclose(terms.at[key, "cash"], row.cash, rtol=0, atol=1e-8)
                or terms.at[key, "record_date"] != row.record_date
            ):
                raise ValueError(f"Issuer and archive entitlement differ for {key}")
            terms.loc[key, ["pay_date", "payment_evidence"]] = [row.pay_date, row.source_url]
    terms = terms.reset_index().rename(columns={"trade_date": "ex_date"})
    terms = terms.loc[terms.ex_date.isin(days) & (terms.cash.gt(0) | terms.shares.gt(0))].copy()
    prior = dict(zip(days[1:], days[:-1]))
    for row in terms.itertuples():
        if (
            prior.get(row.ex_date) != row.record_date
            or not isinstance(row.pay_date, str)
            or row.pay_date < row.ex_date
        ):
            raise ValueError(
                f"Unresolved entitlement or settlement date: {row.ts_code} {row.ex_date}"
            )
    return terms


def daily_inputs(archive, output, end, bs):
    calendar, members, bars = supplement(archive, output, end, bs)
    dates = [d for d in calendar if d <= end][-121:]
    if len(dates) != 121 or dates[-1] != end or dates[0] < members.trade_date.min():
        raise ValueError("Window needs a seed plus 120 covered sessions")
    members = members.loc[members.trade_date.isin(dates)].copy()
    if not members.groupby("trade_date").ts_code.nunique().reindex(dates).eq(300).all():
        raise ValueError("Incomplete daily membership")
    old = pd.read_csv(archive / "daily_bars.csv")
    fields = ["open", "close", "pre_close", "high", "low", "volume", "isST"]
    overlap = old.loc[old.trade_date.isin(dates)].merge(
        bars, how="left", on=KEY, validate="one_to_one"
    )
    for field in fields:
        if not np.allclose(
            overlap[field + "_x"], overlap[field + "_y"], atol=1e-8, rtol=0, equal_nan=True
        ):
            raise ValueError(f"Archived and new BaoStock histories disagree on {field}")
    bars = bars.sort_values(["ts_code", "trade_date"]).copy()
    bars["suspended"] = bars.tradestatus.astype(str).eq("0")
    valid = ~bars.suspended
    if (
        not bars.tradestatus.astype(str).isin(["0", "1"]).all()
        or not bars.loc[valid, ["open", "close", "pre_close", "volume"]].gt(0).all().all()
    ):
        raise ValueError("Invalid tradability or prices")
    traded = bars.loc[valid].copy()
    scale = traded.groupby("ts_code").close.shift() / traded.pre_close
    traded["factor"] = scale.fillna(1).groupby(traded.ts_code).cumprod()
    bars["factor"] = traded.factor.reindex(bars.index).groupby(bars.ts_code).ffill()
    bars["adj_open"] = bars.open * bars.factor
    bars["adj_close"] = bars.close * bars.factor
    bars["adv_volume"] = bars.groupby("ts_code").volume.transform(
        lambda x: x.shift(1).rolling(20).mean()
    )
    growth = bars.ts_code.str.startswith(("30", "688", "689"))
    percent = np.where(
        growth, 20, np.where(bars.isST.eq(1) & bars.trade_date.lt("2026-07-06"), 5, 10)
    )
    cents = np.rint(bars.pre_close * 100)
    bars["up_limit"] = np.floor((cents * (100 + percent) + 50) / 100) / 100
    bars["down_limit"] = np.floor((cents * (100 - percent) + 50) / 100) / 100
    scope = members[KEY].merge(bars, on=KEY, how="left", validate="one_to_one")
    if (
        scope.suspended.isna().any()
        or (
            (~scope.suspended) & (scope.high.gt(scope.up_limit) | scope.low.lt(scope.down_limit))
        ).any()
    ):
        raise ValueError("Missing member quote or exception to dated price-band model")
    mergers = pd.read_csv(archive / "merger_evidence.csv", dtype={"ts_code": str})
    event_days = {}
    more_marks = []
    for m in mergers.itertuples():
        for day, kind in [
            (m.delisting_date, "convert"),
            (m.successor_new_share_listing_date, "release"),
        ]:
            event_days.setdefault(day, []).append(
                {
                    "kind": kind,
                    "old": m.ts_code,
                    "new": m.successor,
                    "ratio": m.share_exchange_ratio,
                }
            )
        prior_rows = bars.loc[bars.ts_code.eq(m.ts_code) & bars.trade_date.le(m.record_date)]
        if prior_rows.empty or prior_rows.trade_date.iloc[-1] != m.record_date:
            raise ValueError("Missing merger record-date valuation")
        replacement = bars.loc[
            bars.ts_code.eq(m.successor) & bars.trade_date.ge(m.delisting_date)
        ].copy()
        # Continue the old benchmark line as a fractional claim until the next executable rebalance.
        replacement["ts_code"] = m.ts_code
        replacement["adj_open"] = (
            replacement.open * m.share_exchange_ratio * prior_rows.factor.iloc[-1]
        )
        replacement["adj_close"] = (
            replacement.close * m.share_exchange_ratio * prior_rows.factor.iloc[-1]
        )
        replacement["mark_locked"] = replacement.trade_date.lt(m.successor_new_share_listing_date)
        replacement["suspended"] = replacement.mark_locked | replacement.suspended
        more_marks.append(replacement)
    bench_bars = bars.loc[bars.trade_date.isin(dates)].copy()
    bench_bars["mark_locked"] = False
    bench_bars = pd.concat([bench_bars, *more_marks], ignore_index=True)
    if bench_bars.duplicated(KEY).any():
        raise ValueError("Duplicate benchmark valuation")
    status = bench_bars.loc[bench_bars.suspended, KEY].assign(is_suspended=True)
    base = monthly_equal_weight_benchmark(members, bench_bars, status)
    if not base.audit.ready.all() or len(base.nav) != len(dates):
        raise ValueError(base.audit.loc[~base.audit.ready].head().to_string(index=False))
    base.audit.to_csv(output / "benchmark_audit.csv", index=False)
    nav = base.nav.assign(trade_date=base.nav.trade_date.astype(str)).set_index("trade_date")
    reference = base.weights.assign(trade_date=base.weights.trade_date.astype(str)).pivot(
        index="trade_date", columns="ts_code", values="weight"
    )
    for m in mergers.itertuples():
        during = reference.index >= m.delisting_date
        reference.loc[during, m.successor] = reference.loc[during, m.successor].fillna(
            0
        ) + reference.loc[during, m.ts_code].fillna(0)
        reference.loc[during, m.ts_code] = np.nan
    history = [d for d in calendar if d <= end]
    prices = bars.pivot(index="trade_date", columns="ts_code", values="adj_close").reindex(history)
    observed = prices.mask(
        bars.pivot(index="trade_date", columns="ts_code", values="suspended")
        .reindex_like(prices)
        .eq(True)
    )
    volatility = -observed.pct_change(fill_method=None).rolling(20).std()
    momentum = observed.shift(5) / observed.shift(60) - 1
    reversal = 1 - observed / observed.shift(5)
    risk = prices.pct_change(fill_method=None)
    industries = pd.read_csv(archive / "industries.csv")
    terms = company_actions(
        archive, output, end, set(members.ts_code), history, online=bs is not None
    )
    archived_end = json.loads((archive / "coverage.json").read_text())["end"]
    for day in dates:
        if day > archived_end:
            jumps = traded.loc[
                traded.trade_date.eq(day) & ~np.isclose(scale.fillna(1), 1, rtol=0, atol=1e-8)
            ]
            terms_today = terms.loc[terms.ex_date.eq(day)].set_index("ts_code")
            for row in jumps.itertuples():
                term = terms_today.loc[row.ts_code]
                expected = (row.pre_close * scale.at[row.Index] - term.cash) / (1 + term.shares)
                if abs(expected - row.pre_close) > 0.00500001:
                    raise ValueError(f"{day} {row.ts_code}: unverified ex-rights reference price")
    if not terms.rename(columns={"ex_date": "trade_date"}).merge(status, on=KEY).empty:
        raise ValueError("Suspended ex-date requires a separate carrying-value adjustment")
    cols = ["ts_code", "open", "close", "isST", "suspended", "adv_volume", "up_limit", "down_limit"]
    for i, day in enumerate(dates):
        nxt = calendar[calendar.index(day) + 1]
        pool = members.loc[members.trade_date.eq(day), "ts_code"].tolist()
        quotes = bars.loc[bars.trade_date.eq(day), cols].set_index("ts_code")
        signal = None
        if pd.Timestamp(day).to_period("W-FRI") != pd.Timestamp(nxt).to_period("W-FRI"):
            eligible = quotes.index.isin(pool) & ~quotes.suspended & quotes.isST.eq(0)
            scores = pd.concat([v.loc[day] for v in (volatility, momentum, reversal)], axis=1)
            scores = (
                scores.reindex(quotes.index[eligible])
                .replace([np.inf, -np.inf], np.nan)
                .dropna()
                .rank(pct=True)
                .mean(axis=1)
            )
            hist = risk.loc[:day].tail(60)
            names = (
                industries.loc[industries.available_at.le(day)]
                .sort_values("available_at")
                .drop_duplicates("ts_code", keep="last")
            )
            signal = {
                "scores": scores.to_dict(),
                "weights": reference.loc[day].dropna().to_dict(),
                "industry": names.set_index("ts_code").industry.to_dict(),
                "risk_returns": {
                    "dates": hist.index.tolist(),
                    "codes": hist.columns.tolist(),
                    "values": hist.astype(object).where(hist.notna(), None).values.tolist(),
                },
            }
        q = quotes.reset_index().astype(object)
        yield {
            "trade_date": day,
            "previous_trade_date": dates[i - 1] if i else None,
            "next_trade_date": nxt,
            "members": pool,
            "quotes": q.where(q.notna(), None).to_dict("records"),
            "benchmark_open": nav.at[day, "open_nav"],
            "benchmark_close": nav.at[day, "nav"],
            "signal_inputs": signal,
            "actions": terms.loc[terms.ex_date.eq(day)].to_dict("records"),
            "exchanges": event_days.get(day, []),
        }


def check_day(packet, record, previous, prior_members):
    day = packet["trade_date"]
    old = {r["ts_code"]: r for r in previous["positions"]} if previous else {}
    actual = {r["ts_code"]: r for r in record["positions"]}
    before = Counter({c: r["quantity"] for c, r in old.items()})
    expected = before.copy()
    pool = set(packet["members"])
    quotes = {r["ts_code"]: r for r in packet["quotes"]}
    removed = prior_members - pool
    start_cash = previous["account"]["cash"] if previous else 10_000_000.0
    cash = Decimal(str(start_cash)) - Decimal(str(record["account"]["fixed_cost"]))
    events = []
    locked = Counter({c: r.get("locked_quantity", 0) for c, r in old.items()})
    due = sum(
        Decimal(str(x["amount"]))
        for x in (previous or {}).get("receivables", [])
        if x["pay_date"] <= day
    )
    for term in packet["actions"]:
        if term["pay_date"] <= day:
            due += Decimal(str(fen(before[term["ts_code"]] * term["cash"])))
    if fen(float(due)) != record["account"]["dividend_received"]:
        raise ValueError(f"{day}: dividend receipts differ from entitled shares and due dates")
    for event in record["actions"]:
        cash += Decimal(str(event["cash_delta"]))
        code = event["ts_code"]
        if event["event"] == "exchange_entitlement":
            (conversion,) = [
                e for e in packet["exchanges"] if e["kind"] == "convert" and e["old"] == code
            ]
            successor = conversion["new"]
            shares = int(Decimal(before[code]) * Decimal(str(conversion["ratio"])))
            if (event["old_quantity"], event["new_quantity"], event["cash_delta"]) != (
                before[code],
                shares,
                0,
            ):
                raise ValueError(f"{day}: invalid conversion booking")
            expected[code] -= before[code]
            expected[successor] += shares
            locked[successor] += shares
            events.append(
                {
                    "event": "conversion",
                    "ts_code": code,
                    "successor": successor,
                    "before": before[code],
                    "successor_entitlement": shares,
                    "cash_delta": 0,
                }
            )
        elif event["event"] == "exchange_listing":
            if event["shares"] != locked[code] or event["cash_delta"] != 0:
                raise ValueError(f"{day}: conversion entitlement was not released exactly once")
            locked[code] -= event["shares"]
            events.append(
                {"event": "listing", "ts_code": code, "released": event["shares"], "cash_delta": 0}
            )
        elif event["event"] == "stock_distribution":
            expected[code] += event["shares"]
    for event in packet["exchanges"]:
        if (
            event["kind"] == "convert"
            and before[event["old"]]
            and not any(e["event"] == "conversion" for e in events)
        ):
            raise ValueError(f"{day}: held merger security was not converted")
        if (
            event["kind"] == "release"
            and event["old"] in (previous or {}).get("pending_exchanges", {})
            and not any(e["event"] == "listing" for e in events)
        ):
            raise ValueError(f"{day}: missing share listing")
    order_by_code = {}
    for order in record["orders"]:
        code = order["ts_code"]
        if code in order_by_code:
            raise ValueError(f"{day}: duplicate order for {code}")
        order_by_code[code] = order
        qty = order["filled_quantity"]
        buy = order["side"] == "BUY"
        if (quotes[code]["suspended"] and qty) or (buy and code not in pool):
            raise ValueError(f"{day}: invalid suspension or membership trade for {code}")
        if not buy and qty > expected[code] - locked[code]:
            raise ValueError(f"{day}: sale includes unlisted shares")
        expected[code] += qty if buy else -qty
        cash += Decimal(str(order["notional"])) * (-1 if buy else 1) - Decimal(str(order["fees"]))
    if fen(float(cash)) != record["account"]["cash"]:
        raise ValueError(f"{day}: independent cash reconciliation failed")
    if {c: q for c, q in expected.items() if q} != {c: r["quantity"] for c, r in actual.items()}:
        raise ValueError(f"{day}: independent position reconciliation failed")
    for code in old.keys() - pool:
        if expected[code] > locked[code] and not order_by_code.get(code, {}).get("forced_exit"):
            raise ValueError(f"{day}: missing forced-exit retry for {code}")
    for code, row in actual.items():
        if locked[code] != row.get("locked_quantity", 0) or row["quantity"] != (
            row["settled"] + sum(q for _, q in row["unsettled_lots"]) + locked[code]
        ):
            raise ValueError(f"{day}: unavailable shares double counted for {code}")
        if locked[code]:
            events.append(
                {
                    "event": "pending_claim",
                    "ts_code": code,
                    "locked_shares": locked[code],
                    "after": row["quantity"],
                    "ordinary_shares": row["quantity"] - locked[code],
                    "claim_close_value": fen(locked[code] * row["close"]),
                }
            )
    for code in sorted(
        removed
        | (pool - prior_members if previous else set())
        | {c for c, q in quotes.items() if q["suspended"] and (c in pool or c in old)}
    ):
        halt = quotes.get(code, {}).get("suspended", False)
        order = order_by_code.get(code, {})
        if code in removed and before[code] and expected[code] and not order.get("forced_exit"):
            raise ValueError(f"{day}: missing forced exit")
        if halt and before[code] and actual[code]["close"] != old[code]["close"]:
            raise ValueError(f"{day}: suspended mark changed without a corporate action")
        events.append(
            {
                "event": "removed"
                if code in removed
                else "added"
                if code not in prior_members
                else "suspended",
                "ts_code": code,
                "before": before[code],
                "after": expected[code],
                "suspended": halt,
                "order_status": order.get("status"),
                "filled": order.get("filled_quantity", 0),
                "reason": order.get("reason"),
                "forced_exit": order.get("forced_exit", False),
            }
        )
    account = record["account"]
    value = fen(sum(r["quantity"] * quotes[c]["close"] for c, r in actual.items()))
    if (
        value != account["market_value"]
        or fen(value + float(cash) + sum(r["amount"] for r in record["receivables"]))
        != account["nav_cny"]
    ):
        raise ValueError(f"{day}: positions, receivables and cash do not reconcile to NAV")
    return [
        {
            "trade_date": day,
            "opening_cash": start_cash,
            "closing_cash": account["cash"],
            "cash_difference": fen(float(cash) - account["cash"]),
            **e,
        }
        for e in events
    ]


def rehearse(archive, output, end, bs):
    output.mkdir(parents=True, exist_ok=True)
    inputs = output / "inputs"
    inputs.mkdir(exist_ok=True)
    history, checks = [], []
    pool = set()
    with TemporaryDirectory(dir=output) as tmp:
        db = Path(tmp) / "account.sqlite3"
        for packet in daily_inputs(archive, output, end, bs):
            path = inputs / (packet["trade_date"] + ".json")
            path.write_text(
                json.dumps(packet, allow_nan=False, separators=(",", ":")), encoding="utf-8"
            )
            record, inserted = run_day(db, path)
            retry, repeated = run_day(db, path)
            if not inserted or repeated or retry != record:
                raise ValueError("Non-idempotent day booking")
            checks.extend(check_day(packet, record, history[-1] if history else None, pool))
            history.append(record)
            pool = set(packet["members"])
        account = pd.DataFrame(
            [{"trade_date": r["trade_date"], **r["account"]} for r in history[1:]]
        )
        if len(account) != 120 or account.trade_date.iloc[-1] != end:
            raise ValueError("Incomplete rehearsal")
        account.to_csv(Path(tmp) / "account.csv", index=False)
        for field in ("positions", "orders", "actions"):
            pd.DataFrame(
                [{"trade_date": r["trade_date"], **x} for r in history for x in r[field]]
            ).to_csv(Path(tmp) / f"{field}.csv", index=False)
        events = pd.DataFrame(checks)
        events.to_csv(Path(tmp) / "event_checks.csv", index=False)
        by_day = {r["trade_date"]: r for r in history}
        for m in pd.read_csv(archive / "merger_evidence.csv").itertuples():
            if m.record_date not in by_day or m.successor_new_share_listing_date not in by_day:
                continue
            entitlement = next(
                (
                    p["quantity"]
                    for p in by_day[m.record_date]["positions"]
                    if p["ts_code"] == m.ts_code
                ),
                0,
            )
            if entitlement:
                (converted,) = [
                    e
                    for e in by_day[m.delisting_date]["actions"]
                    if e["event"] == "exchange_entitlement" and e["ts_code"] == m.ts_code
                ]
                (delivered,) = [
                    e
                    for e in by_day[m.successor_new_share_listing_date]["actions"]
                    if e["event"] == "exchange_listing" and e["old"] == m.ts_code
                ]
                if converted["old_quantity"] != entitlement or delivered["shares"] != int(
                    Decimal(entitlement) * Decimal(str(m.share_exchange_ratio))
                ):
                    raise ValueError(
                        "Record-date position does not reconcile to the delivered shares"
                    )
        risk = pd.DataFrame(
            [
                {
                    "signal_date": s["signal_date"],
                    "execution_date": s["execution_date"],
                    "decision": s["decision"],
                    **attempt,
                }
                for r in history
                if (s := r["signal"])
                for attempt in (s.get("attempts") or [{}])
            ]
        )
        risk.to_csv(Path(tmp) / "risk_checks.csv", index=False)
        local_events = pd.read_csv(
            Path("data/reference/official_membership/csi300_events_200507_202606.csv"), dtype=str
        )
        reviewed = local_events.loc[
            local_events.effective_date.between(account.trade_date.iloc[0], end)
        ]
        for date, rows in reviewed.groupby("effective_date"):
            observed = events.loc[events.trade_date.eq(date)]
            for action, label in [("ADD", "added"), ("REMOVE", "removed")]:
                if set(rows.loc[rows.action.eq(action), "ts_code"]) != set(
                    observed.loc[observed.event.eq(label), "ts_code"]
                ):
                    raise ValueError(
                        f"{date}: portfolio universe differs from reviewed {action} manifest"
                    )
        endpoint = output / "source" / "official_endpoint.xls"
        official_date = None
        if endpoint.is_file():
            from normalize_csi300_current import (
                CODE_COLUMN,
                EXCHANGE_COLUMN,
                _date_value,
                _normalize_code,
            )

            sheet = pd.read_excel(endpoint, dtype=str)
            official_date = _date_value(sheet, "official closing universe")
            if (
                official_date != end
                or len(sheet) != 300
                or {
                    _normalize_code(code, exchange)
                    for code, exchange in zip(sheet[CODE_COLUMN], sheet[EXCHANGE_COLUMN])
                }
                != pool
            ):
                raise ValueError("Final membership disagrees with dated official endpoint")
        report = {
            "mode": "historical_daily_rehearsal",
            "start": account.trade_date.iloc[0],
            "end": end,
            "sessions": len(account),
            "startup": history[0]["trade_date"],
            "official_end_membership_matched": official_date,
            "reviewed_membership_manifest_rows": len(reviewed),
            "signals_rejected_all_turnovers_above_cap": sum(
                all(attempt["turnover"] > 0.20 for attempt in s["attempts"])
                for r in history
                if (s := r["signal"]) and s["decision"] == "constraints_blocked"
            ),
            "duplicate_submissions": len(history),
            "weekly_signals": sum(r["signal"] is not None for r in history),
            "approved": sum((r["signal"] or {}).get("decision") == "approved" for r in history),
            "orders": sum(len(r["orders"]) for r in history),
            "orders_with_fills": sum(
                o["filled_quantity"] > 0 for r in history for o in r["orders"]
            ),
            "net_return": account.strategy_nav.iloc[-1] - 1,
            "benchmark_return": account.benchmark_nav.iloc[-1] - 1,
            "ending_nav_cny": account.nav_cny.iloc[-1],
            "total_cost": fen(account.total_cost.sum()),
            "max_cash_error": account.cash_error.abs().max(),
            "events": events.event.value_counts().to_dict(),
            "membership_source": "dated BaoStock snapshots",
            "price_limit_source": "dated board-rule model",
            "benchmark": "MQ300.EW seeded at window opening; locked conversion claim marked at successor price",
            "fraction_policy": "floor to whole successor shares; no assumed extra clearing share or cash",
            "conversion_details": [e for e in checks if e["event"] in ("conversion", "listing")],
            "costs_cny": {k: fen(account[k].sum()) for k in COST_FIELDS},
            "dividends_with_assumed_payment_date": sum(
                a["event"] == "dividend_paid" and a["payment_evidence"] == "assumed_ex_date"
                for r in history
                for a in r["actions"]
            ),
        }
        (Path(tmp) / "verification.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        db.replace(output / "verified.sqlite3")
        for file in Path(tmp).iterdir():
            file.replace(output / file.name)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "2026-11-17")
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    end = pd.Timestamp(args.end).date().isoformat()
    output = args.output or Path("data/exports") / f"mq300-v1-rehearsal-{end}"
    with provider(not args.offline) as bs:
        rehearse(args.archive, output, end, bs)
