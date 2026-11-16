"""Verify the 2024 MQ300 replay and compare the archived data feeds."""
import argparse
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep

import numpy as np
import pandas as pd

from myquant.processing.cleaning import rebuild_index_membership
from myquant.research.benchmark_weights import monthly_equal_weight_benchmark


LOCAL_AUDIT = Path("data/exports/mq300-2024-simulated")


def year_members(days):
    ref = Path("data/reference/official_membership")
    baseline = pd.read_csv(ref / "csi300_baseline_rebuilt_20181217.csv", dtype=str)
    events = pd.read_csv(ref / "csi300_events_201812_202606.csv", dtype=str)
    return rebuild_index_membership(
        pd.Series(days), baseline, events[events.effective_date.gt("2018-12-17")],
    )


def suspended_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["trade_date", "ts_code", "is_suspended"])
    full_day = frame["suspend_timing"].fillna("").str.strip().eq("")
    return frame.loc[full_day & frame["suspend_type"].eq("S"), ["trade_date", "ts_code"]].assign(
        is_suspended=True
    )


def checked_replay(members, bars, statuses, **kwargs):
    result = monthly_equal_weight_benchmark(members, bars, statuses, **kwargs)
    blocked = result.audit.loc[~result.audit.ready]
    if not blocked.empty:
        raise ValueError(blocked.head(10).to_string(index=False))
    nav = result.nav
    if len(nav) != members.trade_date.nunique():
        raise ValueError("NAV does not cover every trading day")
    np.testing.assert_allclose(nav.previous_nav + nav.overnight_pnl + nav.intraday_pnl, nav.nav)
    return result


def compare_replays(members, bars, statuses, dividends=None):
    split = pd.Timestamp("2024-07-01")

    def half(frame, first):
        dates = pd.to_datetime(frame.trade_date, format="mixed")
        return frame.loc[dates.lt(split) if first else dates.ge(split)]

    prefix = checked_replay(
        half(members, True), half(bars, True), half(statuses, True),
        suspended_dividends=half(dividends, True) if dividends is not None else None,
    )
    if prefix.closing_state is None:
        raise ValueError("First half did not produce a closing state")
    suffix = checked_replay(
        half(members, False), half(bars, False), half(statuses, False),
        suspended_dividends=half(dividends, False) if dividends is not None else None,
        initial_state=prefix.closing_state,
    )
    full = checked_replay(members, bars, statuses, suspended_dividends=dividends)
    for field in ("nav", "weights", "rebalances", "audit"):
        split_rows = pd.concat([getattr(prefix, field), getattr(suffix, field)], ignore_index=True)
        pd.testing.assert_frame_equal(split_rows, getattr(full, field), check_exact=True)
    return full, prefix, suffix


def exchange_days(bars):
    # https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20231226_5733941.shtml
    # https://www.szse.cn/disclosure/notice/t20231226_605108.html
    closures = pd.to_datetime([
        "2024-01-01", "2024-02-09", "2024-02-12", "2024-02-13", "2024-02-14",
        "2024-02-15", "2024-02-16", "2024-04-04", "2024-04-05", "2024-05-01",
        "2024-05-02", "2024-05-03", "2024-06-10", "2024-09-16", "2024-09-17",
        "2024-10-01", "2024-10-02", "2024-10-03", "2024-10-04", "2024-10-07",
    ])
    days = pd.bdate_range("2024-01-01", "2024-12-31").difference(closures)
    observed = pd.to_datetime(bars.loc[bars.trade_date.str.startswith("2024"), "trade_date"])
    if len(days) != 242 or set(observed) != set(days):
        raise ValueError("Local bars do not match the 2024 exchange trading calendar")
    return days


def local_inputs():
    bars = pd.read_json(LOCAL_AUDIT / "replay_adjusted_bars.json", convert_dates=False, precise_float=True)
    status = pd.read_json(LOCAL_AUDIT / "confirmed_suspensions.json", convert_dates=False)
    dividends = pd.read_json(LOCAL_AUDIT / "replay_suspended_dividends.json", convert_dates=False,
                             precise_float=True)
    if not pd.read_json(LOCAL_AUDIT / "anomalies.json").empty:
        raise ValueError("Unresolved anomalies in the simulated factor ledger")
    return bars, status, dividends


