from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from myquant.data.archive import RawArchiver
from myquant.data.quality import CheckResult, check_daily_bars, check_index_weights, require_passing
from myquant.data.tushare_client import TushareClient
from myquant.models import (
    CorporateAction, DailyBar, DailyObservation, FinancialObservation, IndexConstituent,
    Instrument, RawArchive, SecurityStatus, SourceDailyBar, TradeCalendar,
)


def _date(value: str | int | date) -> date:
    return pd.Timestamp(str(value)).date()


def _json_safe(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (None if pd.isna(value) else value.item() if hasattr(value, "item") else value)
        for key, value in record.items()
    }


class TushareIngestionService:
    """Archives every response before idempotently loading normalized market tables."""

    def __init__(self, client: TushareClient, archiver: RawArchiver, session: Session, benchmark: str) -> None:
        self.client = client
        self.archiver = archiver
        self.session = session
        self.benchmark = benchmark

    def sync_trade_date(self, trade_date: str) -> list[CheckResult]:
        bundle = self.client.sync_bundle(trade_date, self.benchmark)
        for endpoint, frame in bundle.items():
            path, digest = self.archiver.archive(endpoint, {"trade_date": trade_date}, frame)
            self._upsert(
                RawArchive,
                [{
                    "source": "tushare", "endpoint": endpoint, "request_params": {"trade_date": trade_date},
                    "archive_path": str(path), "content_sha256": digest, "source_revision": "api",
                    "ingested_at": datetime.now(UTC),
                }],
                ["archive_path"],
            )
        checks = [*check_daily_bars(bundle["daily"]), check_index_weights(bundle["index_weight"])]
        require_passing(checks)
        self._load_daily(bundle["daily"])
        self._load_index_weights(bundle["index_weight"], trade_date)
        self._load_security_status(bundle["suspend_d"], trade_date)
        for name in ("daily_basic", "moneyflow", "stk_limit", "suspend_d", "adj_factor", "index_daily"):
            self._load_observation(name, bundle[name], trade_date)
        self.session.commit()
        return checks

    def sync_reference_data(self, start_date: str, end_date: str) -> None:
        bundle = self.client.reference_bundle(start_date, end_date)
        self._archive_bundle(bundle, {"start_date": start_date, "end_date": end_date})
        now = datetime.now(UTC)
        instruments = []
        for item in bundle["stock_basic"].to_dict("records"):
            instruments.append({
                "ts_code": item["ts_code"], "symbol": item.get("symbol"), "name": item.get("name"),
                "exchange": item.get("exchange"), "industry": item.get("industry"),
                "list_date": _date(item["list_date"]) if item.get("list_date") else None,
                "delist_date": _date(item["delist_date"]) if item.get("delist_date") else None,
                "available_at": _date(end_date), "ingested_at": now,
            })
        self._upsert(Instrument, instruments, ["ts_code"])
        calendar = []
        for item in bundle["trade_cal"].to_dict("records"):
            calendar.append({
                "exchange": item.get("exchange", "SSE"), "trade_date": _date(item["cal_date"]),
                "is_open": bool(int(item["is_open"])),
                "previous_trade_date": _date(item["pretrade_date"]) if item.get("pretrade_date") else None,
                "ingested_at": now,
            })
        self._upsert(TradeCalendar, calendar, ["exchange", "trade_date"])
        actions = []
        for item in bundle["dividend"].to_dict("records"):
            if not item.get("ann_date"):
                continue
            actions.append({
                "ts_code": item["ts_code"], "action_type": "dividend", "record_date": _date(item["record_date"]) if item.get("record_date") else None,
                "ex_date": _date(item["ex_date"]) if item.get("ex_date") else None,
                "announcement_date": _date(item["ann_date"]), "payload": _json_safe(item),
                "available_at": _date(item["ann_date"]), "ingested_at": now,
            })
        self._upsert(CorporateAction, actions, ["ts_code", "action_type", "record_date", "announcement_date"])
        self.session.commit()

    def sync_financials(self, start_date: str, end_date: str) -> None:
        bundle = self.client.financial_bundle(start_date, end_date)
        self._archive_bundle(bundle, {"start_date": start_date, "end_date": end_date})
        metrics = {"roe", "roa", "grossprofit_margin", "debt_to_assets", "ocf_to_or", "ocf_to_opincome", "ocf_to_assets"}
        rows = []
        now = datetime.now(UTC)
        for frame in bundle.values():
            for item in frame.to_dict("records"):
                if not item.get("ann_date") or not item.get("end_date"):
                    continue
                for metric in metrics.intersection(item):
                    if pd.isna(item[metric]):
                        continue
                    normalized_metric = "cashflow_to_assets" if metric == "ocf_to_assets" else metric
                    rows.append({
                        "ts_code": item["ts_code"], "metric": normalized_metric, "value": float(item[metric]),
                        "end_date": _date(item["end_date"]), "announcement_date": _date(item["ann_date"]),
                        "available_at": _date(item["ann_date"]), "source_revision": "api", "ingested_at": now,
                    })
        self._upsert(FinancialObservation, rows, ["ts_code", "metric", "end_date", "announcement_date"])
        self.session.commit()

    def backfill_daily(self, start_date: str, end_date: str) -> int:
        """Run idempotent daily syncs for every open Shanghai trading day in a date range."""
        calendar = self.client.fetch("trade_cal", exchange="SSE", start_date=start_date, end_date=end_date)
        open_days = calendar.loc[calendar["is_open"].astype(int) == 1, "cal_date"].sort_values()
        for trade_date in open_days.astype(str):
            self.sync_trade_date(trade_date)
        return int(len(open_days))

    def _archive_bundle(self, bundle: dict[str, pd.DataFrame], params: dict[str, str]) -> None:
        for endpoint, frame in bundle.items():
            path, digest = self.archiver.archive(endpoint, params, frame)
            self._upsert(
                RawArchive,
                [{"source": "tushare", "endpoint": endpoint, "request_params": params, "archive_path": str(path),
                  "content_sha256": digest, "source_revision": "api", "ingested_at": datetime.now(UTC)}],
                ["archive_path"],
            )

    def _load_daily(self, frame: pd.DataFrame) -> None:
        rows = []
        for record in frame.to_dict("records"):
            trade_day = _date(record["trade_date"])
            volume = record.get("vol")
            rows.append({
                "ts_code": record["ts_code"], "trade_date": trade_day,
                "open": record["open"], "high": record["high"], "low": record["low"], "close": record["close"],
                # Tushare daily `vol` is in lots; internal execution quantities are shares.
                "pre_close": record.get("pre_close"), "volume": 0 if pd.isna(volume) else volume * 100, "amount": record.get("amount"),
                "available_at": trade_day, "source_revision": "api", "ingested_at": datetime.now(UTC),
            })
        self._upsert(DailyBar, rows, ["ts_code", "trade_date"])
        source_rows = [{**row, "source": "tushare"} for row in rows]
        self._upsert(SourceDailyBar, source_rows, ["source", "ts_code", "trade_date"])

    def _load_index_weights(self, frame: pd.DataFrame, trade_date: str) -> None:
        day = _date(trade_date)
        rows = [
            {
                "index_code": self.benchmark, "ts_code": record["con_code"], "trade_date": day,
                "weight": float(record["weight"]) / 100.0, "available_at": day,
                "source": "tushare", "methodology": "published_vendor_weight",
                "ingested_at": datetime.now(UTC),
            }
            for record in frame.to_dict("records")
        ]
        self._upsert(IndexConstituent, rows, ["index_code", "ts_code", "trade_date"])

    def _load_security_status(self, frame: pd.DataFrame, trade_date: str) -> None:
        """Persist explicit same-day suspension evidence for the execution layer."""
        if frame.empty or "ts_code" not in frame:
            return
        day = _date(trade_date)
        now = datetime.now(UTC)
        rows = [{
            "ts_code": record["ts_code"], "trade_date": day, "name": record.get("name"),
            "is_st": False, "is_suspended": True, "is_delisted": False,
            "available_at": day, "ingested_at": now,
        } for record in frame.to_dict("records")]
        self._upsert(SecurityStatus, rows, ["ts_code", "trade_date"])

    def _load_observation(self, dataset: str, frame: pd.DataFrame, trade_date: str) -> None:
        if frame.empty or "ts_code" not in frame.columns:
            return
        day = _date(trade_date)
        rows = [
            {
                "dataset": dataset, "ts_code": record["ts_code"], "trade_date": day,
                "payload": _json_safe(record), "available_at": day, "source_revision": "api",
                "ingested_at": datetime.now(UTC),
            }
            for record in frame.to_dict("records")
        ]
        self._upsert(DailyObservation, rows, ["dataset", "ts_code", "trade_date"])

    def _upsert(self, model: type[Any], rows: list[dict[str, Any]], keys: list[str]) -> None:
        if not rows:
            return
        statement = insert(model).values(rows)
        updates = {column.name: getattr(statement.excluded, column.name) for column in model.__table__.columns if column.name not in keys and column.name != "id"}
        self.session.execute(statement.on_conflict_do_update(index_elements=keys, set_=updates))
