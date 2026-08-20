from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .features import build_features
from .finmind import FinMindClient
from .models import (
    AccumulationFeature, AccumulationScore, BrokerDaily, DataSyncStatus, ForeignShareholdingDaily,
    HoldingDistribution, InstitutionalDaily, JobRun, PriceDaily, ScoreVersion, Stock,
)
from .scoring import SCORE_VERSION, WEIGHTS, calculate_score, classify_score


def _v(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(db: Session, model: type[Any], filters: dict[str, Any], values: dict[str, Any]) -> Any:
    item = db.scalar(select(model).filter_by(**filters))
    if item is None:
        item = model(**filters, **values)
        db.add(item)
    else:
        for key, value in values.items():
            setattr(item, key, value)
    return item


def normalize_stock(row: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any] | None:
    stock_id = str(_v(row, "stock_id", "股票代號", "證券代號") or "").strip()
    name = str(_v(row, "stock_name", "股票名稱", "證券名稱") or "").strip()
    market = str(_v(row, "market", "市場別") or "").strip()
    security_type = str(_v(row, "type", "security_type", "證券類別") or "").strip()
    excluded_terms = ("ETF", "ETN", "權證", "牛熊", "特別股", "認購權利", "可轉債")
    common_type = security_type in {"股票", "普通股", "Common Stock", ""}
    supported_market = market in {"上市", "上櫃", "興櫃", "TWSE", "TPEx", "ESB"}
    if not stock_id.isdigit() or len(stock_id) != 4 or not supported_market or not common_type or any(term.lower() in f"{name} {security_type}".lower() for term in excluded_terms):
        return None
    return {"stock_id": stock_id, "stock_name": name or stock_id, "market": market, "industry": _v(row, "industry_category", "industry", "產業類別"), "security_type": security_type or "股票", "is_common_stock": True, "source_date": _as_date(_v(row, "date", "source_date")), "fetched_at": fetched_at or _now()}


def normalize_institutional(row: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any] | None:
    source_date = _as_date(_v(row, "date", "source_date"))
    stock_id = str(_v(row, "stock_id", "證券代號") or "").strip()
    if not source_date or not stock_id:
        return None
    foreign = _net_field(row, ("Foreign_Investor_Buy", "foreign_investor_buy", "ForeignInvestorBuy"), ("Foreign_Investor_Sell", "foreign_investor_sell", "ForeignInvestorSell"), ("Foreign_Investor_Net", "foreign_net", "ForeignInvestorNet"))
    foreign_self = _net_field(row, ("Foreign_Dealer_Self_Buy", "foreign_dealer_self_buy"), ("Foreign_Dealer_Self_Sell", "foreign_dealer_self_sell"), ("Foreign_Dealer_Self_Net", "foreign_dealer_self_net"))
    trust = _net_field(row, ("Investment_Trust_Buy", "investment_trust_buy"), ("Investment_Trust_Sell", "investment_trust_sell"), ("Investment_Trust_Net", "investment_trust_net", "InvestmentTrustNet"))
    dealer = _net_field(row, ("Dealer_Buy", "dealer_buy"), ("Dealer_Sell", "dealer_sell"), ("Dealer_Net", "dealer_net", "DealerNet"))
    dealer_self = _net_field(row, ("Dealer_self_Buy", "Dealer_Self_Buy", "dealer_self_buy"), ("Dealer_self_Sell", "Dealer_Self_Sell", "dealer_self_sell"), ("Dealer_self_Net", "Dealer_Self_Net", "dealer_self_net"))
    hedge = _net_field(row, ("Dealer_Hedging_Buy", "dealer_hedging_buy"), ("Dealer_Hedging_Sell", "dealer_hedging_sell"), ("Dealer_Hedging_Net", "dealer_hedging_net"))
    components = [foreign, foreign_self, trust, dealer, dealer_self, hedge]
    institutional = sum(float(v) for v in components) if all(v is not None for v in components) else None
    return {"stock_id": stock_id, "source_date": source_date, "foreign_net": _num(foreign), "foreign_dealer_self_net": _num(foreign_self), "investment_trust_net": _num(trust), "dealer_net": _num(dealer), "dealer_self_net": _num(dealer_self), "dealer_hedging_net": _num(hedge), "institutional_net": institutional, "source_dataset": "TaiwanStockInstitutionalInvestorsBuySellWide", "fetched_at": fetched_at or _now()}


def normalize_foreign(row: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any] | None:
    source_date = _as_date(_v(row, "date", "source_date"))
    stock_id = str(_v(row, "stock_id", "證券代號") or "").strip()
    if not source_date or not stock_id:
        return None
    return {"stock_id": stock_id, "source_date": source_date, "foreign_investment_shares": _num(_v(row, "ForeignInvestmentShares", "foreign_investment_shares")), "foreign_investment_shares_ratio": _num(_v(row, "ForeignInvestmentSharesRatio", "foreign_investment_shares_ratio")), "number_of_shares_issued": _num(_v(row, "NumberOfSharesIssued", "number_of_shares_issued")), "recently_declare_date": _v(row, "RecentlyDeclareDate", "recently_declare_date"), "source_dataset": "TaiwanStockShareholding", "fetched_at": fetched_at or _now()}


def normalize_holding(row: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any] | None:
    source_date = _as_date(_v(row, "date", "source_date"))
    stock_id = str(_v(row, "stock_id", "證券代號") or "").strip()
    level = _v(row, "HoldingSharesLevel", "holding_shares_level")
    threshold = _v(row, "holding_shares_threshold")
    if threshold is None:
        from .scoring import parse_holding_level
        threshold = parse_holding_level(level)
    if not source_date or not stock_id or threshold is None:
        return None
    people = _num(_v(row, "people", "People"))
    percent = _num(_v(row, "percent", "Percent"))
    return {"stock_id": stock_id, "source_date": source_date, "holding_shares_level": str(level), "holding_shares_threshold": threshold, "people": people, "percent": percent, "unit": _v(row, "unit", "Unit"), "source_dataset": "TaiwanStockHoldingSharesPer", "fetched_at": fetched_at or _now()}


def normalize_broker(row: dict[str, Any], fetched_at: datetime | None = None, dataset: str = "TaiwanStockTradingDailyReport") -> dict[str, Any] | None:
    source_date = _as_date(_v(row, "date", "source_date"))
    stock_id = str(_v(row, "stock_id", "證券代號") or "").strip()
    trader_id = str(_v(row, "securities_trader_id", "securities_trader_id", "券商代號", "證券商代號") or "").strip()
    if not source_date or not stock_id or not trader_id:
        return None
    buy = _num(_v(row, "buy_volume", "買進股數", "BuyVolume"))
    sell = _num(_v(row, "sell_volume", "賣出股數", "SellVolume"))
    net_value = _num(_v(row, "net_volume", "NetVolume"))
    if net_value is None and buy is not None and sell is not None:
        net_value = buy - sell
    return {"stock_id": stock_id, "source_date": source_date, "securities_trader_id": trader_id, "securities_trader_name": _v(row, "securities_trader_name", "券商名稱"), "buy_volume": buy, "sell_volume": sell, "net_volume": net_value, "buy_amount": _num(_v(row, "buy_amount", "買進金額")), "sell_amount": _num(_v(row, "sell_amount", "賣出金額")), "avg_buy_price": _num(_v(row, "avg_buy_price")), "avg_sell_price": _num(_v(row, "avg_sell_price")), "source_dataset": dataset, "fetched_at": fetched_at or _now()}


def normalize_price(row: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any] | None:
    source_date = _as_date(_v(row, "date", "source_date"))
    stock_id = str(_v(row, "stock_id", "證券代號") or "").strip()
    if not source_date or not stock_id:
        return None
    return {"stock_id": stock_id, "source_date": source_date, "close": _num(_v(row, "close", "收盤價")), "volume": _num(_v(row, "TradingVolume", "volume", "成交股數")), "change": _num(_v(row, "change", "漲跌價差")), "source_dataset": "TaiwanStockPrice", "fetched_at": fetched_at or _now()}


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _net_field(row: dict[str, Any], buy_keys: tuple[str, ...], sell_keys: tuple[str, ...], net_keys: tuple[str, ...]) -> float | None:
    direct = _v(row, *net_keys)
    if direct is not None:
        return _num(direct)
    buy = _num(_v(row, *buy_keys))
    sell = _num(_v(row, *sell_keys))
    return buy - sell if buy is not None and sell is not None else None


def ingest_records(db: Session, dataset: str, records: list[dict[str, Any]]) -> int:
    count = 0
    for row in records:
        if dataset == "TaiwanStockInfo":
            normalized = normalize_stock(row)
            model, unique, values = Stock, {"stock_id": normalized["stock_id"]} if normalized else None, normalized
        elif dataset == "TaiwanStockInstitutionalInvestorsBuySellWide":
            normalized = normalize_institutional(row)
            model, unique, values = InstitutionalDaily, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"]} if normalized else None, normalized
        elif dataset == "TaiwanStockShareholding":
            normalized = normalize_foreign(row)
            model, unique, values = ForeignShareholdingDaily, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"]} if normalized else None, normalized
        elif dataset == "TaiwanStockHoldingSharesPer":
            normalized = normalize_holding(row)
            model, unique, values = HoldingDistribution, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"], "holding_shares_level": normalized["holding_shares_level"]} if normalized else None, normalized
        elif dataset in {"TaiwanStockTradingDailyReport", "TaiwanStockTradingDailyReportSecIdAgg"}:
            normalized = normalize_broker(row, dataset=dataset)
            model, unique, values = BrokerDaily, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"], "securities_trader_id": normalized["securities_trader_id"]} if normalized else None, normalized
        elif dataset == "TaiwanStockPrice":
            normalized = normalize_price(row)
            model, unique, values = PriceDaily, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"]} if normalized else None, normalized
        else:
            continue
        if normalized and unique:
            _upsert(db, model, unique, {k: v for k, v in values.items() if k not in unique})
            count += 1
    db.commit()
    return count


def sync_universe(db: Session, client: FinMindClient) -> int:
    records, meta = client.fetch("TaiwanStockInfo")
    count = ingest_records(db, "TaiwanStockInfo", records)
    _mark_sync(db, "TaiwanStockInfo", "SUCCESS", count, _as_date(meta.get("source_date")))
    return count


def sync_stock_dataset(db: Session, client: FinMindClient, dataset: str, stock_id: str, start_date: str, end_date: str) -> int:
    records, meta = client.fetch(dataset, stock_id, start_date, end_date)
    count = ingest_records(db, dataset, records)
    _mark_sync(db, dataset, "SUCCESS", count, _as_date(meta.get("source_date")))
    return count


def _mark_sync(db: Session, dataset: str, status: str, records: int, latest: date | None, error_code: str | None = None, error: str | None = None) -> None:
    item = db.get(DataSyncStatus, dataset)
    if item is None:
        item = DataSyncStatus(dataset=dataset, status=status, records=records)
        db.add(item)
    item.status = status
    item.records = records
    item.latest_source_date = latest
    item.last_successful_sync = _now() if status == "SUCCESS" else item.last_successful_sync
    item.last_error_code = error_code
    item.last_error = error
    db.commit()


def calculate_stock_features_and_score(db: Session, stock_id: str, as_of: date | None = None) -> AccumulationScore:
    as_of = as_of or date.today()
    inst_rows = db.scalars(select(InstitutionalDaily).where(InstitutionalDaily.stock_id == stock_id, InstitutionalDaily.source_date <= as_of).order_by(InstitutionalDaily.source_date.desc()).limit(20)).all()
    foreign_rows = db.scalars(select(ForeignShareholdingDaily).where(ForeignShareholdingDaily.stock_id == stock_id, ForeignShareholdingDaily.source_date <= as_of).order_by(ForeignShareholdingDaily.source_date.desc()).limit(21)).all()
    holding_rows = db.scalars(select(HoldingDistribution).where(HoldingDistribution.stock_id == stock_id, HoldingDistribution.source_date <= as_of).order_by(HoldingDistribution.source_date.desc()).limit(100)).all()
    broker_rows = db.scalars(select(BrokerDaily).where(BrokerDaily.stock_id == stock_id, BrokerDaily.source_date <= as_of).order_by(BrokerDaily.source_date.desc()).limit(10000)).all()
    price_rows = db.scalars(select(PriceDaily).where(PriceDaily.stock_id == stock_id, PriceDaily.source_date <= as_of).order_by(PriceDaily.source_date.desc()).limit(21)).all()
    to_dict = lambda row: {k: getattr(row, k) for k in row.__table__.columns.keys()}
    inst = [to_dict(r) for r in reversed(inst_rows)]
    foreign = [to_dict(r) for r in reversed(foreign_rows)]
    holdings = [to_dict(r) for r in reversed(holding_rows)]
    brokers = [to_dict(r) for r in reversed(broker_rows)]
    prices = [to_dict(r) for r in reversed(price_rows)]
    features = build_features(inst, foreign, holdings, brokers, prices)
    coverage = {"InstitutionalDataAvailable": len(inst) >= 20, "ForeignHoldingDataAvailable": len(foreign) >= 2, "HoldingDistributionAvailable": bool(holdings), "BrokerDataAvailable": bool(brokers), "PriceDataAvailable": len(prices) >= 20}
    result = calculate_score(features, coverage)
    now = _now()
    _upsert(db, AccumulationFeature, {"stock_id": stock_id, "source_date": as_of}, {"values": features, "coverage": coverage, "latest_source_date": max((str(r.get("source_date")) for r in inst + foreign + holdings + brokers + prices if r.get("source_date")), default=None), "calculated_at": now})
    score = _upsert(db, AccumulationScore, {"stock_id": stock_id, "source_date": as_of, "score_version": SCORE_VERSION}, {"score": result.score, "status": result.status, "components": result.components, "explanation": result.explanation, "coverage": coverage, "calculated_at": now})
    db.commit()
    return score


def seed_score_version(db: Session) -> None:
    if db.get(ScoreVersion, SCORE_VERSION) is None:
        db.add(ScoreVersion(version=SCORE_VERSION, config={"weights": WEIGHTS, "low_profile_modifier": [-10, 10], "allowed_datasets": sorted([*{ "TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding", "TaiwanStockHoldingSharesPer", "TaiwanStockTradingDailyReport", "TaiwanStockTradingDailyReportSecIdAgg"}])}, explanation="S-only v1: persistence, ownership accumulation and broker persistence are weighted; price/volume only modifies by -10 to +10.", created_at=_now()))
        db.commit()


async def catch_up(db: Session, client: FinMindClient) -> dict[str, Any]:
    """Best-effort scheduled catch-up; permission errors remain visible, never converted to empty data."""
    result: dict[str, Any] = {"status": "PARTIAL", "datasets": {}}
    stock_count = sync_universe(db, client)
    result["datasets"]["TaiwanStockInfo"] = stock_count
    result["status"] = "SUCCESS"
    return result
