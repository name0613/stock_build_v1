from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
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
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("stock_id", "source_date", "securities_trader_id", name="uq_broker_stock_date_id"),)


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
    __table_args__ = (UniqueConstraint("stock_id", "source_date", name="uq_features_stock_date"),)


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
    __table_args__ = (UniqueConstraint("stock_id", "source_date", "score_version", name="uq_score_stock_date_version"),)


class DataSyncStatus(Base):
    __tablename__ = "data_sync_status"
    dataset: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    latest_source_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    records: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(100), index=True)
    requested_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)


class ScoreVersion(Base):
    __tablename__ = "score_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    explanation: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
