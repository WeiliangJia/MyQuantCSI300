"""Archive BaoStock paper inputs locally and report coverage without requiring PostgreSQL."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import time

import numpy as np
import pandas as pd

from myquant.data.free_adapters import normalize_baostock_daily, to_baostock_code, to_ts_code
from myquant.processing.cleaning import rebuild_index_membership, reverse_index_membership


ROOT = Path("data/exports/mq300-paper-inputs")
BAR_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,tradestatus,isST"
KEY = ["trade_date", "ts_code"]


def recent_dividends(end):
    from audit_mq300_actions_2024 import calendar

    directory = ROOT / end
    dates = pd.read_csv(directory / "calendar-response.csv", dtype=str)
    start = dates.loc[dates.is_trading_day.eq("1"), "calendar_date"].sort_values().tail(121).iloc[0]
    next_date = (pd.Timestamp(end) + pd.Timedelta(days=1)).date().isoformat()
    events = calendar(start, next_date, dataset="paper_sharebonus_calendar",
                      columns="SECUCODE,EX_DIVIDEND_DATE,REPORT_DATE,PRETAX_BONUS_RMB,BONUS_IT_RATIO,EQUITY_RECORD_DATE")
    events = events.rename(columns={"SECUCODE": "ts_code", "EX_DIVIDEND_DATE": "trade_date"})
    events["trade_date"] = events.trade_date.str.slice(0, 10)
    if not events.trade_date.between(start, end).all():
        raise ValueError("Calendar returned dates outside the requested window")
    events.to_csv(directory / "dividend_calendar_market.csv", index=False)
    print(f"Archived {len(events)} market-wide cash dividend and distribution notices", flush=True)


@contextmanager
def provider(enabled):
    if not enabled:
        yield None
        return
    import baostock as bs

    socket.setdefaulttimeout(25)
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login: {login.error_code} {login.error_msg}")
    try:
        yield bs
    finally:
        bs.logout()


def cached(path, bs, query, *args, **kwargs):
    if path.is_file():
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if bs is None:
        raise FileNotFoundError(path)
    for attempt in range(3):
        result = getattr(bs, query)(*args, **kwargs)
        rows = []
        while result.error_code == "0" and result.next():
            rows.append(result.get_row_data())
        if result.error_code not in {"10001001", "10002006", "10002007"} or attempt == 2:
            break
        print(f"Reconnecting BaoStock after {query} {path.stem}: {result.error_code}", flush=True)
        time.sleep(2 * (attempt + 1))
        if bs.login().error_code != "0":
            raise RuntimeError(f"BaoStock could not reconnect after {query} {path.stem}")
    if result.error_code != "0":
        raise RuntimeError(f"{query} {path.stem}: {result.error_code} {result.error_msg}")
    frame = pd.DataFrame(rows, columns=result.fields)
    if not len(frame.columns):
        raise ValueError(f"{query} {path.stem}: response has no schema")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)
    return frame


def industries(bs, days, directory):
    parts = []
    for day in days:
        rows = cached(directory / "industry-raw" / f"{day}.csv", bs, "query_stock_industry", date=day)
        valid = rows.loc[rows.industry.ne("") & rows.updateDate.ne("")].copy()
        if valid.empty or valid.code.duplicated().any() or valid.updateDate.gt(day).any():
            raise ValueError(f"Invalid as-of industry classification on {day}")
        parts.append(pd.DataFrame({"available_at": day, "ts_code": valid.code.map(to_ts_code),
                                   "industry": valid.industry, "source_date": valid.updateDate}))
        print(f"industry {day}: {len(valid)} rows", flush=True)
    history = pd.concat(parts, ignore_index=True)
    history.to_csv(directory / "industries.csv", index=False)
    return history


def payments_2024(bs):
    from backtest_mq300_round_one_2024 import corporate_actions
    from verify_mq300_2024 import local_inputs

    directory = ROOT / "2024"
    bars, _, _ = local_inputs()
    events = corporate_actions()
    expected = events.loc[events.cash.gt(0) & events.ts_code.isin(bars.ts_code)].copy()
    expected["trade_date"] = expected.trade_date.dt.strftime("%Y-%m-%d")
    parts = []
    for position, code in enumerate(sorted(expected.ts_code.unique()), 1):
        raw = cached(directory / "payments-raw" / f"{code}.csv", bs, "query_dividend_data",
                     to_baostock_code(code), "2024", "operate")
        if not raw.empty:
            parts.append(pd.DataFrame({"ts_code": code, "trade_date": raw.dividOperateDate,
                                       "pay_date": raw.dividPayDate, "record_date": raw.dividRegistDate,
                                       "amount": pd.to_numeric(raw.dividCashPsBeforeTax, errors="coerce")}))
        if position % 50 == 0:
            print(f"Payment responses archived for {position} symbols", flush=True)
    raw_events = pd.concat(parts, ignore_index=True).drop_duplicates()
    raw_events = raw_events.loc[raw_events.amount.gt(0) & raw_events.pay_date.ne("") & raw_events.record_date.ne("")]
    terms = raw_events.groupby(KEY, as_index=False).agg(
        amount=("amount", "sum"), pay_date=("pay_date", "first"), distinct_pay_dates=("pay_date", "nunique"),
        record_date=("record_date", "first"), distinct_record_dates=("record_date", "nunique"),
    )
    match = expected.merge(terms, on=KEY, how="left", validate="one_to_one")
    verified = match.loc[
        np.isclose(match.cash, match.amount, rtol=0, atol=1e-6) & match.distinct_pay_dates.eq(1)
        & match.distinct_record_dates.eq(1) & match.pay_date.ge(match.trade_date)
        & match.record_date.lt(match.trade_date), [*KEY, "pay_date", "record_date"],
    ]
    verified.to_csv(directory / "payments.csv", index=False)
    match.to_csv(directory / "payments_audit.csv", index=False)
    print(f"Matched BaoStock payment dates for {len(verified)}/{len(expected)} cash events", flush=True)


def recent_payments(bs, end):
    directory = ROOT / end
    dates = pd.read_csv(directory / "calendar-response.csv", dtype=str)
    start = dates.loc[dates.is_trading_day.eq("1"), "calendar_date"].sort_values().tail(121).iloc[0]
    codes = sorted(path.stem for path in (directory / "bars-raw").glob("*.csv"))
    if len(codes) < 300:
        raise ValueError("Archive the recent member quotes before retrieving their company actions")
    parts = []
    for position, code in enumerate(codes, 1):
        for year in range(int(start[:4]), int(end[:4]) + 1):
            raw = cached(directory / "payments-raw" / f"{code}-{year}.csv", bs, "query_dividend_data",
                         to_baostock_code(code), str(year), "operate")
            if not raw.empty:
                valid = raw.loc[raw.dividOperateDate.between(start, end)]
                parts.append(valid.rename(columns={"dividOperateDate": "trade_date"}).assign(ts_code=code))
        if position % 50 == 0:
            print(f"Recent dividend and distribution requests archived for {position}/{len(codes)} symbols", flush=True)
    cal = pd.concat(parts, ignore_index=True).drop_duplicates().sort_values(KEY)
    cal.to_csv(directory / "dividend_calendar_baostock.csv", index=False)
    print(f"Archived {len(cal)} recent BaoStock corporate-action notices", flush=True)


def membership_audit(members, days, directory):
    sets = {day: set(rows.ts_code) for day, rows in members.groupby("trade_date")}
    changes = []
    for prior, current in zip(days, days[1:]):
        for action, diff in (("ADD", sets[current] - sets[prior]), ("REMOVE", sets[prior] - sets[current])):
            changes.extend((current, code, action) for code in sorted(diff))
    pd.DataFrame(changes, columns=[*KEY, "action"]).to_csv(directory / "membership_vendor_changes.csv", index=False)
    ref = Path("data/reference/official_membership")
    baseline_file = ref / "csi300_baseline_20260721.csv"
    event_file = ref / "csi300_events_200507_202606.csv"
    if not baseline_file.is_file() or not event_file.is_file() or days[0] >= "2026-07-21":
        return {"official_overlap_days": 0, "official_overlap_match_days": 0}
    baseline = pd.read_csv(baseline_file, dtype=str)
    events = pd.read_csv(event_file, dtype=str)
    seed = reverse_index_membership(baseline, events, days[0], "2026-07-21")
    overlap = [day for day in days if day <= "2026-07-17"]
    known_events = events.loc[events.effective_date.gt(days[0])]
    rebuilt = rebuild_index_membership(pd.Series(overlap), seed, known_events)
    discrepancies = []
    matching = 0
    for date, rows in rebuilt.groupby("trade_date"):
        day = str(date)
        expected = set(rows.ts_code)
        if expected == sets[day]:
            matching += 1
        for side, diff in (("vendor_only", sets[day] - expected), ("official_only", expected - sets[day])):
            discrepancies.extend((day, code, side) for code in sorted(diff))
    pd.DataFrame(discrepancies, columns=[*KEY, "side"]).to_csv(directory / "membership_discrepancies.csv", index=False)
    audit = {"official_overlap_days": len(overlap), "official_overlap_match_days": matching,
             "vendor_membership_changes": len(changes)}
    current_file = directory / "official_current_endpoint.xls"
    if current_file.is_file():
        from normalize_csi300_current import CODE_COLUMN, EXCHANGE_COLUMN, _date_value, _normalize_code, _sha256

        current = pd.read_excel(current_file, dtype=str)
        official_date = _date_value(current, "official current constituents")
        official = {_normalize_code(code, exchange) for code, exchange in zip(current[CODE_COLUMN], current[EXCHANGE_COLUMN])}
        if len(current) != 300 or len(official) != 300:
            raise ValueError("Expected 300 distinct official constituents at the endpoint")
        compared_date = official_date if official_date in sets else days[-1]
        audit.update(official_endpoint_date=official_date, vendor_comparison_date=compared_date,
                     official_endpoint_overlap=len(official & sets[compared_date]),
                     official_endpoint_sha256=_sha256(current_file))
    return audit


def check_reference_terms(candidates, events, cash, shares, source):
    terms = events[KEY].assign(provider_cash=cash, provider_shares=shares).groupby(KEY).sum()
    joined = candidates.join(terms, on=KEY, validate="many_to_one")
    expected = (joined.previous_close - joined.provider_cash) / (1 + joined.provider_shares)
    candidates[f"{source}_date_matches"] = joined.provider_cash.notna()
    candidates[f"{source}_reference_matches"] = (expected - joined.pre_close).abs().le(0.00500001)


def recent(bs, end):
    start_limit = (pd.Timestamp(end) - pd.Timedelta(days=205)).date().isoformat()
    directory = ROOT / end
    cal = cached(directory / "calendar-response.csv", bs, "query_trade_dates", start_limit, end)
    days = cal.loc[cal.is_trading_day.eq("1"), "calendar_date"].sort_values().tail(121).tolist()
    if len(set(days)) != 121 or days[-1] != end:
        raise ValueError("Expected a completed trading date and 120 preceding trading dates")
    sessions = days[1:]
    print(f"Calendar: {sessions[0]} to {end} (120 sessions), seed close {days[0]}", flush=True)
    # Start quotes with the union from weekly snapshots, then verify all daily snapshots below.
    weeks = pd.Series(days).groupby(pd.to_datetime(days).to_period("W-FRI")).first().tolist()
    member_sets = {}

    def snapshot(day):
        raw = cached(directory / "membership-raw" / f"{day}.csv", bs, "query_hs300_stocks", date=day)
        if len(raw) != 300 or raw.code.nunique() != 300 or raw.updateDate.gt(day).any():
            raise ValueError(f"Incomplete or future-dated BaoStock membership on {day}")
        return raw.assign(trade_date=day, ts_code=raw.code.map(to_ts_code),
                          source="baostock_vendor_snapshot")[[*KEY, "updateDate", "source"]]

    for day in dict.fromkeys([*weeks, end]):
        member_sets[day] = snapshot(day)
    quotes = {}

    def fetch_quotes(codes):
        for number, code in enumerate(sorted(set(codes).difference(quotes)), 1):
            raw = cached(directory / "bars-raw" / f"{code}.csv", bs, "query_history_k_data_plus",
                         to_baostock_code(code), BAR_FIELDS, start_date=days[0], end_date=end,
                         frequency="d", adjustflag="3")
            normalized = normalize_baostock_daily(raw)
            if normalized.empty or normalized.trade_date.duplicated().any() or not normalized.ts_code.eq(code).all():
                raise ValueError(f"Invalid daily history for {code}")
            quotes[code] = normalized
            if number % 25 == 0:
                print(f"Raw bars archived for {len(quotes)} symbols", flush=True)

    fetch_quotes(pd.concat(member_sets.values()).ts_code)
    for day in days:
        if day not in member_sets:
            member_sets[day] = snapshot(day)
    members = pd.concat(member_sets.values(), ignore_index=True).sort_values(KEY)
    fetch_quotes(members.ts_code)
    members.to_csv(directory / "membership_vendor.csv", index=False)
    membership_status = membership_audit(members, days, directory)
    panel = pd.concat(quotes.values(), ignore_index=True).sort_values(["ts_code", "trade_date"])
    panel["is_suspended"] = panel.tradestatus.eq("0")
    amounts = ["open", "high", "low", "close", "volume", "amount", "pre_close"]
    panel["quote_ok"] = (
        panel.tradestatus.eq("1") & np.isfinite(panel[amounts]).all(axis=1)
        & panel[amounts].gt(0).all(axis=1) & panel.isST.isin([0, 1])
        & panel.low.le(panel[["open", "close"]].min(axis=1))
        & panel.high.ge(panel[["open", "close"]].max(axis=1))
    )
    growth = (
        (panel.ts_code.str.endswith(".SZ") & panel.ts_code.str.startswith("30"))
        | (panel.ts_code.str.endswith(".SH") & panel.ts_code.str.startswith(("688", "689")))
    )
    old_st = panel.trade_date.lt("2026-07-06") & panel.isST.eq(1)
    rate = np.where(growth, 20, np.where(old_st, 5, 10))
    reference = np.rint(panel.pre_close * 100)
    panel["up_limit_candidate"] = np.floor((reference * (100 + rate) + 50) / 100) / 100
    panel["down_limit_candidate"] = np.floor((reference * (100 - rate) + 50) / 100) / 100
    panel["limit_exception"] = panel.quote_ok & (
        panel.high.gt(panel.up_limit_candidate) | panel.low.lt(panel.down_limit_candidate)
    )
    traded = panel.loc[panel.quote_ok, [*KEY, "close", "pre_close"]].copy()
    prior = traded.groupby("ts_code").close.shift()
    traded["previous_close"] = prior
    traded["reference_changed"] = prior.notna() & ~np.isclose(prior, traded.pre_close, atol=0.0001, rtol=0)
    action_review = traded.loc[traded.reference_changed].copy()
    event_path = directory / "dividend_calendar_market.csv"
    if event_path.is_file():
        events = pd.read_csv(event_path, dtype={"trade_date": str, "ts_code": str})
        scoped_events = events.loc[events.ts_code.isin(quotes)].copy()
        scoped_events.to_csv(directory / "dividend_calendar_scoped.csv", index=False)
        check_reference_terms(
            action_review, scoped_events, pd.to_numeric(scoped_events.PRETAX_BONUS_RMB).fillna(0) / 10,
            pd.to_numeric(scoped_events.BONUS_IT_RATIO).fillna(0) / 10, "eastmoney",
        )
    baostock_events = directory / "dividend_calendar_baostock.csv"
    if baostock_events.is_file():
        provider_events = pd.read_csv(baostock_events, dtype=str)
        check_reference_terms(
            action_review, provider_events, pd.to_numeric(provider_events.dividCashPsBeforeTax, errors="coerce").fillna(0),
            pd.to_numeric(provider_events.dividStocksPs, errors="coerce").fillna(0)
            + pd.to_numeric(provider_events.dividReserveToStockPs, errors="coerce").fillna(0), "baostock",
        )
    issuer_path = directory / "issuer_cash_evidence.csv"
    if issuer_path.is_file():
        issuer = pd.read_csv(issuer_path, dtype={"trade_date": str, "ts_code": str})
        check_reference_terms(action_review, issuer, issuer.reference_cash_per_share, issuer.shares_per_share, "issuer")
    date_checks = action_review.filter(regex="_date_matches$")
    term_checks = action_review.filter(regex="_reference_matches$")
    action_review.to_csv(directory / "action_review_candidates.csv", index=False)
    action_review.loc[~term_checks.any(axis=1)].to_csv(directory / "action_terms_pending.csv", index=False)
    traded["adj_factor_candidate"] = (prior / traded.pre_close).fillna(1).groupby(traded.ts_code).cumprod()
    panel = panel.merge(traded[[*KEY, "adj_factor_candidate"]], on=KEY, how="left", validate="one_to_one")
    panel.to_csv(directory / "daily_bars.csv", index=False)

    month_starts = pd.Series(days).groupby(pd.to_datetime(days).to_period("M")).first().tolist()
    if bs is None:
        month_starts = [day for day in month_starts if (directory / "industry-raw" / f"{day}.csv").is_file()]
    industry = industries(bs, month_starts, directory) if month_starts else None
    evidence = members.loc[members.trade_date.isin(sessions)].merge(panel, on=KEY, how="left", validate="one_to_one")
    industry_missing = len(evidence)
    if industry is not None:
        industry_covered = pd.merge_asof(
            evidence[KEY].assign(trade_date=lambda frame: pd.to_datetime(frame.trade_date)).sort_values(KEY),
            industry[["ts_code", "available_at", "industry"]].assign(
                available_at=lambda frame: pd.to_datetime(frame.available_at),
            ).sort_values("available_at"),
            left_on="trade_date", right_on="available_at", by="ts_code",
        )
        industry_missing = int(industry_covered.industry.isna().sum())
    evidence["price_or_suspension"] = evidence.quote_ok.eq(True) | evidence.is_suspended.eq(True)
    coverage = evidence.groupby("trade_date").price_or_suspension.agg(["size", "sum"])
    coverage.columns = ["members", "price_or_suspension"]
    coverage.to_csv(directory / "coverage.csv")
    evidence.loc[~evidence.price_or_suspension, KEY].to_csv(directory / "missing_quotes.csv", index=False)
    evidence.loc[evidence.limit_exception.eq(True), [*KEY, "low", "high", "down_limit_candidate", "up_limit_candidate"]].to_csv(
        directory / "price_limit_review.csv", index=False,
    )
    report = {
        "start": sessions[0], "end": end, "trading_days": len(sessions), "symbols_with_bars": len(quotes),
        "member_rows": len(evidence), "quote_rows": int(evidence.quote_ok.eq(True).sum()),
        "confirmed_provider_suspension_rows": int(evidence.is_suspended.eq(True).sum()),
        "unexplained_quote_rows": int((~evidence.price_or_suspension).sum()),
        "industry_missing_rows": industry_missing, "industry_snapshots": month_starts,
        "complete_price_or_suspension_days": int(coverage.price_or_suspension.eq(300).sum()),
        "action_review_candidates": int(traded.reference_changed.sum()),
        "action_date_matches_by_source": {key: int(count) for key, count in date_checks.sum().items()},
        "action_terms_match_at_least_one_source": int(term_checks.any(axis=1).sum()),
        "price_limit_exception_rows": int(evidence.limit_exception.eq(True).sum()),
        **membership_status,
        "paper_ready": False,
        "paper_blockers": ["official membership event coverage", "dated price-limit evidence", "corporate-action terms and settlement"],
    }
    (directory / "coverage.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--end", help="Last completed ISO trading date for the 120-session window")
    mode.add_argument("--industry-2024", action="store_true")
    mode.add_argument("--payments-2024", action="store_true")
    mode.add_argument("--recent-dividends", metavar="END")
    mode.add_argument("--recent-payments", metavar="END")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if args.recent_dividends:
        if args.offline:
            parser.error("--recent-dividends requires the public calendar feed")
        recent_dividends(pd.Timestamp(args.recent_dividends).date().isoformat())
        return
    with provider(not args.offline) as bs:
        if args.industry_2024:
            months = pd.date_range("2024-04-01", "2024-12-01", freq="MS").strftime("%Y-%m-%d").tolist()
            industries(bs, ["2024-03-08", *months], ROOT / "2024")
        elif args.payments_2024:
            payments_2024(bs)
        elif args.recent_payments:
            recent_payments(bs, pd.Timestamp(args.recent_payments).date().isoformat())
        else:
            end = pd.Timestamp(args.end).date().isoformat()
            recent(bs, end)


if __name__ == "__main__":
    main()