def compare_local() -> None:
    bars, status, dividends = local_inputs()
    days = exchange_days(bars)
    full, prefix, suffix = compare_replays(year_members(days), bars, status, dividends)
    split = pd.concat([prefix.nav, suffix.nav], ignore_index=True)
    comparison = full.nav.merge(split[["trade_date", "nav"]], on="trade_date",
                                validate="one_to_one", suffixes=("_full", "_split"))
    comparison["nav_difference"] = comparison.nav_split - comparison.nav_full
    output = LOCAL_AUDIT / "replay_comparison_2024.csv"
    temporary = output.with_suffix(".tmp")
    comparison.to_csv(temporary, index=False)
    temporary.replace(output)
    print(f"Local replay: {len(prefix.nav)} + {len(suffix.nav)} days; NAV, weights and audits match exactly")


def independent_inputs(bars, status):
    from reconcile_suspended_dividend_2024 import huadian_inputs

    summary = json.loads((LOCAL_AUDIT / "independent_feed_summary_2024.json").read_text(encoding="utf-8"))
    if any(summary[key] for key in ("response_failures", "unresolved_rows_2024", "unresolved_rows_warmup")):
        raise ValueError("Independent feed reconciliation has unresolved results")
    keys = ["trade_date", "ts_code"]
    panel = pd.read_csv(
        LOCAL_AUDIT / "independent_feed_reconciliation_2024.csv",
        usecols=[*keys, "suspended", "tencent_open", "tencent_close",
                 "sina_normalized_factor", "sina_step", "sina_implied_reference", "sina_effective_date"],
        float_precision="round_trip",
    )
    if panel.duplicated(keys).any():
        raise ValueError("Duplicate independent feed keys")
    quotes = panel.loc[~panel.suspended].copy()
    if (set(quotes[keys].itertuples(index=False, name=None))
            != set(bars[keys].itertuples(index=False, name=None))
            or set(panel.loc[panel.suspended, keys].itertuples(index=False, name=None))
            != set(status[keys].itertuples(index=False, name=None))):
        raise ValueError("Independent feed keys differ from the reviewed quote or suspension inputs")

    quotes = quotes.rename(columns={
        "tencent_open": "open", "tencent_close": "close",
        "sina_normalized_factor": "adj_factor", "sina_step": "factor_step",
        "sina_implied_reference": "pre_close",
    })
    fields = quotes[["open", "close", "adj_factor"]]
    if not (np.isfinite(fields) & fields.gt(0)).all().all():
        raise ValueError("Invalid independent prices or factors")
    huadian = panel.loc[panel.ts_code.eq("600027.SH") & panel.trade_date.eq("2024-08-02")]
    if huadian.sina_effective_date.tolist() != ["2024-07-25"]:
        raise ValueError("Sina factor change does not align with the reviewed Huadian ex-date")
    quotes["adj_open"] = quotes.open * quotes.adj_factor
    quotes["adj_close"] = quotes.close * quotes.adj_factor
    return huadian_inputs(quotes)


