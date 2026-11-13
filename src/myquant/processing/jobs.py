from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pandas as pd
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from myquant.models import (
    BenchmarkRebalance,
    CleanDailyBar, CleanDailyFeature, DailyBar, DailyObservation, DataQualityIssue,
    FinancialObservation, FinancialSnapshot, IndexConstituent, IndexMembershipEvent, IndexMembershipSnapshot,
    SecurityStatus, SourceDailyBar,
)
from myquant.processing.cleaning import build_financial_snapshots, clean_daily_bars, rebuild_index_membership
from myquant.research.benchmark_weights import (
    free_float_market_cap_weights,
    monthly_equal_weight_benchmark,
)


def _frame(session: Session, model: type[Any], start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    statement = select(model)
    if start_date:
        date_column = getattr(model, "trade_date", None) or getattr(model, "as_of_date", None)
        if date_column is not None:
            statement = statement.where(date_column >= pd.Timestamp(start_date).date())
            if end_date:
                statement = statement.where(date_column <= pd.Timestamp(end_date).date())
    rows = session.execute(statement).scalars().all()
    records = [{column.name: getattr(row, column.name) for column in model.__table__.columns} for row in rows]
    frame = pd.DataFrame(records)
    return frame


class DataProcessingService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def seed_source_layer_from_legacy_bars(self) -> int:
        """One-time migration for rows downloaded before source_daily_bar existed."""
        legacy = _frame(self.session, DailyBar)
        if legacy.empty:
            return 0
        rows = [{
            "source": record["source_revision"], "ts_code": record["ts_code"], "trade_date": record["trade_date"],
            "open": record["open"], "high": record["high"], "low": record["low"], "close": record["close"],
            "pre_close": record["pre_close"], "volume": record["volume"], "amount": record["amount"],
            "available_at": record["available_at"], "source_revision": record["source_revision"], "ingested_at": record["ingested_at"],
        } for record in legacy.to_dict("records")]
        self._upsert(SourceDailyBar, rows, ["source", "ts_code", "trade_date"])
        self.session.commit()
        return len(rows)

    def clean_market_data(
        self, start_date: str | None = None, end_date: str | None = None,
        excluded_ts_codes: set[str] | None = None,
    ) -> tuple[str, int, int]:
        bars = _frame(self.session, SourceDailyBar, start_date, end_date)
        exclusions = excluded_ts_codes or set()
        if exclusions and not bars.empty:
            bars = bars.loc[~bars["ts_code"].isin(exclusions)]
        if bars.empty:
            return "", 0, 0
        statuses = _frame(self.session, SecurityStatus, start_date, end_date)
        factors = self._adjustment_factors(start_date, end_date)
        result = clean_daily_bars(bars, statuses, factors)
        self._upsert(CleanDailyBar, result.bars.to_dict("records"), ["ts_code", "trade_date"])
        if not result.issues.empty:
            self.session.add_all(DataQualityIssue(**record) for record in result.issues.to_dict("records"))
        self._clean_daily_features(start_date, end_date, exclusions)
        self.session.commit()
        return result.run_id, len(result.bars), len(result.issues)

    def build_financial_snapshots(
        self, start_date: str | None = None, end_date: str | None = None,
        excluded_ts_codes: set[str] | None = None,
    ) -> int:
        observations = _frame(self.session, FinancialObservation)
        dates = _frame(self.session, CleanDailyBar, start_date, end_date)
        exclusions = excluded_ts_codes or set()
        if exclusions:
            observations = observations.loc[~observations["ts_code"].isin(exclusions)] if not observations.empty else observations
            dates = dates.loc[~dates["ts_code"].isin(exclusions)] if not dates.empty else dates
        if observations.empty or dates.empty:
            return 0
        snapshots = build_financial_snapshots(observations, dates["trade_date"])
        self._upsert(FinancialSnapshot, snapshots.to_dict("records"), ["ts_code", "as_of_date"])
        self.session.commit()
        return len(snapshots)

    def rebuild_membership(
        self, baseline_path: Path, index_code: str, start_date: str, end_date: str,
        excluded_ts_codes: set[str] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> int:
        baseline = pd.read_csv(baseline_path, dtype=str)
        required = {"ts_code", "as_of_date"}
        if missing := required.difference(baseline.columns):
            raise ValueError(f"Baseline file must contain {sorted(missing)}")
        baseline_dates = pd.to_datetime(baseline["as_of_date"], errors="coerce").dropna().dt.normalize().unique()
        if len(baseline_dates) != 1:
            raise ValueError("Baseline file must contain exactly one valid as_of_date")
        baseline_date = pd.Timestamp(baseline_dates[0]).date()
        requested_start = pd.Timestamp(start_date).date()
        if requested_start < baseline_date:
            raise ValueError("Cannot rebuild membership before the official baseline as_of_date")
        if baseline["ts_code"].nunique() != 300 or len(baseline) != 300:
            raise ValueError("Official CSI 300 baseline must contain exactly 300 unique ts_code values")
        if progress:
            progress(
                f"[1/4] baseline validated: as_of={baseline_date} members=300 "
                f"requested={start_date}..{end_date}"
            )
        calendar_dates = self.session.execute(
            select(CleanDailyBar.trade_date)
            .where(CleanDailyBar.trade_date >= baseline_date)
            .where(CleanDailyBar.trade_date <= pd.Timestamp(end_date).date())
            .distinct()
            .order_by(CleanDailyBar.trade_date)
        ).scalars().all()
        if not calendar_dates:
            raise ValueError("Clean daily bars are required before rebuilding membership")
        if progress:
            progress(
                f"[2/4] trading calendar loaded: dates={len(calendar_dates)} "
                f"first={calendar_dates[0]} last={calendar_dates[-1]}"
            )
        events = _frame(self.session, IndexMembershipEvent)
        events = events.loc[events["index_code"] == index_code] if not events.empty else events
        if not events.empty:
            event_dates = pd.to_datetime(events["effective_date"], errors="raise").dt.date
            events = events.loc[event_dates > baseline_date].copy()
        snapshots = rebuild_index_membership(
            pd.Series(calendar_dates), baseline, events, index_code
        )
        snapshots = snapshots.loc[pd.to_datetime(snapshots["trade_date"]).dt.date >= requested_start]
        counts = snapshots.groupby("trade_date")["ts_code"].nunique()
        if counts.empty or not counts.eq(300).all():
            invalid = counts.loc[~counts.eq(300)].head(10).to_dict()
            raise ValueError(f"Membership snapshot validation failed: {invalid}")
        if progress:
            progress(
                f"[3/4] snapshots materialized: dates={len(counts)} rows={len(snapshots)} "
                "members_per_date=300"
            )
        # Keep all 300 official members as benchmark evidence.  The separate
        # tradable-universe stage applies user research exclusions, so excluded
        # symbols never enter factors, optimizer candidates, or orders.
        self.session.execute(
            delete(IndexMembershipSnapshot)
            .where(IndexMembershipSnapshot.index_code == index_code)
            .where(IndexMembershipSnapshot.trade_date >= requested_start)
            .where(IndexMembershipSnapshot.trade_date <= pd.Timestamp(end_date).date())
        )
        batch_size = 50_000
        for start in range(0, len(snapshots), batch_size):
            stop = min(start + batch_size, len(snapshots))
            self._upsert(
                IndexMembershipSnapshot,
                snapshots.iloc[start:stop].to_dict("records"),
                ["index_code", "ts_code", "trade_date"],
                batch_size=5_000,
            )
            if progress:
                progress(
                    f"[4/4] database upsert: rows={stop}/{len(snapshots)} "
                    f"progress={stop / len(snapshots):.1%}"
                )
        self.session.commit()
        return len(snapshots)

    def quality_summary(self, run_id: str | None = None) -> pd.DataFrame:
        issues = _frame(self.session, DataQualityIssue)
        if issues.empty:
            return pd.DataFrame(columns=["severity", "category", "count"])
        if run_id:
            issues = issues.loc[issues["run_id"] == run_id]
        return issues.groupby(["severity", "category"], as_index=False).size().rename(columns={"size": "count"})

    def build_monthly_equal_weight_benchmark(
        self,
        membership_index_code: str,
        output_index_code: str,
        start_date: str,
        end_date: str,
        *,
        require_verified_nav: bool = False,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Materialise the fail-closed MQ300 monthly equal-weight benchmark."""
        if output_index_code != "MQ300.EW":
            raise ValueError("Equal-weight output index must be MQ300.EW")
        requested_start = pd.Timestamp(start_date).normalize()
        requested_end = pd.Timestamp(end_date).normalize()
        if requested_start > requested_end:
            raise ValueError("start_date must not be after end_date")
        # Replay from inception: suspended positions can survive across month boundaries.
        members = _frame(self.session, IndexMembershipSnapshot)
        members = (
            members.loc[members["index_code"].eq(membership_index_code)]
            if not members.empty
            else members
        )
        if members.empty:
            raise ValueError("No membership snapshots found")
        members = members.loc[pd.to_datetime(members["trade_date"]) <= requested_end]
        if members.empty:
            raise ValueError("No membership snapshots found before end_date")
        build_start = pd.to_datetime(members["trade_date"]).min().date().isoformat()
        bars = _frame(self.session, CleanDailyBar, build_start, end_date)
        statuses = _frame(self.session, SecurityStatus, build_start, end_date)
        if require_verified_nav:
            factors = self._adjustment_factors(build_start, end_date)
            if factors.empty:
                raise ValueError("No archived adjustment factors; cannot publish verified NAV")
            bars = bars.merge(
                factors.rename(columns={"adj_factor": "archived_factor"}),
                on=["ts_code", "trade_date"], how="left", validate="one_to_one",
            )
            factor = pd.to_numeric(bars["archived_factor"], errors="coerce")
            verified = factor.gt(0) & factor.lt(float("inf"))
            for side in ("open", "close"):
                verified &= (
                    pd.to_numeric(bars[f"adj_{side}"]) - pd.to_numeric(bars[side]) * factor
                ).abs().le(0.000001)
            bars.loc[~verified, ["adj_open", "adj_close"]] = float("nan")
        build = monthly_equal_weight_benchmark(members, bars, statuses)
        audit = build.audit.loc[
            pd.to_datetime(build.audit["trade_date"]).between(requested_start, requested_end)
        ].copy()
        weights = build.weights.loc[
            pd.to_datetime(build.weights["trade_date"]).between(requested_start, requested_end)
        ].copy()
        if audit.empty:
            raise ValueError("No membership snapshots found in the requested date range")
        if require_verified_nav and len(build.nav) != len(build.audit):
            first = build.audit.loc[~build.audit["ready"]].iloc[0]
            raise ValueError(
                f"NAV replay blocked at {first.trade_date.date()}: {first.reason}; "
                f"missing or unverified prices={first.missing_price_count}"
            )
        if not audit["ready"].all():
            return pd.DataFrame(columns=build.weights.columns), audit

        self.session.execute(
            delete(IndexConstituent)
            .where(IndexConstituent.index_code == output_index_code)
            .where(IndexConstituent.trade_date >= requested_start.date())
            .where(IndexConstituent.trade_date <= requested_end.date())
        )
        now = datetime.now(UTC)
        rows = [{
            **record,
            "index_code": output_index_code,
            "available_at": record["trade_date"],
            "ingested_at": now,
        } for record in weights.to_dict("records")]
        self._upsert(
            IndexConstituent,
            rows,
            ["index_code", "ts_code", "trade_date"],
            batch_size=5_000,
        )
        self.session.execute(
            delete(BenchmarkRebalance)
            .where(BenchmarkRebalance.index_code == output_index_code)
            .where(BenchmarkRebalance.execution_date >= requested_start.date())
            .where(BenchmarkRebalance.execution_date <= requested_end.date())
        )
        rebalances = build.rebalances.loc[
            pd.to_datetime(build.rebalances["execution_date"]).between(requested_start, requested_end)
        ]
        self._upsert(
            BenchmarkRebalance,
            [{**record, "index_code": output_index_code} for record in rebalances.to_dict("records")],
            ["index_code", "execution_date"],
        )
        self.session.commit()
        if require_verified_nav:
            audit.attrs["nav"] = build.nav.loc[
                pd.to_datetime(build.nav["trade_date"]).between(requested_start, requested_end)
            ].copy()
        return weights, audit

    def build_free_float_market_cap_weights(
        self,
        index_code: str,
        start_date: str,
        end_date: str,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Materialise a research-only weight proxy from daily ``circ_mv`` data.

        The resulting source is deliberately not an official-license source and
        therefore remains excluded from certified index-enhancement research.
        """
        members = _frame(self.session, IndexMembershipSnapshot, start_date, end_date)
        members = members.loc[members["index_code"].eq(index_code)] if not members.empty else members
        # BaoStock has no direct free-float market value.  Its daily turnover
        # rate permits a transparent, lower-priority proxy when trading occurs:
        # circulating shares ≈ volume / (turn / 100), then value = close × shares.
        # Direct outstanding-share data appended below always supersedes it.
        valuation_rows: list[dict[str, object]] = []
        bars = _frame(self.session, SourceDailyBar, start_date, end_date)
        observations = _frame(self.session, DailyObservation, start_date, end_date)
        if not bars.empty and not observations.empty:
            baostock_bars = bars.loc[bars["source"].eq("baostock")][
                ["trade_date", "ts_code", "close", "volume"]
            ]
            turns = []
            for row in observations.loc[observations["dataset"].eq("baostock_daily")].to_dict("records"):
                turns.append({
                    "trade_date": row["trade_date"], "ts_code": row["ts_code"],
                    "turn": (row["payload"] or {}).get("turn"), "available_at": row["available_at"],
                })
            if turns:
                inferred = baostock_bars.merge(pd.DataFrame(turns), on=["trade_date", "ts_code"], how="inner")
                inferred["turn"] = pd.to_numeric(inferred["turn"], errors="coerce")
                inferred["circ_mv"] = (
                    pd.to_numeric(inferred["close"], errors="coerce")
                    * pd.to_numeric(inferred["volume"], errors="coerce") * 100 / inferred["turn"]
                )
                valuation_rows.extend({
                    "trade_date": row.trade_date, "ts_code": row.ts_code, "circ_mv": row.circ_mv,
                    "available_at": row.available_at, "source": "baostock_turnover_implied",
                    "methodology": "close_times_volume_div_turn_v1",
                } for row in inferred.loc[inferred["circ_mv"].gt(0)].itertuples(index=False))

        features = _frame(self.session, CleanDailyFeature, start_date, end_date)
        features = features.loc[features["dataset"].eq("valuation")] if not features.empty else features
        for row in features.to_dict("records"):
            payload = row["payload"] or {}
            valuation_rows.append({
                "trade_date": row["trade_date"],
                "ts_code": row["ts_code"],
                "circ_mv": payload.get("circ_mv"),
                "available_at": row["available_at"],
                "source": "tushare_daily_basic",
                "methodology": "vendor_circ_mv",
            })
        free_float = _frame(self.session, DailyObservation, start_date, end_date)
        if not free_float.empty:
            for row in free_float.loc[free_float["dataset"].eq("free_float_market_value")].to_dict("records"):
                payload = row["payload"] or {}
                valuation_rows.append({
                    "trade_date": row["trade_date"], "ts_code": row["ts_code"],
                    "circ_mv": payload.get("circ_mv"), "available_at": row["available_at"],
                    "source": payload.get("weight_source", "akshare_outstanding_share"),
                    "methodology": payload.get("weight_methodology", "close_times_outstanding_share_v1"),
                })
        build = free_float_market_cap_weights(members, pd.DataFrame(valuation_rows)) if valuation_rows else free_float_market_cap_weights(
            members,
            pd.DataFrame(columns=["trade_date", "ts_code", "circ_mv", "available_at"]),
        )
        run_id = str(uuid4())
        audit_rows = []
        for item in build.audit.to_dict("records"):
            if item["ready"]:
                continue
            audit_rows.append({
                "run_id": run_id,
                "severity": "ERROR",
                "category": "free_float_weight_coverage",
                "source": "free_float_market_cap",
                "ts_code": None,
                "trade_date": pd.Timestamp(item["trade_date"]).date(),
                "details": {
                    "member_count": int(item["member_count"]),
                    "valid_circ_mv_count": int(item["valid_circ_mv_count"]),
                    "reason": item["reason"],
                },
                "created_at": datetime.now(UTC),
            })
        if audit_rows:
            self.session.add_all(DataQualityIssue(**record) for record in audit_rows)
        if not build.weights.empty:
            self.session.execute(
                delete(IndexConstituent)
                .where(IndexConstituent.index_code == index_code)
                .where(IndexConstituent.trade_date >= pd.Timestamp(start_date).date())
                .where(IndexConstituent.trade_date <= pd.Timestamp(end_date).date())
            )
            now = datetime.now(UTC)
            rows = [{
                **record,
                "index_code": index_code,
                "available_at": record["trade_date"],
                "ingested_at": now,
            } for record in build.weights.to_dict("records")]
            self._upsert(IndexConstituent, rows, ["index_code", "ts_code", "trade_date"])
        self.session.commit()
        return build.weights, build.audit

    def reclassify_legacy_zero_volume_issues(self) -> int:
        """Align old clean runs with the conservative untradable-zero-volume policy."""
        result = self.session.execute(
            update(DataQualityIssue)
            .where(DataQualityIssue.category == "zero_volume_not_marked_suspended")
            .values(severity="INFO", category="untradable_zero_volume")
        )
        self.session.commit()
        return int(result.rowcount or 0)

    def mark_existing_zero_volume_untradable(self) -> int:
        """Backfill the current conservative trade-status flags without changing quotes."""
        result = self.session.execute(text("""
            UPDATE market.clean_daily_bar
            SET flags = (
                COALESCE(flags, '{}'::json)::jsonb ||
                '{"untradable": true, "untradable_reason": "zero_volume_and_amount"}'::jsonb
            )::json
            WHERE is_zero_volume IS TRUE
        """))
        self.session.commit()
        return int(result.rowcount or 0)

    def reclassify_non_trading_placeholders(self) -> int:
        """Correct legacy ERRORs caused by all-null vendor rows on non-trading days."""
        result = self.session.execute(text("""
            UPDATE ops.data_quality_issue issue
            SET severity = 'INFO',
                category = CASE issue.category
                    WHEN 'invalid_ohlc' THEN 'non_trading_placeholder'
                    WHEN 'no_valid_source' THEN 'untradable_missing_quote'
                    ELSE issue.category
                END
            WHERE issue.category IN ('invalid_ohlc', 'no_valid_source')
              AND EXISTS (
                  SELECT 1
                  FROM market.source_daily_bar bar
                  WHERE bar.ts_code = issue.ts_code
                    AND bar.trade_date = issue.trade_date
                    AND (bar.open IS NULL OR bar.open = 'NaN'::numeric)
                    AND (bar.high IS NULL OR bar.high = 'NaN'::numeric)
                    AND (bar.low IS NULL OR bar.low = 'NaN'::numeric)
                    AND (bar.close IS NULL OR bar.close = 'NaN'::numeric)
                    AND (bar.volume IS NULL OR bar.volume = 'NaN'::float8 OR bar.volume <= 0)
                    AND (bar.amount IS NULL OR bar.amount = 'NaN'::float8 OR bar.amount <= 0)
              )
        """))
        self.session.commit()
        return int(result.rowcount or 0)

    def _adjustment_factors(self, start_date: str | None, end_date: str | None) -> pd.DataFrame:
        observations = _frame(self.session, DailyObservation, start_date, end_date)
        if observations.empty:
            return pd.DataFrame()
        rows = []
        for record in observations.loc[observations["dataset"] == "adj_factor"].to_dict("records"):
            payload = record["payload"] or {}
            if "adj_factor" in payload:
                rows.append({"ts_code": record["ts_code"], "trade_date": record["trade_date"], "adj_factor": payload["adj_factor"]})
        return pd.DataFrame(rows)

    def _clean_daily_features(self, start_date: str | None, end_date: str | None, excluded_ts_codes: set[str]) -> None:
        observations = _frame(self.session, DailyObservation, start_date, end_date)
        if observations.empty:
            return
        observations = observations.loc[~observations["ts_code"].isin(excluded_ts_codes)]
        mapping = {"daily_basic": "valuation", "moneyflow": "moneyflow", "akshare_fund_flow": "moneyflow"}
        rows = []
        for record in observations.to_dict("records"):
            category = mapping.get(record["dataset"])
            if not category:
                continue
            rows.append({
                "dataset": category, "ts_code": record["ts_code"], "trade_date": record["trade_date"],
                "selected_source": record["source_revision"], "payload": record["payload"],
                "available_at": record["available_at"], "cleaned_at": datetime.now(UTC),
            })
        self._upsert(CleanDailyFeature, rows, ["dataset", "ts_code", "trade_date"])

    def _upsert(
        self,
        model: type[Any],
        rows: list[dict[str, Any]],
        keys: list[str],
        batch_size: int = 500,
    ) -> None:
        if not rows:
            return
        for start in range(0, len(rows), batch_size):
            statement = insert(model).values(rows[start:start + batch_size])
            updates = {
                column.name: getattr(statement.excluded, column.name)
                for column in model.__table__.columns
                if column.name not in keys and column.name != "id"
            }
            self.session.execute(statement.on_conflict_do_update(index_elements=keys, set_=updates))
