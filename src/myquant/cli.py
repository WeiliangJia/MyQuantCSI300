from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import typer
from sqlalchemy import select, text

from myquant.config import get_settings
from myquant.data.archive import RawArchiver
from myquant.data.free_adapters import to_ts_code
from myquant.data.free_ingestion import FreeDataIngestionService
from myquant.data.ingestion import TushareIngestionService
from myquant.data.official_history import (
    AutomatedCollectionPausedError,
    archive_official_history,
)
from myquant.data.tushare_client import TushareClient
from myquant.db import create_schemas, make_engine, make_session_factory
from myquant.models import IndexConstituent, IndexMembershipSnapshot
from myquant.processing.cleaning import replay_index_membership_events, reverse_index_membership
from myquant.processing.jobs import DataProcessingService
from myquant.research.certification import certified_ranges, membership_coverage
from myquant.universe_exclusions import load_excluded_ts_codes

app = typer.Typer(help="沪深300日频指数增强模拟盘")


@app.command("init-db")
def init_db() -> None:
    """Create PostgreSQL schemas and tables."""
    import myquant.models  # noqa: F401 -- registers declarative models

    engine = make_engine(get_settings())
    create_schemas(engine)
    typer.echo("Database schemas and tables are ready.")


@app.command("sync")
def sync(trade_date: str = typer.Argument(..., help="YYYYMMDD")) -> None:
    """Incrementally archive and load all daily Tushare inputs for one trade date."""
    settings = get_settings()
    engine = make_engine(settings)
    session = make_session_factory(engine)()
    try:
        service = TushareIngestionService(
            TushareClient(settings.tushare_token), RawArchiver(settings.raw_archive_path), session, settings.benchmark_code
        )
        checks = service.sync_trade_date(trade_date)
        typer.echo("; ".join(f"{item.name}={'PASS' if item.passed else 'FAIL'}" for item in checks))
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _ingestion_service() -> tuple[TushareIngestionService, object]:
    settings = get_settings()
    engine = make_engine(settings)
    session = make_session_factory(engine)()
    service = TushareIngestionService(
        TushareClient(settings.tushare_token), RawArchiver(settings.raw_archive_path), session, settings.benchmark_code
    )
    return service, session


def _free_ingestion_service() -> tuple[FreeDataIngestionService, object]:
    settings = get_settings()
    engine = make_engine(settings)
    session = make_session_factory(engine)()
    return FreeDataIngestionService(RawArchiver(settings.raw_archive_path), session), session


def _processing_service() -> tuple[DataProcessingService, object]:
    engine = make_engine(get_settings())
    session = make_session_factory(engine)()
    return DataProcessingService(session), session


