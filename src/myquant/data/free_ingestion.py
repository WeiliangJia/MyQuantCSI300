from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
from pathlib import Path
import time
from typing import Any, Callable

import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from myquant.data.archive import RawArchiver
from myquant.data.free_adapters import AKShareClient, BaoStockClient, OfficialAnnouncementClient, SinaHistoricalConstituentClient
from myquant.data.quality import check_daily_bars, require_passing
from myquant.models import (
    DailyBar, DailyObservation, FinancialObservation, IndexMembershipEvent, IndexMembershipInterval,
    IndexMembershipSnapshot,
    RawArchive, SourceDailyBar,
)


@dataclass(frozen=True)
class BackfillResult:
    requested: int
    loaded: int
    skipped_complete: int
    failed: tuple[str, ...]


def _as_date(value: Any) -> object:
    return pd.Timestamp(str(value)).date()


def _safe(value: Any) -> Any:
    if pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value


class FreeDataIngestionService:
    """Loads free-provider data into the same point-in-time tables used by the strategy."""

    def __init__(self, archiver: RawArchiver, session: Session) -> None:
        self.archiver = archiver
        self.session = session

    def sync_daily(
        self, provider: str, ts_code: str, start_date: str, end_date: str,
        client: BaoStockClient | AKShareClient | None = None,
    ) -> int:
        if provider == "baostock":
            frame = (client or BaoStockClient()).daily_bars(ts_code, start_date, end_date)  # type: ignore[union-attr]
        elif provider == "akshare":
            frame = (client or AKShareClient()).daily_bars(ts_code, start_date, end_date)  # type: ignore[union-attr]
        else:
            raise ValueError("provider must be 'baostock' or 'akshare'")
        self.load_daily(provider, frame, {"ts_code": ts_code, "start_date": start_date, "end_date": end_date})
        return len(frame)

    def sync_free_float_market_value(
        self,
        ts_code: str,
        start_date: str,
        end_date: str,
        client: AKShareClient | None = None,
    ) -> int:
        """Archive AKShare's historical outstanding-share free-float proxy."""
        frame = (client or AKShareClient()).daily_free_float_market_value(ts_code, start_date, end_date)
        self.load_free_float_market_value(
            frame,
            {"ts_code": ts_code, "start_date": start_date, "end_date": end_date},
        )
        return len(frame)

    def load_free_float_market_value(self, frame: pd.DataFrame, request_params: dict[str, str]) -> None:
        required = {"trade_date", "ts_code", "close", "outstanding_share", "circ_mv"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"Free-float market-value data missing columns: {sorted(missing)}")
        valid = frame.copy()
        valid["circ_mv"] = pd.to_numeric(valid["circ_mv"], errors="coerce")
        valid = valid.loc[valid["circ_mv"].gt(0)]
        path, digest = self.archiver.archive("free_float_market_value", request_params, valid, source="akshare")
        now = datetime.now(UTC)
        self._upsert(RawArchive, [{
            "source": "akshare", "endpoint": "free_float_market_value", "request_params": request_params,
            "archive_path": str(path), "content_sha256": digest, "source_revision": "akshare_sina_daily",
            "ingested_at": now,
        }], ["archive_path"])
        rows = [{
            "dataset": "free_float_market_value", "ts_code": item["ts_code"],
            "trade_date": _as_date(item["trade_date"]),
            "payload": {
                "circ_mv": _safe(item["circ_mv"]),
                "outstanding_share": _safe(item["outstanding_share"]),
                "close": _safe(item["close"]),
                "weight_source": "akshare_outstanding_share",
                "weight_methodology": "close_times_outstanding_share_v1",
            },
            "available_at": _as_date(item["trade_date"]), "source_revision": "akshare_sina_daily",
            "ingested_at": now,
        } for item in valid.to_dict("records")]
        self._upsert(DailyObservation, rows, ["dataset", "ts_code", "trade_date"])
        self.session.commit()

    def load_daily(self, source: str, frame: pd.DataFrame, request_params: dict[str, str]) -> None:
        require_passing(check_daily_bars(frame))
        path, digest = self.archiver.archive("daily", request_params, frame, source=source)
        now = datetime.now(UTC)
        self._upsert(RawArchive, [{
            "source": source, "endpoint": "daily", "request_params": request_params, "archive_path": str(path),
            "content_sha256": digest, "source_revision": "free", "ingested_at": now,
        }], ["archive_path"])
        bars = []
        extras = []
        for row in frame.to_dict("records"):
            day = _as_date(row["trade_date"])
            bars.append({
                "ts_code": row["ts_code"], "trade_date": day, "open": row["open"], "high": row["high"],
                "low": row["low"], "close": row["close"], "pre_close": row.get("pre_close"),
                "volume": row.get("volume"), "amount": row.get("amount"), "available_at": day,
                "source_revision": source, "ingested_at": now,
            })
            payload = {key: _safe(value) for key, value in row.items() if key not in {"ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "volume", "amount"}}
            if payload:
                extras.append({
                    "dataset": f"{source}_daily", "ts_code": row["ts_code"], "trade_date": day, "payload": payload,
                    "available_at": day, "source_revision": source, "ingested_at": now,
                })
        self._upsert(DailyBar, bars, ["ts_code", "trade_date"])
        source_bars = [{**bar, "source": source} for bar in bars]
        self._upsert(SourceDailyBar, source_bars, ["source", "ts_code", "trade_date"])
        self._upsert(DailyObservation, extras, ["dataset", "ts_code", "trade_date"])
        self.session.commit()

    def sync_baostock_financials(self, ts_code: str, year: int, quarter: int, client: BaoStockClient | None = None) -> int:
        frame = (client or BaoStockClient()).financial_indicators(ts_code, year, quarter)
        if frame.empty:
            return 0
        path, digest = self.archiver.archive(
            "quarterly_financials", {"ts_code": ts_code, "year": str(year), "quarter": str(quarter)}, frame, source="baostock"
        )
        now = datetime.now(UTC)
        self._upsert(RawArchive, [{
            "source": "baostock", "endpoint": "quarterly_financials", "request_params": {"ts_code": ts_code, "year": str(year), "quarter": str(quarter)},
            "archive_path": str(path), "content_sha256": digest, "source_revision": "free", "ingested_at": now,
        }], ["archive_path"])
        aliases = {"roeAvg": "roe", "gpMargin": "grossprofit_margin", "CAToAsset": "cashflow_to_assets"}
        rows = []
        for item in frame.to_dict("records"):
            announcement = item.get("pubDate")
            end_date = item.get("statDate")
            if not announcement or not end_date:
                continue
            for field, metric in aliases.items():
                value = pd.to_numeric(pd.Series([item.get(field)]), errors="coerce").iloc[0]
                if pd.notna(value):
                    rows.append({
                        "ts_code": item["ts_code"], "metric": metric, "value": float(value),
                        "end_date": _as_date(end_date), "announcement_date": _as_date(announcement),
                        "available_at": _as_date(announcement), "source_revision": "baostock", "ingested_at": now,
                    })
        self._upsert(FinancialObservation, rows, ["ts_code", "metric", "end_date", "announcement_date"])
        self.session.commit()
        return len(rows)

    def import_official_membership_manifest(self, path: Path) -> int:
        events = OfficialAnnouncementClient.read_manifest(path)
        frame = pd.read_csv(path)
        archive_path, digest = self.archiver.archive("csi300_membership_manifest", {"path": str(path)}, frame, source="official")
        now = datetime.now(UTC)
        self._upsert(RawArchive, [{
            "source": "official", "endpoint": "csi300_membership_manifest", "request_params": {"path": str(path)},
            "archive_path": str(archive_path), "content_sha256": digest, "source_revision": "reviewed_manifest", "ingested_at": now,
        }], ["archive_path"])
        rows = [{
            "index_code": event.index_code, "ts_code": event.ts_code, "action": event.action,
            "effective_date": _as_date(event.effective_date), "announcement_date": _as_date(event.announcement_date),
            "source_url": event.source_url, "available_at": _as_date(event.announcement_date), "ingested_at": now,
        } for event in events]
        self._upsert(IndexMembershipEvent, rows, ["index_code", "ts_code", "effective_date", "action"])
        self.session.commit()
        return len(rows)

    def archive_official_document(self, url: str, destination: Path) -> Path:
        archived = OfficialAnnouncementClient().archive_url(url, destination)
        digest = hashlib.sha256(archived.read_bytes()).hexdigest()
        self._upsert(RawArchive, [{
            "source": "official", "endpoint": "index_adjustment_announcement", "request_params": {"url": url},
            "archive_path": str(archived), "content_sha256": digest, "source_revision": "official_document",
            "ingested_at": datetime.now(UTC),
        }], ["archive_path"])
        self.session.commit()
        return archived

    def import_sina_historical_membership(self, index_code: str = "000300.SH", client: SinaHistoricalConstituentClient | None = None) -> int:
        """Import public entry/exit intervals; source is labelled non-official for auditability."""
        history = (client or SinaHistoricalConstituentClient()).history(index_code.split(".")[0])
        path, digest = self.archiver.archive(
            "csi300_historical_components", {"index_code": index_code}, history, source="sina"
        )
        now = datetime.now(UTC)
        self._upsert(RawArchive, [{
            "source": "sina", "endpoint": "csi300_historical_components", "request_params": {"index_code": index_code},
            "archive_path": str(path), "content_sha256": digest, "source_revision": "public_history", "ingested_at": now,
        }], ["archive_path"])
        rows = [{
            "index_code": index_code, "ts_code": item["ts_code"], "name": item["name"],
            "entry_date": _as_date(item["entry_date"]), "exit_date": _as_date(item["exit_date"]) if pd.notna(item["exit_date"]) else None,
            "source": "sina_public_history", "source_url": "https://money.finance.sina.com.cn/corp/go.php/vII_HistoryComponent/indexid/000300.phtml",
            # Entry date is deliberately conservative: no historical member is available earlier than its effective date.
            "available_at": _as_date(item["entry_date"]), "ingested_at": now,
        } for item in history.to_dict("records")]
        self._upsert(IndexMembershipInterval, rows, ["index_code", "ts_code", "entry_date", "source"])
        self.session.commit()
        return len(rows)

    def import_current_csi300_membership(self, as_of_date: str, client: AKShareClient | None = None) -> int:
        """Archive an AKShare current snapshot without pretending it is historical data."""
        current = (client or AKShareClient()).csi300_constituents()
        path, digest = self.archiver.archive(
            "csi300_current_components", {"as_of_date": as_of_date}, current, source="akshare"
        )
        now = datetime.now(UTC)
        as_of = _as_date(as_of_date)
        self._upsert(RawArchive, [{
            "source": "akshare", "endpoint": "csi300_current_components", "request_params": {"as_of_date": as_of_date},
            "archive_path": str(path), "content_sha256": digest, "source_revision": "current_snapshot", "ingested_at": now,
        }], ["archive_path"])
        rows = [{
            "index_code": "000300.SH", "ts_code": item["ts_code"], "name": item["name"],
            "entry_date": as_of, "exit_date": None, "source": "akshare_current_snapshot",
            "source_url": "https://www.csindex.com.cn/#/indices/family/detail?indexCode=000300",
            "available_at": as_of, "ingested_at": now,
        } for item in current.to_dict("records")]
        self._upsert(IndexMembershipInterval, rows, ["index_code", "ts_code", "entry_date", "source"])
        self.session.commit()
        return len(rows)

    def backfill_baostock_membership_universe(
        self, start_date: str, end_date: str, index_code: str = "000300.SH", limit: int | None = None,
        offset: int = 0, progress: Callable[[str], None] | None = None,
        excluded_ts_codes: set[str] | None = None,
    ) -> BackfillResult:
        """Resume bars for the certified point-in-time membership universe.

        The former implementation read ``IndexMembershipInterval``, which is
        populated from incomplete public-history sources and omitted 445 of the
        941 symbols in the reconstructed official snapshot history.  The daily
        snapshot is the authoritative local universe; each request is bounded
        to that symbol's actual membership interval.
        """
        statement = (
            select(
                IndexMembershipSnapshot.ts_code,
                func.min(IndexMembershipSnapshot.trade_date),
                func.max(IndexMembershipSnapshot.trade_date),
            )
            .where(IndexMembershipSnapshot.index_code == index_code)
            .where(IndexMembershipSnapshot.is_member.is_(True))
            .group_by(IndexMembershipSnapshot.ts_code)
        )
        exclusions = excluded_ts_codes or set()
        membership_bounds = {
            code: (first_date, last_date)
            for code, first_date, last_date in self.session.execute(statement).all()
            if code not in exclusions
        }
        codes = sorted(membership_bounds)[offset:]
        if limit is not None:
            codes = codes[:limit]
        if not codes:
            raise ValueError("No membership snapshots available; rebuild official membership first")
        bounds = {
            code: (first_date, last_date)
            for code, first_date, last_date in self.session.execute(
                select(SourceDailyBar.ts_code, func.min(SourceDailyBar.trade_date), func.max(SourceDailyBar.trade_date))
                .where(SourceDailyBar.source == "baostock", SourceDailyBar.ts_code.in_(codes))
                .group_by(SourceDailyBar.ts_code)
            ).all()
        }
        target_start, target_end = _as_date(start_date), _as_date(end_date)
        loaded = skipped = 0
        failed: list[str] = []
        client = BaoStockClient()
        for position, code in enumerate(codes, start=1):
            member_start, member_end = membership_bounds[code]
            requested_start = max(target_start, member_start)
            requested_end = min(target_end, member_end)
            if requested_start > requested_end:
                skipped += 1
                continue
            existing = bounds.get(code)
            if existing and existing[0] <= requested_start and existing[1] >= requested_end:
                skipped += 1
                if progress:
                    progress(f"[{position}/{len(codes)}] {code}: skip (already complete)")
                continue
            try:
                row_count = self.sync_daily(
                    "baostock", code, str(requested_start), str(requested_end), client
                )
                loaded += row_count
                if progress:
                    progress(f"[{position}/{len(codes)}] {code}: loaded {row_count} rows")
            except Exception as error:
                self.session.rollback()
                failed.append(f"{code}:{type(error).__name__}")
                if progress:
                    progress(f"[{position}/{len(codes)}] {code}: FAILED ({type(error).__name__})")
        return BackfillResult(len(codes), loaded, skipped, tuple(failed))

    def backfill_akshare_free_float_membership_universe(
        self,
        start_date: str,
        end_date: str,
        index_code: str = "000300.SH",
        limit: int | None = None,
        offset: int = 0,
        progress: Callable[[str], None] | None = None,
        request_spacing_seconds: float = 1.0,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 5.0,
        cooldown_after_failures: int = 2,
        cooldown_seconds: float = 60.0,
        request_timeout_seconds: float = 30.0,
    ) -> BackfillResult:
        """Resume direct AKShare outstanding-share data for official members.

        This is deliberately separate from daily-bar fallback: it stores the
        model input and preserves its source-specific audit trail.
        """
        statement = (
            select(
                IndexMembershipSnapshot.ts_code,
                func.min(IndexMembershipSnapshot.trade_date),
                func.max(IndexMembershipSnapshot.trade_date),
            )
            .where(IndexMembershipSnapshot.index_code == index_code)
            .where(IndexMembershipSnapshot.is_member.is_(True))
            .group_by(IndexMembershipSnapshot.ts_code)
        )
        membership_bounds = {code: (first, last) for code, first, last in self.session.execute(statement).all()}
        codes = sorted(membership_bounds)[offset:]
        if limit is not None:
            codes = codes[:limit]
        if not codes:
            raise ValueError("No membership snapshots available; rebuild official membership first")
        existing = {
            code: (first_date, last_date)
            for code, first_date, last_date in self.session.execute(
                select(DailyObservation.ts_code, func.min(DailyObservation.trade_date), func.max(DailyObservation.trade_date))
                .where(DailyObservation.dataset == "free_float_market_value", DailyObservation.ts_code.in_(codes))
                .group_by(DailyObservation.ts_code)
            ).all()
        }
        target_start, target_end = _as_date(start_date), _as_date(end_date)
        loaded = skipped = 0
        failed: list[str] = []
        consecutive_failures = 0
        client = AKShareClient()
        for position, code in enumerate(codes, start=1):
            member_start, member_end = membership_bounds[code]
            requested_start, requested_end = max(target_start, member_start), min(target_end, member_end)
            if requested_start > requested_end:
                skipped += 1
                continue
            covered = existing.get(code)
            if covered and covered[0] <= requested_start and covered[1] >= requested_end:
                skipped += 1
                if progress:
                    progress(f"[{position}/{len(codes)}] {code}: skip (already complete)")
                continue
            if consecutive_failures >= cooldown_after_failures:
                if progress:
                    progress(
                        f"[{position}/{len(codes)}] circuit-breaker: {consecutive_failures} consecutive failures; "
                        f"cooling down {cooldown_seconds:.0f}s"
                    )
                time.sleep(cooldown_seconds)
                consecutive_failures = 0
            for attempt in range(1, retry_attempts + 1):
                try:
                    frame = client.daily_free_float_market_value(
                        code, str(requested_start), str(requested_end), request_timeout_seconds
                    )
                    self.load_free_float_market_value(
                        frame, {"ts_code": code, "start_date": str(requested_start), "end_date": str(requested_end)}
                    )
                    rows = len(frame)
                    loaded += rows
                    consecutive_failures = 0
                    if progress:
                        progress(f"[{position}/{len(codes)}] {code}: loaded {rows} free-float rows")
                    break
                except Exception as error:
                    self.session.rollback()
                    if attempt == retry_attempts:
                        failed.append(f"{code}:{type(error).__name__}")
                        consecutive_failures += 1
                        if progress:
                            progress(
                                f"[{position}/{len(codes)}] {code}: FAILED after {attempt} attempts "
                                f"({type(error).__name__})"
                            )
                        break
                    wait_seconds = retry_delay_seconds * attempt
                    if progress:
                        progress(
                            f"[{position}/{len(codes)}] {code}: {type(error).__name__}; "
                            f"retry {attempt + 1}/{retry_attempts} in {wait_seconds:.0f}s"
                        )
                    time.sleep(wait_seconds)
            if position < len(codes) and request_spacing_seconds:
                time.sleep(request_spacing_seconds)
        return BackfillResult(len(codes), loaded, skipped, tuple(failed))

    def backfill_akshare_fallback(
        self, targets: pd.DataFrame, start_date: str = "2005-04-08",
        progress: Callable[[str], None] | None = None, retry_attempts: int = 3,
        retry_delay_seconds: float = 15.0, request_spacing_seconds: float = 15.0,
        cooldown_after_failures: int = 2, cooldown_seconds: float = 120.0,
    ) -> BackfillResult:
        """Fill provider failures with AKShare while preserving source provenance.

        `end_date` is the last date required by the stock's point-in-time
        membership interval.  Exited names are therefore not incorrectly
        treated as missing merely because they have no quote today.
        """
        required = {"ts_code", "end_date"}
        missing = required.difference(targets.columns)
        if missing:
            raise ValueError(f"Fallback targets missing columns: {sorted(missing)}")
        client = AKShareClient()
        loaded = skipped = 0
        failed: list[str] = []
        consecutive_failures = 0
        target_rows = targets.to_dict("records")
        for position, item in enumerate(target_rows, start=1):
            code, target_end = item["ts_code"], _as_date(item["end_date"])
            if consecutive_failures >= cooldown_after_failures:
                if progress:
                    progress(
                        f"[{position}/{len(target_rows)}] circuit-breaker: {consecutive_failures} consecutive failures; "
                        f"cooling down {cooldown_seconds:.0f}s"
                    )
                time.sleep(cooldown_seconds)
                consecutive_failures = 0
            existing_last = self.session.execute(
                select(func.max(SourceDailyBar.trade_date)).where(SourceDailyBar.ts_code == code)
            ).scalar_one()
            # A prior provider has already supplied the required final date.
            if existing_last and existing_last >= target_end:
                skipped += 1
                if progress:
                    progress(f"[{position}/{len(target_rows)}] {code}: skip (covered through {existing_last})")
                continue
            for attempt in range(1, retry_attempts + 1):
                try:
                    row_count = self.sync_daily("akshare", code, start_date, str(target_end), client)
                    loaded += row_count
                    if progress:
                        progress(f"[{position}/{len(target_rows)}] {code}: loaded {row_count} rows through {target_end}")
                    consecutive_failures = 0
                    break
                except Exception as error:
                    self.session.rollback()
                    detail = " ".join(str(error).split())[:220] or "no provider detail"
                    if attempt == retry_attempts:
                        failed.append(f"{code}:{type(error).__name__}")
                        consecutive_failures += 1
                        if progress:
                            progress(
                                f"[{position}/{len(target_rows)}] {code}: FAILED after {attempt} attempts "
                                f"({type(error).__name__}: {detail})"
                            )
                        break
                    wait_seconds = retry_delay_seconds * attempt
                    if progress:
                        progress(
                            f"[{position}/{len(target_rows)}] {code}: {type(error).__name__}: {detail}; "
                            f"retry {attempt + 1}/{retry_attempts} in {wait_seconds:.0f}s"
                        )
                    time.sleep(wait_seconds)
            if position < len(target_rows) and request_spacing_seconds:
                if progress:
                    progress(f"[{position}/{len(target_rows)}] pacing: wait {request_spacing_seconds:.0f}s before next request")
                time.sleep(request_spacing_seconds)
        return BackfillResult(len(targets), loaded, skipped, tuple(failed))

    def _upsert(self, model: type[Any], rows: list[dict[str, Any]], keys: list[str]) -> None:
        if not rows:
            return
        # A 20-year BaoStock response has more rows than PostgreSQL accepts in
        # one parameterised INSERT.  Small, deterministic chunks also make
        # individual-symbol loading restartable after a network interruption.
        # 3,000 source-bar rows remain below PostgreSQL's bind-parameter
        # ceiling while substantially reducing round trips for a 20-year
        # history response.
        for start in range(0, len(rows), 3000):
            statement = insert(model).values(rows[start:start + 3000])
            updates = {
                column.name: getattr(statement.excluded, column.name)
                for column in model.__table__.columns
                if column.name not in keys and column.name != "id"
            }
            self.session.execute(statement.on_conflict_do_update(index_elements=keys, set_=updates))
