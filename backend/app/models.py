from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Stock(Base):
    __tablename__ = "stocks"
    stock_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    stock_name: Mapped[str] = mapped_column(String(128))
    market: Mapped[str] = mapped_column(String(16), index=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    security_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_common_stock: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InstitutionalDaily(Base):
    __tablename__ = "institutional_daily"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[str] = mapped_column(ForeignKey("stocks.stock_id"), index=True)
    source_date: Mapped[date] = mapped_column(Date, index=True)
    foreign_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    foreign_dealer_self_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    investment_trust_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    dealer_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    dealer_aggregate_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    dealer_self_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    dealer_hedging_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    institutional_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_dataset: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("stock_id", "source_date", name="uq_institutional_stock_date"), Index("ix_institutional_date", "source_date"))


class ForeignShareholdingDaily(Base):
    __tablename__ = "foreign_shareholding_daily"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[str] = mapped_column(ForeignKey("stocks.stock_id"), index=True)
    source_date: Mapped[date] = mapped_column(Date, index=True)
    foreign_investment_shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    foreign_investment_shares_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    number_of_shares_issued: Mapped[float | None] = mapped_column(Float, nullable=True)
    recently_declare_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_dataset: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("stock_id", "source_date", name="uq_foreign_stock_date"),)


class HoldingDistribution(Base):
    __tablename__ = "holding_distribution"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[str] = mapped_column(ForeignKey("stocks.stock_id"), index=True)
    source_date: Mapped[date] = mapped_column(Date, index=True)
    holding_shares_level: Mapped[str] = mapped_column(String(128))
    holding_shares_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    people: Mapped[float | None] = mapped_column(Float, nullable=True)
    percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_dataset: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("stock_id", "source_date", "holding_shares_level", name="uq_holding_stock_date_level"),)


class BrokerDaily(Base):
    __tablename__ = "broker_daily"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[str] = mapped_column(ForeignKey("stocks.stock_id"), index=True)
    source_date: Mapped[date] = mapped_column(Date, index=True)
    securities_trader_id: Mapped[str] = mapped_column(String(32), index=True)
    securities_trader_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    buy_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_buy_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_sell_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_dataset: Mapped[str] = mapped_column(String(100))
    provider_report_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("stock_id", "source_date", "securities_trader_id", name="uq_broker_stock_date_id"),
        CheckConstraint("source_dataset = 'TaiwanStockTradingDailyReport'", name="ck_broker_daily_official_source"),
    )


class PriceDaily(Base):
    __tablename__ = "price_daily"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[str] = mapped_column(ForeignKey("stocks.stock_id"), index=True)
    source_date: Mapped[date] = mapped_column(Date, index=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    change: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_dataset: Mapped[str] = mapped_column(String(100))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("stock_id", "source_date", name="uq_price_stock_date"),)


class AccumulationFeature(Base):
    __tablename__ = "accumulation_features"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[str] = mapped_column(ForeignKey("stocks.stock_id"), index=True)
    source_date: Mapped[date] = mapped_column(Date, index=True)
    values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    latest_source_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    knowledge_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    input_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("stock_id", "source_date", "knowledge_cutoff", name="uq_features_stock_date_cutoff"),)


class AccumulationScore(Base):
    __tablename__ = "accumulation_scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[str] = mapped_column(ForeignKey("stocks.stock_id"), index=True)
    source_date: Mapped[date] = mapped_column(Date, index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    score_version: Mapped[str] = mapped_column(String(64), index=True)
    components: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explanation: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    knowledge_cutoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    input_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_source_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    formula_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (UniqueConstraint("stock_id", "source_date", "score_version", "knowledge_cutoff", name="uq_score_stock_date_version_cutoff"),)


class DataSyncStatus(Base):
    __tablename__ = "data_sync_status"
    dataset: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    latest_source_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_http_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fully_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_usable_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    records: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usable_records: Mapped[int] = mapped_column(Integer, default=0)
    stored_records: Mapped[int] = mapped_column(Integer, default=0)
    staleness_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempt_latest_source_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expected_latest_source_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_received_this_attempt: Mapped[int] = mapped_column(Integer, default=0)
    rows_accepted_this_attempt: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected_this_attempt: Mapped[int] = mapped_column(Integer, default=0)
    rows_versioned_this_attempt: Mapped[int] = mapped_column(Integer, default=0)
    stored_rows_total: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(100), index=True)
    requested_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    requested_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    requested_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    stocks_attempted: Mapped[int] = mapped_column(Integer, default=0)
    stocks_completed: Mapped[int] = mapped_column(Integer, default=0)
    stocks_failed: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ScoreVersion(Base):
    __tablename__ = "score_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    explanation: Mapped[str] = mapped_column(Text)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceRevision(Base):
    """Append-only normalized source snapshot used for point-in-time calculations."""

    __tablename__ = "source_revisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(100), index=True)
    stock_id: Mapped[str | None] = mapped_column(ForeignKey("stocks.stock_id"), nullable=True, index=True)
    source_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    natural_key: Mapped[str] = mapped_column(String(255), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    __table_args__ = (UniqueConstraint("dataset", "natural_key", "content_hash", name="uq_source_revision_content"),)


class MajorShareholderDisclosure(Base):
    __tablename__ = "major_shareholder_disclosures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stock_id: Mapped[str] = mapped_column(ForeignKey("stocks.stock_id"), index=True)
    holder: Mapped[str] = mapped_column(String(255))
    declare_date: Mapped[date] = mapped_column(Date, index=True)
    holding_shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    change: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("stock_id", "holder", "declare_date", name="uq_major_holder_date"),)
