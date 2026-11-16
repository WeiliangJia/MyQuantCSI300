"""Cross-check archived BaoStock bars with Tencent quotes and Sina factor events."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path

import numpy as np
import pandas as pd
import requests


OUT = Path("data/exports/mq300-2024-simulated")
CACHE = OUT / "independent-feed"
START, END = "2023-12-01", "2024-12-31"
KEY = ["ts_code", "trade_date"]
OHLC = ["open", "close", "high", "low"]
TENCENT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SINA = "https://finance.sina.com.cn/realstock/company"


def raw_response(code, kind, url, params, offline):
    path = CACHE / f"{code}-{kind}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    if offline:
        raise ValueError("No validated response in the local cache")
    response = requests.get(url, params=params, timeout=18)
    response.raise_for_status()
    return response.text


def fetch_pair(code, offline=False):
    ticker, exchange = code.split(".")
    symbol = exchange.lower() + ticker
    result = []
    for kind in ("quotes", "factors"):
        path = CACHE / f"{code}-{kind}.txt"
        try:
            if kind == "quotes":
                raw = raw_response(code, kind, TENCENT, {
                    "param": f"{symbol},day,{START},{END},320,",
                }, offline)
                payload = json.loads(raw)
                rows = payload["data"][symbol]["day"]
                if payload["code"] != 0 or not rows or any(len(row) < 6 for row in rows):
                    raise ValueError("Invalid Tencent daily response")
                frame = pd.DataFrame([row[:6] for row in rows],
                                     columns=["trade_date", *OHLC, "reported_volume"])
                frame = frame.loc[frame.trade_date.le(END)].copy()
                # Tencent reports STAR Market turnover in shares and other A-share turnover in lots.
                frame["volume"] = pd.to_numeric(frame.pop("reported_volume"))
                if not (exchange == "SH" and ticker.startswith("688")):
                    frame["volume"] *= 100
                values = [*OHLC, "volume"]
            else:
                raw = raw_response(code, kind, f"{SINA}/{symbol}/hfq.js", None, offline)
                if not raw.startswith(f"var {symbol}hfq="):
                    raise ValueError("Unexpected Sina factor symbol")
                payload, _ = json.JSONDecoder().raw_decode(raw.split("=", 1)[1].lstrip())
                if int(payload["total"]) != len(payload["data"]):
                    raise ValueError("Incomplete Sina factor response")
                frame = pd.DataFrame(payload["data"]).rename(columns={"d": "trade_date", "f": "sina_factor"})
                values = ["sina_factor"]
            frame["trade_date"] = pd.to_datetime(frame.trade_date, format="%Y-%m-%d")
            frame[values] = frame[values].apply(pd.to_numeric)
            if frame.empty or frame.trade_date.duplicated().any() or not np.isfinite(frame[values]).all().all():
                raise ValueError("Empty, duplicate or invalid observations")
            if kind == "factors" and not frame.sina_factor.gt(0).all():
                raise ValueError("Nonpositive factor")
            if not path.exists():
                temporary = path.with_suffix(".tmp")
                temporary.write_text(raw, encoding="utf-8")
                temporary.replace(path)
            result.append((frame.assign(ts_code=code), None))
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as exc:
            result.append((None, {"ts_code": code, "feed": kind, "error": str(exc)}))
    return result


def archived_bars(ledger, extra_fields=()):
    frames = []
    for path, rows in ledger.groupby("archive_path"):
        source = pd.read_csv(path, usecols=[*KEY, *OHLC, "pre_close", "volume", *extra_fields])
        source["trade_date"] = pd.to_datetime(source.trade_date, format="%Y-%m-%d")
        source = source.drop_duplicates()
        frames.append(rows.merge(source, on=KEY, validate="one_to_one", suffixes=("", "_raw")))
    result = pd.concat(frames, ignore_index=True)
    if len(result) != len(ledger) or not all(
        np.isclose(result[field], result[f"{field}_raw"], rtol=0, atol=1e-9).all()
        for field in ("open", "close", "pre_close")
    ):
        raise ValueError("BaoStock archives no longer reproduce the factor ledger")
    return result.drop(columns=["open_raw", "close_raw", "pre_close_raw"])


def verify_half_cent_boundaries(panel):
    boundaries = panel.loc[panel.factor_status.eq("reference_half_cent_boundary")]
    if boundaries.empty:
        return
    terms = pd.read_json(OUT / "corporate_action_terms_audit.json", convert_dates=False, dtype=False)
    terms["trade_date"] = pd.to_datetime(terms.factor_date, format="%Y-%m-%d")
    boundaries = boundaries.merge(
        terms[[*KEY, "cash_per_share", "added_shares_per_share", "terms_source"]],
        on=KEY, how="left", validate="one_to_one",
    )
    confirmed = []
    for row in boundaries.itertuples(index=False):
        if pd.isna(row.terms_source):
            continue
        prior = Decimal(str(round(row.tencent_previous_close, 2)))
        reference = (prior - Decimal(row.cash_per_share)) / (1 + Decimal(row.added_shares_per_share))
        rounded = reference.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        if abs(float(reference) - row.sina_implied_reference) <= 1e-8 and rounded == Decimal(
            str(round(row.pre_close, 2))
        ):
            confirmed.append((row.ts_code, row.trade_date))
    keys = pd.MultiIndex.from_frame(panel[KEY])
    panel.loc[keys.isin(confirmed), "factor_status"] = "half_cent_confirmed_by_terms"


def reconcile(local, suspended, quotes, events, failures):
    if not quotes or not events:
        raise ValueError("Both independent feeds are required for reconciliation")
    local["trade_date"] = pd.to_datetime(local.trade_date, format="%Y-%m-%d")
    local["previous_trade_date"] = pd.to_datetime(local.previous_trade_date, format="%Y-%m-%d")
    local = archived_bars(local)
    suspended["trade_date"] = pd.to_datetime(suspended.trade_date, format="%Y-%m-%d")
    local = pd.concat([local, suspended[KEY].assign(suspended=True)], ignore_index=True)
    prices = pd.concat(quotes, ignore_index=True).rename(
        columns={field: f"tencent_{field}" for field in [*OHLC, "volume"]},
    )
    panel = local.merge(prices, on=KEY, how="left", validate="one_to_one").sort_values(KEY)
    schedule = pd.concat(events, ignore_index=True).rename(columns={"trade_date": "sina_effective_date"})
    panel = pd.merge_asof(
        panel.sort_values("trade_date"), schedule.sort_values("sina_effective_date"),
        left_on="trade_date", right_on="sina_effective_date", by="ts_code",
    ).sort_values(KEY).reset_index(drop=True)
    previous = panel[["ts_code", "trade_date", "sina_factor", "tencent_close"]].rename(columns={
        "trade_date": "previous_trade_date", "sina_factor": "sina_previous_factor",
        "tencent_close": "tencent_previous_close",
    })
    panel = panel.merge(previous, on=["ts_code", "previous_trade_date"], how="left", validate="many_to_one")
    independent_pairs = prices.sort_values(KEY).assign(
        independent_previous_date=lambda frame: frame.groupby("ts_code").trade_date.shift(),
        independent_previous_close=lambda frame: frame.groupby("ts_code").tencent_close.shift(),
    )
    anchor_pairs = independent_pairs.dropna(subset=["independent_previous_date"]).sort_values(
        "independent_previous_date",
    )
    anchor_pairs = pd.merge_asof(
        anchor_pairs, schedule.sort_values("sina_effective_date"),
        left_on="independent_previous_date", right_on="sina_effective_date", by="ts_code",
    )
    panel = panel.merge(
        anchor_pairs[[*KEY, "independent_previous_close", "sina_factor"]].rename(
            columns={"sina_factor": "independent_previous_factor"},
        ), on=KEY, how="left", validate="one_to_one",
    )
    panel["suspended"] = panel.suspended.eq(True)
    trading = ~panel.suspended
    has_quote = panel[[f"tencent_{field}" for field in OHLC]].gt(0).all(axis=1) & panel.tencent_volume.gt(0)
    missing_feed = ~panel.ts_code.isin(prices.ts_code)
    for field in [*OHLC, "volume"]:
        panel[f"{field}_difference"] = panel[f"tencent_{field}"] - panel[field]
    panel["quote_status"] = np.select(
        [missing_feed, panel.suspended & has_quote, panel.suspended, ~has_quote,
         panel[[f"{f}_difference" for f in OHLC]].abs().gt(1e-8).any(axis=1),
         panel.volume_difference.abs().gt(50 + 1e-8),
         panel.volume_difference.abs().gt(1e-8)],
        ["missing_quote_feed", "suspension_conflict", "suspension_consistent", "missing_quote", "price_difference",
         "volume_difference", "volume_within_half_lot"],
        default="exact",
    )

    # Compare factors on BaoStock's previous/current trading pair, including a
    # provider event dated during a suspension that first affects the resume quote.
    panel["sina_step"] = panel.sina_factor / panel.sina_previous_factor
    panel["step_difference_bps"] = (panel.sina_step / panel.factor_step - 1) * 10_000
    panel["factor_change_evidence"] = np.select(
        [panel.sina_step.isna(), panel.sina_step.ne(1) & panel.factor_step.ne(1),
         panel.sina_step.ne(1), panel.factor_step.ne(1)],
        ["not_comparable", "both", "sina_only", "baostock_only"],
        default="neither",
    )
    panel["sina_implied_reference"] = panel.tencent_previous_close / panel.sina_step
    panel["reference_difference"] = panel.sina_implied_reference - panel.pre_close
    anchor_row = trading & panel.previous_trade_date.isna()
    panel["anchor_reference_difference"] = (
        panel.independent_previous_close * panel.independent_previous_factor / panel.sina_factor
        - panel.pre_close
    ).where(anchor_row)
    panel["rounding_boundary"] = np.isclose(
        panel.reference_difference.abs(), 0.005, rtol=0, atol=1e-8,
    )
    panel["factor_status"] = np.select(
        [panel.suspended, panel.sina_factor.isna(),
         anchor_row & panel.anchor_reference_difference.abs().le(0.005 + 1e-8),
         anchor_row & panel.anchor_reference_difference.isna(), anchor_row,
         panel.sina_step.isna() | panel.sina_implied_reference.isna(),
         panel.step_difference_bps.abs().le(1e-6) & panel.reference_difference.abs().le(1e-8),
         panel.factor_change_evidence.eq("both") & panel.rounding_boundary,
         panel.factor_change_evidence.eq("both") & panel.reference_difference.abs().lt(0.005)],
        ["suspended", "missing_factor", "anchor_reference_matches", "anchor_no_prior_quote",
         "anchor_reference_difference", "missing_comparison",
         "step_exact", "reference_half_cent_boundary",
         "reference_within_half_cent"],
        default="reference_difference",
    )
    anchor = panel.sina_factor.where(trading & panel.previous_trade_date.isna()).groupby(
        panel.ts_code,
    ).transform("first")
    panel["sina_normalized_factor"] = (panel.sina_factor / anchor).where(trading)
    panel["normalized_difference_bps"] = (
        panel.sina_normalized_factor / panel.adj_factor - 1
    ) * 10_000
    verify_half_cent_boundaries(panel)
    panel = panel.sort_values(["trade_date", "ts_code"])
    panel.to_csv(OUT / "independent_feed_reconciliation_2024.csv", index=False)
    year = panel.loc[panel.trade_date.dt.year.eq(2024)]
    daily = pd.concat({
        field: pd.crosstab(year.trade_date, year[field])
        for field in ("quote_status", "factor_status")
    }, axis=1)
    daily.columns = [f"{field}_{status}" for field, status in daily.columns]
    daily.to_csv(OUT / "independent_feed_daily_2024.csv")
    differences = panel.loc[
        ~panel.quote_status.isin(["exact", "volume_within_half_lot", "suspension_consistent"])
        | ~panel.factor_status.isin(["step_exact", "reference_within_half_cent",
                                     "half_cent_confirmed_by_terms", "anchor_reference_matches",
                                     "suspended"])
    ]
    differences.to_csv(OUT / "independent_feed_differences_2024.csv", index=False)
    panel.loc[panel.factor_change_evidence.isin(["both", "sina_only", "baostock_only"])].to_csv(
        OUT / "independent_feed_factor_events_2024.csv", index=False,
    )
    summary = {
        "securities": int(panel.ts_code.nunique()), "sessions_2024": int(year.trade_date.nunique()),
        "trading_rows_2024": int((~year.suspended).sum()),
        "suspended_rows_2024": int(year.suspended.sum()),
        "response_failures": len(failures),
        "2024": {field: year[field].value_counts().to_dict() for field in ("quote_status", "factor_status")},
        "warmup": {field: panel.loc[panel.trade_date.dt.year.eq(2023), field].value_counts().to_dict()
                   for field in ("quote_status", "factor_status")},
        "factor_changes_2024": year.factor_change_evidence.value_counts().to_dict(),
        "max_abs_step_difference_bps_2024": float(year.step_difference_bps.abs().max()),
        "max_abs_normalized_difference_bps_2024": float(year.normalized_difference_bps.abs().max()),
        "unresolved_rows_2024": int(differences.trade_date.dt.year.eq(2024).sum()),
        "unresolved_rows_warmup": int(differences.trade_date.dt.year.eq(2023).sum()),
    }
    (OUT / "independent_feed_summary_2024.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    if failures or not differences.empty:
        raise SystemExit("Independent feed reconciliation has unresolved rows or responses")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-only", action="store_true", help="Cache provider responses")
    parser.add_argument("--offline", action="store_true", help="Reconcile only cached provider responses")
    args = parser.parse_args()
    CACHE.mkdir(exist_ok=True)
    local = pd.read_json(OUT / "factors.json", convert_dates=False, precise_float=True)
    suspended = pd.read_json(OUT / "confirmed_suspensions.json", convert_dates=False)
    codes = sorted(set(local.ts_code) | set(suspended.ts_code))
    quotes, events, failures = [], [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for index, pair in enumerate(pool.map(lambda code: fetch_pair(code, args.offline), codes), 1):
            for target, (frame, error) in zip((quotes, events), pair):
                if error:
                    failures.append(error)
                else:
                    target.append(frame)
            if index % 25 == 0 or index == len(codes):
                print(f"{index}/{len(codes)} securities, {len(failures)} response failures", flush=True)
    pd.DataFrame(failures, columns=["ts_code", "feed", "error"]).to_csv(
        OUT / "independent_feed_errors_2024.csv", index=False,
    )
    if not args.download_only:
        reconcile(local, suspended, quotes, events, failures)


if __name__ == "__main__":
    main()
