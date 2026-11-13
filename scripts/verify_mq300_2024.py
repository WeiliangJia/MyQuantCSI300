"""Fetch and verify a standalone 2024 MQ300 replay using Tushare evidence."""
from pathlib import Path
from time import sleep

import numpy as np
import pandas as pd

from myquant.config import get_settings
from myquant.data.archive import RawArchiver
from myquant.data.tushare_client import TushareClient
from myquant.processing.cleaning import rebuild_index_membership
from myquant.research.benchmark_weights import monthly_equal_weight_benchmark


def suspended_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["trade_date", "ts_code", "is_suspended"])
    full_day = frame["suspend_timing"].fillna("").str.strip().eq("")
    return frame.loc[full_day & frame["suspend_type"].eq("S"), ["trade_date", "ts_code"]].assign(
        is_suspended=True
    )


def checked_replay(members, bars, statuses):
    result = monthly_equal_weight_benchmark(members, bars, statuses)
    blocked = result.audit.loc[~result.audit.ready]
    if not blocked.empty:
        raise ValueError(blocked.head(10).to_string(index=False))
    nav = result.nav
    if len(nav) != members.trade_date.nunique():
        raise ValueError("NAV does not cover every trading day")
    np.testing.assert_allclose(nav.previous_nav + nav.overnight_pnl + nav.intraday_pnl, nav.nav)
    return nav


def main() -> None:
    settings = get_settings()
    if not settings.tushare_token:
        raise SystemExit("Set TUSHARE_TOKEN locally in .env before running the 2024 audit.")
    client = TushareClient(settings.tushare_token)
    archive = RawArchiver(settings.raw_archive_path)

    def fetch(endpoint, **params):
        frame = client.fetch(endpoint, **params)
        archive.archive(endpoint, params, frame)
        sleep(1.3)
        return frame

    calendar = fetch("trade_cal", exchange="SSE", start_date="20240101", end_date="20241231")
    if set(calendar.cal_date) != set(pd.date_range("2024-01-01", "2024-12-31").strftime("%Y%m%d")):
        raise ValueError("Incomplete 2024 exchange calendar")
    days = sorted(calendar.loc[calendar.is_open.astype(int).eq(1), "cal_date"].unique())
    if len(days) != 242:
        raise ValueError(f"Expected 242 trading days in 2024, got {len(days)}")
    ref = Path("data/reference/official_membership")
    baseline = pd.read_csv(ref / "csi300_baseline_rebuilt_20181217.csv", dtype=str)
    events = pd.read_csv(ref / "csi300_events_201812_202606.csv", dtype=str)
    members = rebuild_index_membership(pd.Series(days), baseline, events[events.effective_date.gt("2018-12-17")])
    codes = set(members.ts_code)
    bars, statuses = [], []
    for day in days:
        prices = fetch("daily", trade_date=day)
        factors = fetch("adj_factor", trade_date=day)
        status = fetch("suspend_d", trade_date=day)
        if prices.empty or factors.empty:
            raise ValueError(f"{day}: empty daily or adjustment-factor response")
        for frame in (prices, factors, status):
            if not frame.empty and not frame.trade_date.astype(str).eq(day).all():
                raise ValueError(f"{day}: response includes dates outside the request")
        quotes = prices.loc[prices.ts_code.isin(codes)].merge(
            factors[["trade_date", "ts_code", "adj_factor"]],
            on=["trade_date", "ts_code"], how="left", validate="one_to_one",
        )
        factor = pd.to_numeric(quotes.adj_factor, errors="coerce")
        factor = factor.where(np.isfinite(factor) & factor.gt(0))
        for side in ("open", "close"):
            quotes[f"adj_{side}"] = pd.to_numeric(quotes[side], errors="coerce") * factor
        quotes["adj_pre_close"] = pd.to_numeric(quotes.pre_close, errors="coerce") * factor
        bars.append(quotes)
        statuses.append(suspended_rows(status))
    quotes = pd.concat(bars, ignore_index=True).sort_values("trade_date")
    previous_close = quotes.groupby("ts_code").adj_close.shift()
    jumps = (quotes.adj_pre_close / previous_close - 1).abs().gt(.01)
    if jumps.any():
        raise ValueError(f"{int(jumps.sum())} adjusted reference-close jumps >1%; review before export")
    status = pd.concat(statuses, ignore_index=True)
    full = checked_replay(members, quotes, status)
    first_half = members.trade_date.astype(str).lt("2024-07-01")
    prefix = checked_replay(
        members.loc[first_half], quotes.loc[quotes.trade_date.lt("20240701")],
        status.loc[status.trade_date.lt("20240701")],
    )
    suffix = checked_replay(members, quotes, status).iloc[len(prefix):]
    pd.testing.assert_frame_equal(pd.concat([prefix, suffix]), full, check_exact=True)
    output = Path("data/exports/mq300-2024-nav.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    full.to_json(output, orient="records", date_format="iso", double_precision=15)
    print(f"Verified {len(full)} days; split replay matches; output={output}")


if __name__ == "__main__":
    main()
