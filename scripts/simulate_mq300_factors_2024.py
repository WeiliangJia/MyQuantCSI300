"""Build a local research-only factor ledger from unadjusted BaoStock quotes."""
from math import isfinite
from pathlib import Path

import pandas as pd

from myquant.processing.cleaning import rebuild_index_membership

SOURCE = "baostock_pre_close_simulated_v1"
KEY = ["trade_date", "ts_code"]
FIELDS = ["open", "close", "pre_close", "volume"]


def simulate(required, raw):
    unique = raw.drop_duplicates(KEY + FIELDS)
    conflicts = unique.duplicated(KEY, keep=False)
    quotes = unique.loc[~conflicts].assign(has_quote=True)
    conflict_keys = unique.loc[conflicts, KEY].drop_duplicates().assign(conflict=True)
    panel = required.merge(quotes, on=KEY, how="left").merge(conflict_keys, on=KEY, how="left")
    ledger, issues = [], []
    for code, rows in panel.sort_values(KEY).groupby("ts_code", sort=True):
        factor, prior_close, prior_date, broken = 1.0, None, None, False
        for row in rows.itertuples(index=False):
            reason = None
            if pd.notna(row.conflict):
                reason = "conflicting_archive_quotes"
            elif pd.isna(row.has_quote):
                reason = "missing_quote_no_suspension_evidence"
            elif not all(isfinite(value) and value > 0 for value in (row.open, row.close, row.pre_close)):
                reason = "invalid_price_or_reference_close"
            elif not isfinite(row.volume) or row.volume <= 0:
                reason = "no_positive_volume_no_suspension_evidence"
            elif broken:
                reason = "prior_gap_blocks_factor_chain"
            if reason:
                issues.append({"trade_date": row.trade_date, "ts_code": code, "reason": reason})
                broken = True
                continue
            step = prior_close / row.pre_close if prior_close is not None else 1.0
            factor *= step
            ledger.append({
                "trade_date": row.trade_date, "ts_code": code, "source": SOURCE,
                "archive_path": row.archive_path, "previous_trade_date": prior_date,
                "previous_close": prior_close, "pre_close": row.pre_close, "factor_step": step,
                "adj_factor": factor, "open": row.open, "close": row.close,
                "adj_open": row.open * factor, "adj_close": row.close * factor,
            })
            prior_close, prior_date = row.close, row.trade_date
    return pd.DataFrame(ledger), pd.DataFrame(issues, columns=KEY + ["reason"])


def main():
    root = Path("data/raw/baostock/daily")
    frames = []
    for path in sorted(root.rglob("*.csv")):
        frame = pd.read_csv(path, usecols=KEY + FIELDS)
        frame = frame.loc[frame.trade_date.between("2023-12-01", "2024-12-31")]
        if not frame.empty:
            frames.append(frame.assign(archive_path=path.as_posix()))
    if not frames:
        raise ValueError("No BaoStock quotes in the warm-up or evaluation period")
    raw = pd.concat(frames, ignore_index=True)
    days = sorted(raw.trade_date.unique())
    year_days = [day for day in days if day.startswith("2024-")]
    warmup = [day for day in days if day.startswith("2023-")]
    if not warmup or len(year_days) != 242:
        raise ValueError("Expected December 2023 warm-up and 242 observed dates in 2024")
    ref = Path("data/reference/official_membership")
    base = pd.read_csv(ref / "csi300_baseline_rebuilt_20181217.csv", dtype=str)
    events = pd.read_csv(ref / "csi300_events_201812_202606.csv", dtype=str)
    members = rebuild_index_membership(pd.Series(days), base, events[events.effective_date.gt("2018-12-17")])
    members.trade_date = members.trade_date.astype(str)
    if not members.groupby("trade_date").ts_code.nunique().eq(300).all():
        raise ValueError("Membership must contain 300 stocks on every observed date")

    # Include the preceding session for entrants and the next opening for exits.
    previous = dict(zip(days[1:], days[:-1]))
    following = dict(zip(days[:-1], days[1:]))
    required = pd.DataFrame([
        {"trade_date": day, "ts_code": code}
        for code, group in members.groupby("ts_code")
        for day in days
        if previous.get(group.trade_date.min(), days[0]) <= day
        <= following.get(group.trade_date.max(), days[-1])
    ])
    ledger, issues = simulate(required, raw)
    out = Path("data/exports/mq300-2024-simulated")
    out.mkdir(parents=True, exist_ok=True)
    ledger.to_json(out / "factors.json", orient="records", double_precision=15)
    issues.to_json(out / "anomalies.json", orient="records")
    valid_2024 = ledger.loc[ledger.trade_date.str.startswith("2024-")]
    issues_2024 = issues.loc[issues.trade_date.str.startswith("2024-")]
    membership_2024 = members.loc[members.trade_date.str.startswith("2024-"), KEY]
    member_issues = membership_2024.merge(issues_2024, on=KEY)
    summary = pd.Series({
        "source": SOURCE, "warmup_start": warmup[0], "warmup_end": warmup[-1],
        "observed_2024_sessions": len(year_days),
        "factor_rows_2024": len(valid_2024),
        "factor_changes_2024": int(valid_2024.factor_step.ne(1).sum()),
        "anomalies_2024": issues_2024.reason.value_counts().to_dict(),
        "warmup_anomalies": issues.loc[~issues.trade_date.str.startswith("2024-")].reason.value_counts().to_dict(),
        "member_anomalies_2024": member_issues.reason.value_counts().to_dict(),
        "independent_reference_close_validation": False,
        "nav_comparison": "not_run_data_not_accepted",
    })
    summary.to_json(out / "summary.json", indent=2)
    print(summary.to_json(indent=2))


if __name__ == "__main__":
    main()