@app.command("sync-reference")
def sync_reference(
    start_date: str = typer.Argument(..., help="YYYYMMDD"),
    end_date: str = typer.Argument(..., help="YYYYMMDD"),
) -> None:
    """Load security master, trading calendar and announced corporate actions."""
    service, session = _ingestion_service()
    try:
        service.sync_reference_data(start_date, end_date)
        typer.echo("Reference data synchronization completed.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("backfill-daily")
def backfill_daily(
    start_date: str = typer.Argument(..., help="YYYYMMDD"),
    end_date: str = typer.Argument(..., help="YYYYMMDD"),
) -> None:
    """Idempotently fill daily market data for every open SSE trading day in a range."""
    service, session = _ingestion_service()
    try:
        count = service.backfill_daily(start_date, end_date)
        typer.echo(f"Backfilled {count} trade dates.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("sync-financials")
def sync_financials(
    start_date: str = typer.Argument(..., help="YYYYMMDD announcement date"),
    end_date: str = typer.Argument(..., help="YYYYMMDD announcement date"),
) -> None:
    """Load financial observations using announcement dates as point-in-time availability."""
    service, session = _ingestion_service()
    try:
        service.sync_financials(start_date, end_date)
        typer.echo("Financial synchronization completed.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("free-sync-daily")
def free_sync_daily(
    provider: str = typer.Argument(..., help="baostock or akshare"),
    ts_code: str = typer.Argument(..., help="e.g. 600000.SH"),
    start_date: str = typer.Argument(..., help="YYYY-MM-DD or YYYYMMDD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD or YYYYMMDD"),
) -> None:
    """Load free daily data into the same market tables as Tushare."""
    service, session = _free_ingestion_service()
    try:
        count = service.sync_daily(provider.lower(), ts_code, start_date, end_date)
        typer.echo(f"Loaded {count} {provider} daily rows for {ts_code}.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("free-sync-circ-mv")
def free_sync_circ_mv(
    ts_code: str = typer.Argument(..., help="e.g. 600000.SH"),
    start_date: str = typer.Argument(..., help="YYYY-MM-DD or YYYYMMDD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD or YYYYMMDD"),
) -> None:
    """Fetch AKShare historical outstanding shares and derive a free-float value proxy."""
    service, session = _free_ingestion_service()
    try:
        count = service.sync_free_float_market_value(ts_code, start_date, end_date)
        typer.echo(f"Loaded {count} AKShare free-float value rows for {ts_code}.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("backfill-csi300-circ-mv")
def backfill_csi300_circ_mv(
    start_date: str = typer.Argument("2005-07-01", help="YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    limit: int | None = typer.Option(None, help="Limit symbols; use 20 for validation first"),
    offset: int = typer.Option(0, min=0, help="Zero-based official-universe offset"),
    request_spacing_seconds: float = typer.Option(1.0, min=0.0, help="Delay between AKShare requests"),
    retry_attempts: int = typer.Option(3, min=1, max=5, help="Attempts per symbol"),
    retry_delay_seconds: float = typer.Option(5.0, min=0.0, help="Linear retry backoff in seconds"),
    cooldown_after_failures: int = typer.Option(2, min=1, help="Failures before a provider cooldown"),
    cooldown_seconds: float = typer.Option(60.0, min=0.0, help="Cooldown duration in seconds"),
    request_timeout_seconds: float = typer.Option(30.0, min=1.0, help="Maximum duration of one upstream request"),
) -> None:
    """Backfill AKShare outstanding-share free-float values for official CSI 300 members."""
    service, session = _free_ingestion_service()
    try:
        result = service.backfill_akshare_free_float_membership_universe(
            start_date, end_date, limit=limit, offset=offset, progress=typer.echo,
            request_spacing_seconds=request_spacing_seconds, retry_attempts=retry_attempts,
            retry_delay_seconds=retry_delay_seconds, cooldown_after_failures=cooldown_after_failures,
            cooldown_seconds=cooldown_seconds, request_timeout_seconds=request_timeout_seconds,
        )
        typer.echo(
            f"codes={result.requested}, loaded_rows={result.loaded}, skipped_complete={result.skipped_complete}, "
            f"failed={','.join(result.failed) if result.failed else 'none'}"
        )
    finally:
        session.close()


@app.command("free-sync-financials")
def free_sync_financials(
    ts_code: str = typer.Argument(..., help="e.g. 600000.SH"),
    year: int = typer.Argument(...),
    quarter: int = typer.Argument(..., min=1, max=4),
) -> None:
    """Load BaoStock's free quarterly indicators using publication dates as availability."""
    service, session = _free_ingestion_service()
    try:
        count = service.sync_baostock_financials(ts_code, year, quarter)
        typer.echo(f"Loaded {count} BaoStock financial observations for {ts_code}.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("official-import-membership")
def official_import_membership(manifest: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Import a reviewed CSI 300 adjustment CSV and preserve its official source URL."""
    service, session = _free_ingestion_service()
    try:
        count = service.import_official_membership_manifest(manifest)
        typer.echo(f"Imported {count} official membership events.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("official-archive")
def official_archive(
    url: str = typer.Argument(..., help="Official CSI/SSE/SZSE announcement URL"),
    destination: Path = typer.Argument(..., help="Local path for the original PDF or HTML"),
) -> None:
    """Archive an official source document before manually reviewing its membership changes."""
    service, session = _free_ingestion_service()
    try:
        path = service.archive_official_document(url, destination)
        typer.echo(f"Archived official document: {path}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("official-discover-membership")
def official_discover_membership(
    start_year: int = typer.Option(2005, min=2005),
    end_year: int = typer.Option(datetime.now(UTC).year, min=2005),
    output_dir: Path = typer.Option(Path("data/official/csindex_announcements")),
    catalog: Path = typer.Option(
        Path("data/reference/official_membership/official_announcement_catalog.csv")
    ),
    download_attachments: bool = typer.Option(True, "--download/--no-download"),
) -> None:
    """PAUSED: discover and archive CSI 300 membership notices."""
    try:
        rows = archive_official_history(
            output_dir=output_dir,
            catalog_path=catalog,
            start_year=start_year,
            end_year=end_year,
            download_attachments=download_attachments,
            log=typer.echo,
        )
    except AutomatedCollectionPausedError as exc:
        typer.echo(f"[paused] {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Archived {len(rows)} official CSI 300 notice records.")


@app.command("import-sina-membership-history")
def import_sina_membership_history(index_code: str = typer.Option("000300.SH")) -> None:
    """Import public CSI 300 historical entry/exit intervals; records are explicitly non-official."""
    service, session = _free_ingestion_service()
    try:
        count = service.import_sina_historical_membership(index_code)
        typer.echo(f"Imported {count} Sina public historical membership intervals.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("import-current-csi300-membership")
def import_current_csi300_membership(
    as_of_date: str = typer.Argument(..., help="Snapshot date, YYYY-MM-DD"),
) -> None:
    """Import and archive a current AKShare CSI 300 snapshot (not historical membership)."""
    service, session = _free_ingestion_service()
    try:
        count = service.import_current_csi300_membership(as_of_date)
        typer.echo(f"Imported {count} current CSI 300 constituent records.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("backfill-csi300-baostock")
def backfill_csi300_baostock(
    start_date: str = typer.Argument("2005-04-08", help="YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="Most recent complete trade date, YYYY-MM-DD"),
    limit: int | None = typer.Option(None, help="Optional code limit for a small test run"),
    offset: int = typer.Option(0, min=0, help="Zero-based universe offset; use with limit for parallel shards"),
    exclusion_file: Path | None = typer.Option(
        None,
        help="Optional do-not-research CSV. Leave unset to preserve the complete benchmark universe.",
    ),
) -> None:
    """Resume BaoStock daily bars for every officially reconstructed CSI 300 member."""
    service, session = _free_ingestion_service()
    try:
        exclusions = load_excluded_ts_codes(exclusion_file) if exclusion_file else set()
        if exclusions:
            typer.echo(f"Excluding {len(exclusions)} explicitly requested research symbols.")
        result = service.backfill_baostock_membership_universe(
            start_date, end_date, limit=limit, offset=offset, progress=typer.echo,
            excluded_ts_codes=exclusions,
        )
        typer.echo(
            f"codes={result.requested}, loaded_rows={result.loaded}, skipped_complete={result.skipped_complete}, "
            f"failed={','.join(result.failed) if result.failed else 'none'}"
        )
    finally:
        session.close()


@app.command("backfill-akshare-fallback")
def backfill_akshare_fallback(
    targets: Path = typer.Argument(..., exists=True, readable=True, help="CSV with ts_code,end_date"),
    start_date: str = typer.Option("2005-04-08", help="Earliest requested price date"),
    retry_attempts: int = typer.Option(4, min=1, max=5, help="Attempts per code for transient provider errors"),
    retry_delay_seconds: float = typer.Option(15.0, min=0.0, help="Base delay for linear retry backoff"),
    request_spacing_seconds: float = typer.Option(15.0, min=0.0, help="Minimum wait between provider requests"),
    cooldown_after_failures: int = typer.Option(2, min=1, help="Consecutive failures before a provider cooldown"),
    cooldown_seconds: float = typer.Option(120.0, min=0.0, help="Provider cooldown duration after consecutive failures"),
) -> None:
    """Retry provider failures via AKShare; exited names end on their last membership date."""
    service, session = _free_ingestion_service()
    try:
        result = service.backfill_akshare_fallback(
            pd.read_csv(targets, dtype=str), start_date, progress=typer.echo,
            retry_attempts=retry_attempts, retry_delay_seconds=retry_delay_seconds,
            request_spacing_seconds=request_spacing_seconds,
            cooldown_after_failures=cooldown_after_failures, cooldown_seconds=cooldown_seconds,
        )
        typer.echo(
            f"codes={result.requested}, loaded_rows={result.loaded}, skipped_complete={result.skipped_complete}, "
            f"failed={','.join(result.failed) if result.failed else 'none'}"
        )
    finally:
        session.close()


@app.command("migrate-source-bars")
def migrate_source_bars() -> None:
    """Copy legacy canonical bars into the provider-preserving source layer once."""
    service, session = _processing_service()
    try:
        count = service.seed_source_layer_from_legacy_bars()
        typer.echo(f"Seeded {count} source-bar rows.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("clean-data")
def clean_data(
    start_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD"),
) -> None:
    """Build clean daily bars/features, select BaoStock first and log source discrepancies."""
    service, session = _processing_service()
    try:
        exclusions = load_excluded_ts_codes()
        typer.echo(f"Excluding {len(exclusions)} active research symbols.")
        run_id, bar_count, issue_count = service.clean_market_data(start_date, end_date, exclusions)
        typer.echo(f"run_id={run_id}, clean_bars={bar_count}, quality_issues={issue_count}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("clean-history")
def clean_history(
    start_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    with_financial_snapshots: bool = typer.Option(True, help="Build point-in-time financial snapshots after each year"),
) -> None:
    """Clean historical data year by year and emit an auditable progress log."""
    service, session = _processing_service()
    try:
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        periods = list(pd.period_range(start.to_period("Y"), end.to_period("Y"), freq="Y"))
        total_bars = total_issues = total_snapshots = 0
        for position, period in enumerate(periods, start=1):
            period_start = max(start, period.start_time).date().isoformat()
            period_end = min(end, period.end_time).date().isoformat()
            typer.echo(f"[{position}/{len(periods)}] cleaning {period_start} to {period_end}")
            exclusions = load_excluded_ts_codes()
            typer.echo(f"[{position}/{len(periods)}] excluding {len(exclusions)} active research symbols")
            run_id, bars, issues = service.clean_market_data(period_start, period_end, exclusions)
            total_bars += bars
            total_issues += issues
            typer.echo(f"[{position}/{len(periods)}] market clean complete: run_id={run_id}, bars={bars}, issues={issues}")
            if with_financial_snapshots:
                snapshots = service.build_financial_snapshots(period_start, period_end, exclusions)
                total_snapshots += snapshots
                typer.echo(f"[{position}/{len(periods)}] financial snapshots complete: rows={snapshots}")
        typer.echo(
            f"COMPLETE years={len(periods)}, clean_bars={total_bars}, quality_issues={total_issues}, "
            f"financial_snapshots={total_snapshots}"
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("build-financial-snapshots")
def build_financial_snapshots(
    start_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD"),
) -> None:
    """Materialize announcement-date-safe financial snapshots for clean trading dates."""
    service, session = _processing_service()
    try:
        count = service.build_financial_snapshots(start_date, end_date, load_excluded_ts_codes())
        typer.echo(f"Financial snapshots written: {count}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("build-free-float-weights")
def build_free_float_weights(
    start_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    index_code: str = typer.Option("000300.SH"),
) -> None:
    """Build research-only weights from same-day available circulating market value.

    These weights are explicitly modelled proxies, not official licensed index
    weights, and can never satisfy ``certified-range``.
    """
    service, session = _processing_service()
    try:
        weights, audit = service.build_free_float_market_cap_weights(index_code, start_date, end_date)
        ready = int(audit["ready"].sum()) if not audit.empty else 0
        blocked = int((~audit["ready"]).sum()) if not audit.empty else 0
        typer.echo(
            f"free_float_weights={len(weights)} ready_dates={ready} blocked_dates={blocked} "
            "source=free_float_market_cap methodology=circ_mv_pro_rata_v1"
        )
        if blocked:
            typer.echo(audit.loc[~audit["ready"], ["trade_date", "reason", "member_count", "valid_circ_mv_count"]].to_string(index=False))
            raise typer.Exit(code=2)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("build-equal-weight-benchmark")
def build_equal_weight_benchmark(
    start_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    membership_index_code: str = typer.Option("000300.SH"),
    output_index_code: str = typer.Option("MQ300.EW"),
    nav_output: Path | None = typer.Option(None, help="Write verified daily NAV and P&L as JSON"),
) -> None:
    """Build the monthly equal-weight simulated market with daily drift."""
    service, session = _processing_service()
    try:
        weights, audit = service.build_monthly_equal_weight_benchmark(
            membership_index_code, output_index_code, start_date, end_date,
            require_verified_nav=nav_output is not None,
        )
        if nav_output is not None:
            audit.attrs["nav"].to_json(nav_output, orient="records", date_format="iso")
        blocked = audit.loc[~audit["ready"]]
        typer.echo(
            f"equal_weight_rows={len(weights)} ready_dates={int(audit['ready'].sum())} "
            f"blocked_dates={len(blocked)} index_code={output_index_code} "
            "methodology=monthly_equal_weight_drift_v1"
        )
        if not blocked.empty:
            typer.echo(
                blocked[
                    [
                        "trade_date",
                        "reason",
                        "member_count",
                        "valid_price_count",
                        "missing_price_count",
                    ]
                ].to_string(index=False)
            )
            raise typer.Exit(code=2)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("rebuild-membership")
def rebuild_membership(
    baseline: Path = typer.Argument(..., exists=True, readable=True),
    start_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    end_date: str = typer.Argument(..., help="YYYY-MM-DD"),
    index_code: str = typer.Option("000300.SH"),
) -> None:
    """Reconstruct daily membership from a dated official 300-name baseline and official events."""
    service, session = _processing_service()
    try:
        count = service.rebuild_membership(
            baseline,
            index_code,
            start_date,
            end_date,
            load_excluded_ts_codes(),
            progress=typer.echo,
        )
        typer.echo(f"Index membership rows written: {count}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("rebuild-membership-backward")
def rebuild_membership_backward(
    current_baseline: Path = typer.Argument(..., exists=True, readable=True),
    events_manifest: Path = typer.Argument(..., exists=True, readable=True),
    target_date: str = typer.Argument(..., help="Target as-of date, YYYY-MM-DD"),
    output: Path = typer.Option(
        Path("data/reference/official_membership/csi300_baseline_rebuilt.csv"),
        help="Local-only output CSV",
    ),
    index_code: str = typer.Option("000300.SH"),
    expected_members: int = typer.Option(300, min=1),
    expected_baseline: Path | None = typer.Option(
        None,
        exists=True,
        readable=True,
        help="Independent official target-date baseline; require exact constituent-set match",
    ),
) -> None:
    """Reverse official events from a current baseline, then replay them for closure."""
    baseline = pd.read_csv(current_baseline, dtype={"ts_code": str})
    events = pd.read_csv(events_manifest, dtype={"ts_code": str, "index_code": str})
    if "as_of_date" not in baseline.columns:
        raise typer.BadParameter("current_baseline must contain as_of_date")
    as_of_dates = pd.to_datetime(baseline["as_of_date"], errors="raise").dt.normalize().unique()
    if len(as_of_dates) != 1:
        raise typer.BadParameter("current_baseline must contain exactly one as_of_date")
    current_date = pd.Timestamp(as_of_dates[0])
    typer.echo(
        f"[validate] current_date={current_date.date()} rows={len(baseline)} "
        f"unique_codes={baseline['ts_code'].nunique()} events={len(events)}"
    )
    rebuilt = reverse_index_membership(
        baseline,
        events,
        target_date=target_date,
        current_date=current_date,
        index_code=index_code,
        expected_members=expected_members,
        progress=typer.echo,
    )
    if expected_baseline is not None:
        expected = pd.read_csv(expected_baseline, dtype={"ts_code": str})
        if "ts_code" not in expected.columns:
            raise typer.BadParameter("expected_baseline must contain ts_code")
        expected_codes = set(expected["ts_code"].map(to_ts_code))
        rebuilt_codes = set(rebuilt["ts_code"])
        typer.echo(
            f"[verify] independent_baseline rows={len(expected)} "
            f"unique_codes={len(expected_codes)} intersection={len(expected_codes & rebuilt_codes)}"
        )
        if len(expected) != expected_members or len(expected_codes) != expected_members:
            raise RuntimeError(
                "Independent baseline must contain exactly "
                f"{expected_members} rows and unique members"
            )
        if expected_codes != rebuilt_codes:
            missing = sorted(expected_codes - rebuilt_codes)
            unexpected = sorted(rebuilt_codes - expected_codes)
            raise RuntimeError(
                f"Independent baseline mismatch: missing={missing}, unexpected={unexpected}"
            )
        typer.echo("[verify] independent_baseline_exact_match=True")
    typer.echo("[verify] replaying events forward to current baseline")
    replayed = replay_index_membership_events(
        rebuilt,
        events,
        baseline_date=target_date,
        end_date=current_date,
        index_code=index_code,
        expected_members=expected_members,
        progress=typer.echo,
    )
    expected_codes = set(baseline["ts_code"].map(to_ts_code))
    actual_codes = set(replayed["ts_code"])
    if expected_codes != actual_codes:
        missing = sorted(expected_codes - actual_codes)
        unexpected = sorted(actual_codes - expected_codes)
        raise RuntimeError(
            f"Forward closure failed: missing={missing}, unexpected={unexpected}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    rebuilt.to_csv(output, index=False)
    typer.echo(
        f"[complete] target_date={pd.Timestamp(target_date).date()} members={len(rebuilt)} "
        f"forward_exact_match=True output={output}"
    )


@app.command("quality-report")
def quality_report(run_id: str | None = typer.Option(None)) -> None:
    """Print validation and source-disagreement counts from the cleaning layer."""
    service, session = _processing_service()
    try:
        report = service.quality_summary(run_id)
        typer.echo(report.to_string(index=False) if not report.empty else "No data quality issues.")
    finally:
        session.close()


@app.command("data-audit")
def data_audit(index_code: str = typer.Option("000300.SH")) -> None:
    """Print compact research-readiness evidence from the local database."""
    engine = make_engine(get_settings())
    with engine.connect() as connection:
        tables = connection.execute(text("""
            SELECT 'clean_daily_bar' AS dataset, COUNT(*) AS rows,
                   COUNT(DISTINCT ts_code) AS securities, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
            FROM market.clean_daily_bar
            UNION ALL
            SELECT 'membership_snapshot', COUNT(*), COUNT(DISTINCT ts_code), MIN(trade_date), MAX(trade_date)
            FROM market.index_membership_snapshot WHERE index_code = :index_code
            UNION ALL
            SELECT 'valuation_feature', COUNT(*), COUNT(DISTINCT ts_code), MIN(trade_date), MAX(trade_date)
            FROM market.clean_daily_feature WHERE dataset = 'valuation'
            UNION ALL
            SELECT 'free_float_model_input', COUNT(*), COUNT(DISTINCT ts_code), MIN(trade_date), MAX(trade_date)
            FROM market.daily_observation WHERE dataset = 'free_float_market_value'
            UNION ALL
            SELECT 'financial_snapshot', COUNT(*), COUNT(DISTINCT ts_code), MIN(as_of_date), MAX(as_of_date)
            FROM research.financial_snapshot
            UNION ALL
            SELECT 'benchmark_weight', COUNT(*), COUNT(DISTINCT ts_code), MIN(trade_date), MAX(trade_date)
            FROM market.index_constituent WHERE index_code = :index_code
        """), {"index_code": index_code}).mappings().all()
        coverage = connection.execute(text("""
            WITH daily AS (
                SELECT m.trade_date, COUNT(*) AS members, COUNT(b.ts_code) AS bars
                FROM market.index_membership_snapshot m
                LEFT JOIN market.clean_daily_bar b ON b.ts_code = m.ts_code AND b.trade_date = m.trade_date
                WHERE m.index_code = :index_code
                GROUP BY m.trade_date
            )
            SELECT COUNT(*) AS days, MIN(bars) AS min_member_bars,
                   ROUND(AVG(bars), 1) AS mean_member_bars,
                   COUNT(*) FILTER (WHERE bars = members) AS fully_covered_days
            FROM daily
        """), {"index_code": index_code}).mappings().one()
        quality = connection.execute(text("""
            SELECT severity, category, COUNT(*) AS count
            FROM ops.data_quality_issue
            GROUP BY severity, category
            ORDER BY count DESC, severity, category
        """)).mappings().all()
    typer.echo("[coverage]")
    typer.echo(pd.DataFrame(tables).to_string(index=False))
    typer.echo("[membership-bar intersection]")
    typer.echo(pd.DataFrame([coverage]).to_string(index=False))
    typer.echo("[quality issues]")
    typer.echo(pd.DataFrame(quality).to_string(index=False) if quality else "No data quality issues.")


@app.command("free-float-gap-report")
def free_float_gap_report(
    output: Path = typer.Option(Path("data/reference/free_float_gap_targets.csv")),
    index_code: str = typer.Option("000300.SH"),
) -> None:
    """Export official constituents missing one or more direct free-float inputs."""
    engine = make_engine(get_settings())
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT m.ts_code,
                   MIN(m.trade_date) AS membership_start,
                   MAX(m.trade_date) AS membership_end,
                   COUNT(*) AS membership_days,
                   COUNT(v.ts_code) AS direct_input_days,
                   COUNT(*) - COUNT(v.ts_code) AS missing_input_days,
                   CASE WHEN COUNT(v.ts_code) = 0 THEN 'no_direct_input' ELSE 'partial_direct_input' END AS classification
            FROM market.index_membership_snapshot m
            LEFT JOIN market.daily_observation v
              ON v.dataset = 'free_float_market_value'
             AND v.ts_code = m.ts_code
             AND v.trade_date = m.trade_date
            WHERE m.index_code = :index_code
            GROUP BY m.ts_code
            HAVING COUNT(v.ts_code) < COUNT(*)
            ORDER BY missing_input_days DESC, m.ts_code
        """), {"index_code": index_code}).mappings().all()
    report = pd.DataFrame(rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output, index=False)
    typer.echo(f"free_float_gap_targets={len(report)} output={output}")


@app.command("certified-range")
def certified_range(
    index_code: str = typer.Option("000300.SH"),
    min_trading_days: int = typer.Option(60, min=1),
) -> None:
    """Show only date ranges safe for official point-in-time CSI 300 research.

    Certification requires exactly 300 official reconstructed constituents and
    300 positive benchmark weights whose sum is approximately one on every
    trading day.  It intentionally fails closed when the data evidence is
    incomplete.
    """
    engine = make_engine(get_settings())
    session = make_session_factory(engine)()
    try:
        member_rows = session.execute(
            select(IndexMembershipSnapshot).where(IndexMembershipSnapshot.index_code == index_code)
        ).scalars().all()
        weight_rows = session.execute(
            select(IndexConstituent).where(IndexConstituent.index_code == index_code)
        ).scalars().all()
        members = pd.DataFrame([
            {"trade_date": row.trade_date, "ts_code": row.ts_code, "is_member": row.is_member, "source": row.source}
            for row in member_rows
        ])
        weights = pd.DataFrame([
            {
                "trade_date": row.trade_date,
                "ts_code": row.ts_code,
                "weight": float(row.weight),
                "source": row.source,
            }
            for row in weight_rows
        ])
        coverage = membership_coverage(members, weights)
        ranges = certified_ranges(coverage, min_trading_days=min_trading_days)
        total = int(len(coverage))
        certified = int(coverage["certified"].sum()) if not coverage.empty else 0
        typer.echo(f"[1/2] membership/weight coverage dates={total}, certified_dates={certified}")
        if not ranges:
            blockers = coverage.loc[~coverage["certified"], "reason"].value_counts().to_dict() if not coverage.empty else {"no_membership_snapshots": 1}
            typer.echo(f"[2/2] BLOCKED: no certified range of at least {min_trading_days} trading days; blockers={blockers}")
            raise typer.Exit(code=2)
        for position, item in enumerate(ranges, start=1):
            typer.echo(f"[2/2] certified_range[{position}] {item.start_date.date()}..{item.end_date.date()} days={item.trading_days}")
    finally:
        session.close()


@app.command("reclassify-zero-volume")
def reclassify_zero_volume() -> None:
    """Reclassify legacy zero-volume warnings as conservative untradable states."""
    service, session = _processing_service()
    try:
        count = service.reclassify_legacy_zero_volume_issues()
        typer.echo(f"Reclassified {count} legacy zero-volume quality issues.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("mark-zero-volume-untradable")
def mark_zero_volume_untradable() -> None:
    """Backfill untradable flags for already-cleaned zero-volume rows."""
    service, session = _processing_service()
    try:
        count = service.mark_existing_zero_volume_untradable()
        typer.echo(f"Marked {count} cleaned zero-volume rows as untradable.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("reclassify-non-trading-placeholders")
def reclassify_non_trading_placeholders() -> None:
    """Convert all-null provider rows from false data errors to untradable information states."""
    service, session = _processing_service()
    try:
        count = service.reclassify_non_trading_placeholders()
        typer.echo(f"Reclassified {count} non-trading placeholder quality issues.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@app.command("quality-check")
def quality_check(trade_date: str = typer.Argument(..., help="YYYYMMDD")) -> None:
    """Show basic post-ingestion coverage counts for an as-of date."""
    engine = make_engine(get_settings())
    query = text(
        "SELECT COUNT(*) AS bars, COUNT(DISTINCT ts_code) AS securities "
        "FROM market.daily_bar WHERE trade_date = :trade_date"
    )
    with engine.connect() as connection:
        row = connection.execute(query, {"trade_date": pd.Timestamp(trade_date).date()}).mappings().one()
    if row["bars"] == 0:
        raise typer.BadParameter(f"No daily bars loaded for {trade_date}")
    typer.echo(f"daily_bars={row['bars']}, securities={row['securities']}")


@app.command("backup")
def backup(output_dir: Path = typer.Option(Path("data/backups"))) -> None:
    """Record a backup request; use pg_dump from the host scheduler for binary database backups."""
    output_dir.mkdir(parents=True, exist_ok=True)
    marker = output_dir / f"backup-request-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.txt"
    marker.write_text("Run pg_dump against DATABASE_URL from the host scheduler.\n", encoding="utf-8")
    typer.echo(f"Created backup marker: {marker}")


if __name__ == "__main__":
    app()