def export_annual_nav() -> None:
    bars, status, dividends = local_inputs()
    independent_bars, independent_dividends = independent_inputs(bars, status)
    members = year_members(exchange_days(bars))
    baostock, first, second = compare_replays(members, bars, status, dividends)
    independent, _, _ = compare_replays(members, independent_bars, status, independent_dividends)
    pd.testing.assert_frame_equal(baostock.rebalances, independent.rebalances, check_exact=True)

    for result in (baostock, independent):
        cash_dates = result.nav.loc[result.nav.dividend_cash.gt(0), "trade_date"].astype(str).tolist()
        if cash_dates != ["2024-07-25"] or result.nav.open_nav.iloc[0] != 1.0:
            raise ValueError("Unexpected suspended dividend posting or NAV opening base")

    daily = baostock.nav.merge(
        independent.nav, on="trade_date", validate="one_to_one",
        suffixes=("_baostock", "_tencent_sina"),
    )
    if len(daily) != 242:
        raise ValueError("Expected 242 matched NAV rows")
    daily["nav_difference"] = daily.nav_tencent_sina - daily.nav_baostock
    daily["relative_difference_bps"] = daily.nav_difference / daily.nav_baostock * 10_000
    for source in ("baostock", "tencent_sina"):
        nav = daily[f"nav_{source}"]
        daily[f"drawdown_{source}"] = nav / nav.cummax().clip(lower=1) - 1
    worst = daily.loc[daily.nav_difference.abs().idxmax()]
    worst_relative = daily.loc[daily.relative_difference_bps.abs().idxmax()]
    last = daily.iloc[-1]
    report = f"""# MQ300.EW 2024 年度净值验收

两套回放均以 {daily.trade_date.iloc[0]} 开盘为 1，覆盖至 {last.trade_date} 的 242 个交易日。
主序列采用 BaoStock 已复核行情和复权因子。独立序列采用腾讯未复权开盘价、收盘价和新浪后复权因子，沿用相同的沪深 300 历史成分、调仓规则和 86 条停牌记录。

华电国际按每原股 0.15 元在 2024-07-25 计入现金，现金金额按各源停牌前因子换算。
两套序列均从复牌行情因子中移除这笔除息带来的跳变，避免重复计入。
两套回放的调仓记录完全一致，各自的上半年 {len(first.nav)} 日与从 6 月 28 日状态续跑的下半年 {len(second.nav)} 日均与全年逐日吻合。

| 指标 | BaoStock | 腾讯行情与新浪因子 |
| --- | ---: | ---: |
| 年末净值 | {last.nav_baostock:.9f} | {last.nav_tencent_sina:.9f} |
| 年度收益 | {last.nav_baostock - 1:.6%} | {last.nav_tencent_sina - 1:.6%} |
| 最大回撤 | {daily.drawdown_baostock.min():.6%} | {daily.drawdown_tencent_sina.min():.6%} |

- 最大绝对净值偏差为 {abs(worst.nav_difference):.9f}，发生于 {worst.trade_date}。
- 最大相对净值偏差为 {abs(worst_relative.relative_difference_bps):.6f} 个基点，发生于 {worst_relative.trade_date}。
- 年末收益差为 {last.nav_difference * 10_000:+.6f} 个基点，方向为腾讯与新浪减 BaoStock。

最大回撤按日末净值计算，首日开盘基值 1 计入历史峰值。两套序列分别保留各数据源的复权精度。
跨源净值偏差按实测记录，项目未设跨源净值差的通过阈值。

逐日净值及损益见 `mq300-2024-nav.csv`。复跑命令为 `python scripts/verify_mq300_2024.py --annual-nav`。
输入来自本地 `mq300-2024-simulated/` 下的独立数据对账明细、已复核 BaoStock 回放行情和停牌派息台账。
"""
    for filename, content in (
        ("mq300-2024-nav.csv", daily.to_csv(index=False)),
        ("mq300-2024-acceptance.md", report),
    ):
        destination = LOCAL_AUDIT.parent / filename
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=destination.parent,
                                delete=False) as temporary:
            temporary.write(content)
        Path(temporary.name).replace(destination)
    print(report)


def verify_tushare() -> None:
    from myquant.config import get_settings
    from myquant.data.archive import RawArchiver
    from myquant.data.tushare_client import TushareClient

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
    members = year_members(days)
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
    full, prefix, suffix = compare_replays(members, quotes, status)
    output = Path("data/exports/mq300-2024-nav.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    full.nav.to_json(output, orient="records", date_format="iso", double_precision=15)
    print(f"Verified {len(prefix.nav)} + {len(suffix.nav)} days; split replay matches; output={output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="Compare the local research replays")
    mode.add_argument("--annual-nav", action="store_true", help="Export 2024 NAV after cross-feed replay")
    args = parser.parse_args()
    if args.annual_nav:
        export_annual_nav()
    elif args.local:
        compare_local()
    else:
        verify_tushare()
