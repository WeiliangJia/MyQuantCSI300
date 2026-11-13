from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import numpy as np
import pandas as pd

from myquant.data.free_adapters import to_ts_code


@dataclass(frozen=True)
class CleaningResult:
    run_id: str
    bars: pd.DataFrame
    issues: pd.DataFrame


def _issue(
    rows: list[dict[str, object]], run_id: str, severity: str, category: str, source: str | None,
    ts_code: str | None, trade_date: object | None, **details: object,
) -> None:
    rows.append({
        "run_id": run_id, "severity": severity, "category": category, "source": source,
        "ts_code": ts_code, "trade_date": pd.Timestamp(trade_date).date() if trade_date is not None else None,
        "details": details, "created_at": datetime.now(UTC),
    })


def normalize_source_bars(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Normalize date/code/numbers and report, rather than hide, duplicates and invalid OHLC rows."""
    required = {"source", "ts_code", "trade_date", "open", "high", "low", "close", "volume", "amount"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Source bars missing required fields: {sorted(missing)}")
    run_id = str(uuid4())
    issues: list[dict[str, object]] = []
    data = frame.copy()
    data["source"] = data["source"].str.lower()
    data["ts_code"] = data["ts_code"].map(to_ts_code)
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    for column in ("open", "high", "low", "close", "pre_close", "volume", "amount", "up_limit", "down_limit", "adj_factor"):
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    duplicates = data.duplicated(["source", "ts_code", "trade_date"], keep="last")
    for row in data.loc[duplicates].itertuples():
        _issue(issues, run_id, "WARN", "duplicate_source_bar", row.source, row.ts_code, row.trade_date)
    data = data.loc[~duplicates].copy()
    low_bound = data[["open", "close", "high"]].min(axis=1)
    high_bound = data[["open", "close", "low"]].max(axis=1)
    non_trading_placeholder = (
        data[["open", "high", "low", "close"]].isna().all(axis=1)
        & (data["volume"].isna() | (data["volume"] <= 0))
        & (data["amount"].isna() | (data["amount"] <= 0))
    )
    for row in data.loc[non_trading_placeholder].itertuples():
        _issue(issues, run_id, "INFO", "non_trading_placeholder", row.source, row.ts_code, row.trade_date)
    invalid = (
        data[["open", "high", "low", "close"]].isna().any(axis=1)
        | (data[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (data["low"] > low_bound)
        | (data["high"] < high_bound)
    ) & ~non_trading_placeholder
    for row in data.loc[invalid].itertuples():
        _issue(issues, run_id, "ERROR", "invalid_ohlc", row.source, row.ts_code, row.trade_date)
    data["is_valid_ohlc"] = ~(invalid | non_trading_placeholder)
    data["is_non_trading_placeholder"] = non_trading_placeholder
    return data, pd.DataFrame(issues)


def clean_daily_bars(
    source_bars: pd.DataFrame,
    statuses: pd.DataFrame | None = None,
    adj_factors: pd.DataFrame | None = None,
    source_priority: tuple[str, ...] = ("baostock", "akshare", "tushare"),
    close_tolerance: float = 0.0001,
) -> CleaningResult:
    """Select one valid source per stock/day while preserving cross-source discrepancies."""
    normalized, initial_issues = normalize_source_bars(source_bars)
    run_id = initial_issues["run_id"].iloc[0] if not initial_issues.empty else str(uuid4())
    issues = initial_issues.to_dict("records")
    ranks = {source: rank for rank, source in enumerate(source_priority)}
    normalized["source_rank"] = normalized["source"].map(ranks).fillna(len(ranks)).astype(int)
    status = _normalize_status(statuses)
    factors = _normalize_factors(adj_factors)
    cleaned: list[dict[str, object]] = []
    for (ts_code, trade_date), group in normalized.groupby(["ts_code", "trade_date"], sort=True):
        valid = group.loc[group["is_valid_ohlc"]].sort_values("source_rank")
        if valid.empty:
            if bool(group["is_non_trading_placeholder"].all()):
                _issue(issues, run_id, "INFO", "untradable_missing_quote", None, ts_code, trade_date)
            else:
                _issue(issues, run_id, "ERROR", "no_valid_source", None, ts_code, trade_date)
            continue
        selected = valid.iloc[0]
        for candidate in valid.iloc[1:].itertuples():
            difference = abs(float(candidate.close) - float(selected.close)) / max(abs(float(selected.close)), 1e-12)
            if difference > close_tolerance:
                _issue(
                    issues, run_id, "WARN", "source_close_disagreement", candidate.source, ts_code, trade_date,
                    selected_source=selected.source, selected_close=float(selected.close), candidate_close=float(candidate.close), relative_difference=difference,
                )
        status_row = status.get((ts_code, trade_date), {})
        zero_volume = pd.isna(selected.volume) or float(selected.volume) <= 0
        zero_amount = pd.isna(selected.amount) or float(selected.amount) <= 0
        untradable_zero_volume = bool(zero_volume and zero_amount)
        suspended = bool(status_row.get("is_suspended", False))
        single_price = float(selected.open) == float(selected.high) == float(selected.low) == float(selected.close)
        up_limit = selected.get("up_limit", np.nan)
        down_limit = selected.get("down_limit", np.nan)
        limit_up = bool(single_price and pd.notna(up_limit) and float(selected.close) >= float(up_limit))
        limit_down = bool(single_price and pd.notna(down_limit) and float(selected.close) <= float(down_limit))
        if untradable_zero_volume:
            # Preserve the quoted close for valuation, but do not imply an
            # executable market.  A free daily feed alone cannot distinguish
            # suspension from another no-trade condition with enough certainty
            # to invent a formal suspension flag.
            _issue(
                issues, run_id, "INFO", "untradable_zero_volume", str(selected.source), ts_code, trade_date,
                volume=0.0 if pd.notna(selected.volume) else None,
                amount=0.0 if pd.notna(selected.amount) else None,
            )
        factor = factors.get((ts_code, trade_date), 1.0)
        flags = {
            "is_st": bool(status_row.get("is_st", False)),
            "is_delisted": bool(status_row.get("is_delisted", False)),
            "untradable": untradable_zero_volume,
            "untradable_reason": "zero_volume_and_amount" if untradable_zero_volume else None,
            "source_candidates": list(valid["source"]),
            "corporate_action_adjustment": factor != 1.0,
        }
        cleaned.append({
            "ts_code": ts_code, "trade_date": trade_date.date(), "selected_source": selected.source,
            "open": float(selected.open), "high": float(selected.high), "low": float(selected.low), "close": float(selected.close),
            "adj_factor": factor, "adj_open": float(selected.open) * factor, "adj_high": float(selected.high) * factor,
            "adj_low": float(selected.low) * factor, "adj_close": float(selected.close) * factor,
            "volume": None if pd.isna(selected.volume) else float(selected.volume),
            "amount": None if pd.isna(selected.amount) else float(selected.amount),
            "is_suspended": suspended, "is_zero_volume": zero_volume,
            "is_limit_up_locked": limit_up, "is_limit_down_locked": limit_down,
            "flags": flags, "available_at": pd.Timestamp(selected.get("available_at", trade_date)).date(), "cleaned_at": datetime.now(UTC),
        })
    return CleaningResult(run_id, pd.DataFrame(cleaned), pd.DataFrame(issues))


def _normalize_status(statuses: pd.DataFrame | None) -> dict[tuple[str, pd.Timestamp], dict[str, object]]:
    if statuses is None or statuses.empty:
        return {}
    data = statuses.copy()
    data["ts_code"] = data["ts_code"].map(to_ts_code)
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    return {(row.ts_code, row.trade_date): row._asdict() for row in data.itertuples(index=False)}


def _normalize_factors(adj_factors: pd.DataFrame | None) -> dict[tuple[str, pd.Timestamp], float]:
    if adj_factors is None or adj_factors.empty:
        return {}
    data = adj_factors.copy()
    data["ts_code"] = data["ts_code"].map(to_ts_code)
    data["trade_date"] = pd.to_datetime(data["trade_date"])
    data["adj_factor"] = pd.to_numeric(data["adj_factor"], errors="coerce").fillna(1.0)
    # A factor supplied by a source is already point-in-time data for this bar;
    # no future normalization factor is used here.
    return {(row.ts_code, row.trade_date): float(row.adj_factor) for row in data.itertuples(index=False)}


def build_financial_snapshots(observations: pd.DataFrame, as_of_dates: pd.Series) -> pd.DataFrame:
    """Create one as-of snapshot per stock/date using only observations announced by that date."""
    if observations.empty:
        return pd.DataFrame(columns=["ts_code", "as_of_date", "payload", "latest_announcement_date"])
    data = observations.copy()
    data["available_at"] = pd.to_datetime(data["available_at"])
    data["announcement_date"] = pd.to_datetime(data["announcement_date"])
    result: list[dict[str, object]] = []
    for as_of_date in pd.DatetimeIndex(pd.to_datetime(as_of_dates).unique()).sort_values():
        known = data.loc[data["available_at"] <= as_of_date].sort_values(["ts_code", "metric", "available_at"])
        latest = known.groupby(["ts_code", "metric"], as_index=False).tail(1)
        for ts_code, group in latest.groupby("ts_code"):
            result.append({
                "ts_code": ts_code, "as_of_date": as_of_date.date(),
                "payload": {row.metric: float(row.value) for row in group.itertuples()},
                "latest_announcement_date": group["announcement_date"].max().date(), "generated_at": datetime.now(UTC),
            })
    return pd.DataFrame(result)


def rebuild_index_membership(
    trade_dates: pd.Series, baseline_members: pd.DataFrame, events: pd.DataFrame, index_code: str = "000300.SH",
) -> pd.DataFrame:
    """Reconstruct the daily member set from a reviewed baseline and official ADD/REMOVE events."""
    baseline = {to_ts_code(code) for code in baseline_members["ts_code"]}
    event_data = events.copy()
    if not event_data.empty:
        event_data["ts_code"] = event_data["ts_code"].map(to_ts_code)
        event_data["effective_date"] = pd.to_datetime(event_data["effective_date"])
        event_data["announcement_date"] = pd.to_datetime(event_data["announcement_date"])
    members = set(baseline)
    output: list[dict[str, object]] = []
    previous_trade_date: pd.Timestamp | None = None
    for trade_date in pd.DatetimeIndex(pd.to_datetime(trade_dates).unique()).sort_values():
        if event_data.empty:
            changes = pd.DataFrame()
        else:
            after_previous = (
                event_data["effective_date"] > previous_trade_date
                if previous_trade_date is not None
                else pd.Series(True, index=event_data.index)
            )
            changes = event_data.loc[
                after_previous & (event_data["effective_date"] <= trade_date)
            ]
        for event in changes.itertuples():
            if event.action == "ADD":
                members.add(event.ts_code)
            elif event.action == "REMOVE":
                members.discard(event.ts_code)
            else:
                raise ValueError(f"Unsupported membership action: {event.action}")
        known_announcements = changes["announcement_date"] if not changes.empty else pd.Series([trade_date])
        available_at = pd.Timestamp(known_announcements.max()).date()
        output.extend({
            "index_code": index_code, "ts_code": code, "trade_date": trade_date.date(), "is_member": True,
            "available_at": available_at, "source": "official_manifest", "generated_at": datetime.now(UTC),
        } for code in sorted(members))
        previous_trade_date = trade_date
    return pd.DataFrame(output)


def _prepare_membership_events(events: pd.DataFrame, index_code: str) -> pd.DataFrame:
    """Normalize and validate an official membership-event manifest."""
    required = {"index_code", "ts_code", "action", "effective_date", "announcement_date"}
    missing = required.difference(events.columns)
    if missing:
        raise ValueError(f"Membership events missing columns: {sorted(missing)}")

    data = events.copy()
    data = data.loc[data["index_code"].eq(index_code)].copy()
    if data.empty:
        raise ValueError(f"No membership events found for {index_code}")
    data["ts_code"] = data["ts_code"].map(to_ts_code)
    data["action"] = data["action"].astype(str).str.upper().str.strip()
    invalid_actions = sorted(set(data["action"]) - {"ADD", "REMOVE"})
    if invalid_actions:
        raise ValueError(f"Unsupported membership actions: {invalid_actions}")
    data["effective_date"] = pd.to_datetime(data["effective_date"], errors="raise").dt.normalize()
    data["announcement_date"] = pd.to_datetime(data["announcement_date"], errors="raise").dt.normalize()
    if (data["announcement_date"] > data["effective_date"]).any():
        raise ValueError("Membership event announcement_date cannot follow effective_date")
    duplicate = data.duplicated(["effective_date", "ts_code", "action"], keep=False)
    if duplicate.any():
        rows = data.loc[duplicate, ["effective_date", "ts_code", "action"]]
        raise ValueError(f"Duplicate membership events: {rows.to_dict('records')}")

    for effective_date, group in data.groupby("effective_date", sort=True):
        added = set(group.loc[group["action"].eq("ADD"), "ts_code"])
        removed = set(group.loc[group["action"].eq("REMOVE"), "ts_code"])
        if len(added) != len(removed):
            raise ValueError(
                f"Unbalanced membership event on {effective_date.date()}: "
                f"ADD={len(added)}, REMOVE={len(removed)}"
            )
        overlap = sorted(added & removed)
        if overlap:
            raise ValueError(
                f"Membership event on {effective_date.date()} both adds and removes: {overlap}"
            )
    return data.sort_values(["effective_date", "action", "ts_code"]).reset_index(drop=True)


def reverse_index_membership(
    current_members: pd.DataFrame,
    events: pd.DataFrame,
    target_date: str | pd.Timestamp,
    current_date: str | pd.Timestamp,
    index_code: str = "000300.SH",
    expected_members: int = 300,
    progress: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Reverse reviewed ADD/REMOVE events from a current official constituent set.

    ``target_date`` is an as-of date: events effective on that date remain applied.
    The function fails closed if any event is inconsistent with the running set.
    """
    target = pd.Timestamp(target_date).normalize()
    current = pd.Timestamp(current_date).normalize()
    if target >= current:
        raise ValueError("target_date must be earlier than current_date")

    members = {to_ts_code(code) for code in current_members["ts_code"]}
    if len(members) != len(current_members):
        raise ValueError("Current baseline contains duplicate ts_code values")
    if len(members) != expected_members:
        raise ValueError(
            f"Current baseline must contain exactly {expected_members} unique members; got {len(members)}"
        )

    data = _prepare_membership_events(events, index_code)
    data = data.loc[(data["effective_date"] > target) & (data["effective_date"] <= current)]
    dates = sorted(data["effective_date"].unique(), reverse=True)
    for position, effective_date in enumerate(dates, start=1):
        group = data.loc[data["effective_date"].eq(effective_date)]
        added = set(group.loc[group["action"].eq("ADD"), "ts_code"])
        removed = set(group.loc[group["action"].eq("REMOVE"), "ts_code"])
        missing_added = sorted(added - members)
        already_present_removed = sorted(removed & members)
        if missing_added or already_present_removed:
            raise ValueError(
                f"Cannot reverse event on {pd.Timestamp(effective_date).date()}: "
                f"added_not_present={missing_added}, removed_already_present={already_present_removed}"
            )
        members.difference_update(added)
        members.update(removed)
        if len(members) != expected_members:
            raise ValueError(
                f"Reverse event on {pd.Timestamp(effective_date).date()} produced "
                f"{len(members)} members; expected {expected_members}"
            )
        if progress:
            progress(
                f"[{position}/{len(dates)}] reverse effective_date="
                f"{pd.Timestamp(effective_date).date()} undo_add={len(added)} "
                f"restore_remove={len(removed)} members={len(members)}"
            )

    return pd.DataFrame({"ts_code": sorted(members), "as_of_date": target.date()})


def replay_index_membership_events(
    baseline_members: pd.DataFrame,
    events: pd.DataFrame,
    baseline_date: str | pd.Timestamp,
    end_date: str | pd.Timestamp,
    index_code: str = "000300.SH",
    expected_members: int = 300,
    progress: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Strictly replay official events and return the final constituent set."""
    start = pd.Timestamp(baseline_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    members = {to_ts_code(code) for code in baseline_members["ts_code"]}
    if len(members) != len(baseline_members) or len(members) != expected_members:
        raise ValueError(
            f"Baseline must contain exactly {expected_members} unique members; got {len(members)}"
        )

    data = _prepare_membership_events(events, index_code)
    data = data.loc[(data["effective_date"] > start) & (data["effective_date"] <= end)]
    dates = sorted(data["effective_date"].unique())
    for position, effective_date in enumerate(dates, start=1):
        group = data.loc[data["effective_date"].eq(effective_date)]
        added = set(group.loc[group["action"].eq("ADD"), "ts_code"])
        removed = set(group.loc[group["action"].eq("REMOVE"), "ts_code"])
        missing_removed = sorted(removed - members)
        already_present_added = sorted(added & members)
        if missing_removed or already_present_added:
            raise ValueError(
                f"Cannot replay event on {pd.Timestamp(effective_date).date()}: "
                f"removed_not_present={missing_removed}, added_already_present={already_present_added}"
            )
        members.difference_update(removed)
        members.update(added)
        if len(members) != expected_members:
            raise ValueError(
                f"Replay event on {pd.Timestamp(effective_date).date()} produced "
                f"{len(members)} members; expected {expected_members}"
            )
        if progress:
            progress(
                f"[{position}/{len(dates)}] replay effective_date="
                f"{pd.Timestamp(effective_date).date()} remove={len(removed)} "
                f"add={len(added)} members={len(members)}"
            )

    return pd.DataFrame({"ts_code": sorted(members), "as_of_date": end.date()})
