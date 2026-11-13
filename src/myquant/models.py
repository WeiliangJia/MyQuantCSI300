from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from myquant.db import Base


class RawArchive(Base):
    __tablename__ = "archive"
    __table_args__ = ({"schema": "raw"},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    archive_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TradeCalendar(Base):
    __tablename__ = "trade_calendar"
    __table_args__ = (UniqueConstraint("exchange", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_open: Mapped[bool] = mapped_column(nullable=False)
    previous_trade_date: Mapped[date | None] = mapped_column(Date)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Instrument(Base):
    __tablename__ = "instrument"
    __table_args__ = ({"schema": "market"},)

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(64))
    exchange: Mapped[str | None] = mapped_column(String(16))
    industry: Mapped[str | None] = mapped_column(String(64))
    list_date: Mapped[date | None] = mapped_column(Date)
    delist_date: Mapped[date | None] = mapped_column(Date)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorporateAction(Base):
    __tablename__ = "corporate_action"
    __table_args__ = (UniqueConstraint("ts_code", "action_type", "record_date", "announcement_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    record_date: Mapped[date | None] = mapped_column(Date)
    ex_date: Mapped[date | None] = mapped_column(Date)
    announcement_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyBar(Base):
    __tablename__ = "daily_bar"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    pre_close: Mapped[float | None] = mapped_column(Numeric(18, 4))
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceDailyBar(Base):
    """Immutable normalized bars per provider; canonical rows are built only by the cleaner."""

    __tablename__ = "source_daily_bar"
    __table_args__ = (UniqueConstraint("source", "ts_code", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    pre_close: Mapped[float | None] = mapped_column(Numeric(18, 4))
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CleanDailyBar(Base):
    __tablename__ = "clean_daily_bar"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    selected_source: Mapped[str] = mapped_column(String(32), nullable=False)
    open: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    adj_factor: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    adj_open: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    adj_high: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    adj_low: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    adj_close: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    is_suspended: Mapped[bool] = mapped_column(nullable=False)
    is_zero_volume: Mapped[bool] = mapped_column(nullable=False)
    is_limit_up_locked: Mapped[bool] = mapped_column(nullable=False)
    is_limit_down_locked: Mapped[bool] = mapped_column(nullable=False)
    flags: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    cleaned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecurityStatus(Base):
    __tablename__ = "security_status"
    __table_args__ = (UniqueConstraint("ts_code", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str | None] = mapped_column(String(64))
    is_st: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_suspended: Mapped[bool] = mapped_column(nullable=False, default=False)
    is_delisted: Mapped[bool] = mapped_column(nullable=False, default=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CleanDailyFeature(Base):
    __tablename__ = "clean_daily_feature"
    __table_args__ = (UniqueConstraint("dataset", "ts_code", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(32), nullable=False)  # valuation or moneyflow
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    selected_source: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    cleaned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IndexConstituent(Base):
    __tablename__ = "index_constituent"
    __table_args__ = (UniqueConstraint("index_code", "ts_code", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_code: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    weight: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    # A weight is not necessarily an official index weight.  Keeping both its
    # provenance and methodology prevents a calculated free-float proxy from
    # being presented as licensed benchmark evidence.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    methodology: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BenchmarkRebalance(Base):
    __tablename__ = "benchmark_rebalance"
    __table_args__ = (UniqueConstraint("index_code", "execution_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_code: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_date: Mapped[date | None] = mapped_column(Date)
    execution_date: Mapped[date] = mapped_column(Date, nullable=False)
    rebalance_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_weights: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    executed: Mapped[bool] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)


class IndexMembershipEvent(Base):
    """Official index-adjustment evidence; weights remain separate from membership."""

    __tablename__ = "index_membership_event"
    __table_args__ = (
        UniqueConstraint("index_code", "ts_code", "effective_date", "action"),
        {"schema": "market"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_code: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)  # ADD or REMOVE
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    announcement_date: Mapped[date] = mapped_column(Date, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IndexMembershipInterval(Base):
    __tablename__ = "index_membership_interval"
    __table_args__ = (
        UniqueConstraint("index_code", "ts_code", "entry_date", "source"),
        {"schema": "market"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_code: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(64))
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    exit_date: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IndexMembershipSnapshot(Base):
    __tablename__ = "index_membership_snapshot"
    __table_args__ = (UniqueConstraint("index_code", "ts_code", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_code: Mapped[str] = mapped_column(String(16), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_member: Mapped[bool] = mapped_column(nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FinancialObservation(Base):
    __tablename__ = "financial_observation"
    __table_args__ = (UniqueConstraint("ts_code", "metric", "end_date", "announcement_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    announcement_date: Mapped[date] = mapped_column(Date, nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FinancialSnapshot(Base):
    __tablename__ = "financial_snapshot"
    __table_args__ = (UniqueConstraint("ts_code", "as_of_date"), {"schema": "research"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    latest_announcement_date: Mapped[date | None] = mapped_column(Date)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DailyObservation(Base):
    """Daily vendor fields that do not warrant a separate wide production table yet."""

    __tablename__ = "daily_observation"
    __table_args__ = (UniqueConstraint("dataset", "ts_code", "trade_date"), {"schema": "market"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FactorValue(Base):
    __tablename__ = "factor_value"
    __table_args__ = (UniqueConstraint("factor_name", "ts_code", "trade_date", "available_at"), {"schema": "research"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    factor_name: Mapped[str] = mapped_column(String(64), nullable=False)
    factor_group: Mapped[str] = mapped_column(String(32), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    available_at: Mapped[date] = mapped_column(Date, nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)


class TargetWeight(Base):
    __tablename__ = "target_weight"
    __table_args__ = (UniqueConstraint("run_id", "execution_date", "ts_code"), {"schema": "portfolio"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    execution_date: Mapped[date] = mapped_column(Date, nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    target_weight: Mapped[float] = mapped_column(Numeric(10, 8), nullable=False)
    alpha: Mapped[float] = mapped_column(Float, nullable=False)
    approved: Mapped[bool] = mapped_column(nullable=False, default=False)


class SimulatedOrder(Base):
    __tablename__ = "order"
    __table_args__ = ({"schema": "simulation"},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fill_price: Mapped[float | None] = mapped_column(Numeric(18, 4))
    fees: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False, default=0.0)


class QualityCheck(Base):
    __tablename__ = "quality_check"
    __table_args__ = ({"schema": "ops"},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issue"
    __table_args__ = ({"schema": "ops"},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    severity: Mapped[str] = mapped_column(String(12), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str | None] = mapped_column(String(32))
    ts_code: Mapped[str | None] = mapped_column(String(16))
    trade_date: Mapped[date | None] = mapped_column(Date)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
