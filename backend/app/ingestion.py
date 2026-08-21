from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .calendar import CALENDAR_VERSION, expected_trading_sessions, missing_sessions
from .features import build_features
from .finmind import GLOBAL_PROVIDER_FAILURE_CODES, FinMindClient, FinMindError, SchemaMismatch
from .models import (
    AccumulationFeature, AccumulationScore, BrokerDaily, DataSyncStatus, ForeignShareholdingDaily,
    HoldingDistribution, InstitutionalDaily, JobRun, PriceDaily, ScoreVersion, SourceRevision, Stock,
)
from .scoring import FORMULA_HASH, SCORE_MANIFEST, SCORE_VERSION, calculate_score


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


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _payload_content_hash(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key not in {"id", "fetched_at"}}
    return hashlib.sha256(json.dumps(_jsonable(content), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _record_revision(db: Session, dataset: str, normalized: dict[str, Any], unique: dict[str, Any], fetched_at: datetime) -> str:
    payload = _jsonable(normalized)
    content_hash = _payload_content_hash(payload)
    natural_key = json.dumps(_jsonable(unique), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    exists = db.scalar(select(SourceRevision).where(SourceRevision.dataset == dataset, SourceRevision.natural_key == natural_key, SourceRevision.content_hash == content_hash))
    if exists is None:
        db.add(SourceRevision(dataset=dataset, stock_id=normalized.get("stock_id"), source_date=normalized.get("source_date"), natural_key=natural_key, payload=payload, content_hash=content_hash, fetched_at=fetched_at))
    return content_hash


def normalize_stock(row: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any] | None:
    stock_id = str(_v(row, "stock_id", "股票代號", "證券代號") or "").strip()
    name = str(_v(row, "stock_name", "股票名稱", "證券名稱") or "").strip()
    raw_market = str(_v(row, "market", "市場別", "type") or "").strip().lower()
    market = {"twse": "上市", "tpex": "上櫃", "esb": "興櫃", "rotc": "興櫃"}.get(raw_market, str(_v(row, "market", "市場別") or "").strip())
    security_type = str(_v(row, "security_type", "證券類別") or "").strip()
    industry = str(_v(row, "industry_category", "industry", "產業類別") or "").strip()
    excluded_terms = ("ETF", "ETN", "權證", "牛熊", "特別股", "認購權利", "可轉債")
    common_type = security_type.lower() in {"股票", "普通股", "common stock", "stock", "common_stock", ""}
    supported_market = market in {"上市", "上櫃", "興櫃", "TWSE", "TPEx", "ESB"}
    if not stock_id.isdigit() or len(stock_id) != 4 or not supported_market or not common_type or any(term.lower() in f"{name} {security_type} {industry}".lower() for term in excluded_terms):
        return None
    return {"stock_id": stock_id, "stock_name": name or stock_id, "market": market, "industry": industry or None, "security_type": security_type or "股票", "is_common_stock": True, "source_date": _as_date(_v(row, "date", "source_date")), "fetched_at": fetched_at or _now()}


def classify_stock_rejection(row: dict[str, Any]) -> str:
    """Classify one rejected TaiwanStockInfo row without overlapping buckets."""
    stock_id = str(_v(row, "stock_id", "股票代號", "證券代號") or "").strip()
    name = str(_v(row, "stock_name", "股票名稱", "證券名稱") or "").strip()
    raw_market = str(_v(row, "market", "市場別", "type") or "").strip().lower()
    market = {"twse": "上市", "tpex": "上櫃", "esb": "興櫃", "rotc": "興櫃"}.get(raw_market, str(_v(row, "market", "市場別") or "").strip())
    security_type = str(_v(row, "security_type", "證券類別") or "").strip()
    industry = str(_v(row, "industry_category", "industry", "產業類別") or "").strip()
    if not stock_id.isdigit() or len(stock_id) != 4:
        return "invalid_identifier"
    if market not in {"上市", "上櫃", "興櫃", "TWSE", "TPEx", "ESB"}:
        return "unsupported_market"
    if security_type.lower() not in {"股票", "普通股", "common stock", "stock", "common_stock", ""}:
        return "unsupported_security_type"
    lowered = f"{name} {security_type} {industry}".lower()
    for term, category in (("etf", "etf"), ("etn", "etn"), ("權證", "warrant"), ("牛熊", "warrant"), ("特別股", "preferred"), ("認購權利", "subscription_right"), ("可轉債", "convertible")):
        if term.lower() in lowered:
            return category
    return "other_non_common_instrument"


def normalize_institutional(row: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any] | None:
    source_date = _as_date(_v(row, "date", "source_date"))
    stock_id = str(_v(row, "stock_id", "證券代號") or "").strip()
    if not source_date or not stock_id:
        return None
    foreign = _net_field(row, ("Foreign_Investor_Buy", "Foreign_Investor_buy", "foreign_investor_buy", "ForeignInvestorBuy"), ("Foreign_Investor_Sell", "Foreign_Investor_sell", "foreign_investor_sell", "ForeignInvestorSell"), ("Foreign_Investor_Net", "foreign_net", "ForeignInvestorNet"))
    foreign_self = _net_field(row, ("Foreign_Dealer_Self_Buy", "Foreign_Dealer_Self_buy", "foreign_dealer_self_buy"), ("Foreign_Dealer_Self_Sell", "Foreign_Dealer_Self_sell", "foreign_dealer_self_sell"), ("Foreign_Dealer_Self_Net", "foreign_dealer_self_net"))
    trust = _net_field(row, ("Investment_Trust_Buy", "Investment_Trust_buy", "investment_trust_buy"), ("Investment_Trust_Sell", "Investment_Trust_sell", "investment_trust_sell"), ("Investment_Trust_Net", "investment_trust_net", "InvestmentTrustNet"))
    dealer = _net_field(row, ("Dealer_Buy", "Dealer_buy", "dealer_buy"), ("Dealer_Sell", "Dealer_sell", "dealer_sell"), ("Dealer_Net", "dealer_net", "DealerNet"))
    dealer_self = _net_field(row, ("Dealer_self_Buy", "Dealer_self_buy", "Dealer_Self_Buy", "dealer_self_buy"), ("Dealer_self_Sell", "Dealer_self_sell", "Dealer_Self_Sell", "dealer_self_sell"), ("Dealer_self_Net", "Dealer_Self_Net", "dealer_self_net"))
    hedge = _net_field(row, ("Dealer_Hedging_Buy", "Dealer_Hedging_buy", "dealer_hedging_buy"), ("Dealer_Hedging_Sell", "Dealer_Hedging_sell", "dealer_hedging_sell"), ("Dealer_Hedging_Net", "dealer_hedging_net"))
    # FinMind's Dealer field is an aggregate when its self/hedging
    # components are present.  Use exactly one representation so dealer
    # activity cannot be counted twice; fall back to the aggregate only when
    # the provider did not return the components.
    if dealer_self is not None and hedge is not None:
        dealer_component = dealer_self + hedge
    else:
        dealer_component = dealer
    components = [foreign, foreign_self, trust, dealer_component]
    institutional = sum(float(v) for v in components) if all(v is not None for v in components) else None
    return {"stock_id": stock_id, "source_date": source_date, "foreign_net": _num(foreign), "foreign_dealer_self_net": _num(foreign_self), "investment_trust_net": _num(trust), "dealer_net": _num(dealer_component), "dealer_aggregate_net": _num(dealer), "dealer_self_net": _num(dealer_self), "dealer_hedging_net": _num(hedge), "institutional_net": institutional, "source_dataset": "TaiwanStockInstitutionalInvestorsBuySellWide", "fetched_at": fetched_at or _now()}


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
    unit = _v(row, "unit", "Unit")
    shares = _num(_v(row, "shares", "Shares", "HoldingShares"))
    if shares is None:
        shares = _num(unit)
    return {"stock_id": stock_id, "source_date": source_date, "holding_shares_level": str(level), "holding_shares_threshold": threshold, "people": people, "percent": percent, "shares": shares, "unit": unit, "source_dataset": "TaiwanStockHoldingSharesPer", "fetched_at": fetched_at or _now()}


def normalize_broker(row: dict[str, Any], fetched_at: datetime | None = None, dataset: str = "TaiwanStockTradingDailyReport") -> dict[str, Any] | None:
    source_date = _as_date(_v(row, "date", "source_date"))
    stock_id = str(_v(row, "stock_id", "證券代號") or "").strip()
    trader_id = str(_v(row, "securities_trader_id", "securities_trader_id", "券商代號", "證券商代號") or "").strip()
    if not source_date or not stock_id or not trader_id:
        return None
    buy = _num(_v(row, "buy_volume", "buy", "買進股數", "BuyVolume"))
    sell = _num(_v(row, "sell_volume", "sell", "賣出股數", "SellVolume"))
    price = _num(_v(row, "price", "Price"))
    net_value = _num(_v(row, "net_volume", "NetVolume"))
    if net_value is None and buy is not None and sell is not None:
        net_value = buy - sell
    avg_buy = _num(_v(row, "avg_buy_price"))
    avg_sell = _num(_v(row, "avg_sell_price"))
    return {"stock_id": stock_id, "source_date": source_date, "securities_trader_id": trader_id, "securities_trader_name": _v(row, "securities_trader_name", "securities_trader", "券商名稱"), "buy_volume": buy, "sell_volume": sell, "net_volume": net_value, "buy_amount": buy * price if buy is not None and price is not None else _num(_v(row, "buy_amount", "買進金額")), "sell_amount": sell * price if sell is not None and price is not None else _num(_v(row, "sell_amount", "賣出金額")), "avg_buy_price": avg_buy if avg_buy is not None else price, "avg_sell_price": avg_sell if avg_sell is not None else price, "source_dataset": dataset, "provider_report_complete": _v(row, "provider_report_complete") is True, "fetched_at": fetched_at or _now()}


def normalize_price(row: dict[str, Any], fetched_at: datetime | None = None) -> dict[str, Any] | None:
    source_date = _as_date(_v(row, "date", "source_date"))
    stock_id = str(_v(row, "stock_id", "證券代號") or "").strip()
    if not source_date or not stock_id:
        return None
    return {"stock_id": stock_id, "source_date": source_date, "close": _num(_v(row, "close", "收盤價")), "volume": _num(_v(row, "TradingVolume", "Trading_Volume", "volume", "成交股數")), "change": _num(_v(row, "change", "spread", "漲跌價差")), "source_dataset": "TaiwanStockPrice", "fetched_at": fetched_at or _now()}


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _sum_nullable(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left + right


def _net_field(row: dict[str, Any], buy_keys: tuple[str, ...], sell_keys: tuple[str, ...], net_keys: tuple[str, ...]) -> float | None:
    direct = _v(row, *net_keys)
    if direct is not None:
        return _num(direct)
    buy = _num(_v(row, *buy_keys))
    sell = _num(_v(row, *sell_keys))
    return buy - sell if buy is not None and sell is not None else None


def ingest_records(db: Session, dataset: str, records: list[dict[str, Any]]) -> int:
    if dataset == "TaiwanStockHoldingSharesPer":
        seen_buckets: set[tuple[str, str, str]] = set()
        existing_rows = db.scalars(select(HoldingDistribution)).all()
        existing_by_bucket = {(row.stock_id, row.source_date.isoformat(), row.holding_shares_threshold): row for row in existing_rows if row.holding_shares_threshold is not None}
        for row in records:
            level = _v(row, "HoldingSharesLevel", "holding_shares_level")
            if level is not None and str(level).strip().lower() not in {"total", "all"} and _v(row, "holding_shares_threshold") is None:
                from .scoring import is_holding_metadata_level, parse_holding_level
                if not is_holding_metadata_level(level) and parse_holding_level(level) is None:
                    raise SchemaMismatch("SCHEMA_MISMATCH", "holding source returned an unknown relevant bucket")
            stock = str(_v(row, "stock_id", "證券代號") or "")
            source_day = str(_v(row, "date", "source_date") or "")[:10]
            from .scoring import parse_holding_level
            parsed_threshold = _v(row, "holding_shares_threshold") or parse_holding_level(level)
            bucket = f"threshold:{parsed_threshold}" if parsed_threshold is not None else str(level or "").strip().lower()
            key = (stock, source_day, bucket)
            if bucket and key in seen_buckets:
                raise SchemaMismatch("SCHEMA_MISMATCH", "holding source returned a duplicate bucket for a stock/date")
            if bucket:
                seen_buckets.add(key)
                if parsed_threshold is not None:
                    existing = existing_by_bucket.get((stock, source_day, parsed_threshold))
                    if existing is not None and existing.holding_shares_level != str(level):
                        raise SchemaMismatch("SCHEMA_MISMATCH", "holding source returned a duplicate normalized bucket across fetches")
    if dataset == "TaiwanStockInfo":
        latest_by_stock: dict[str, dict[str, Any]] = {}
        for row in records:
            normalized = normalize_stock(row)
            if normalized is None:
                continue
            previous = latest_by_stock.get(normalized["stock_id"])
            if previous is None or (normalized.get("source_date") or date.min) >= (previous.get("source_date") or date.min):
                latest_by_stock[normalized["stock_id"]] = normalized
        records = list(latest_by_stock.values())
    if dataset in {"TaiwanStockTradingDailyReport", "TaiwanStockTradingDailyReportSecIdAgg"}:
        aggregated: dict[tuple[str, date, str], dict[str, Any]] = {}
        for row in records:
            normalized = normalize_broker(row, dataset=dataset)
            if normalized is None:
                continue
            key = (normalized["stock_id"], normalized["source_date"], normalized["securities_trader_id"])
            current = aggregated.setdefault(key, normalized)
            if current is not normalized:
                for field in ("buy_volume", "sell_volume", "buy_amount", "sell_amount"):
                    current[field] = _sum_nullable(current.get(field), normalized.get(field))
                current["net_volume"] = _sum_nullable(current.get("net_volume"), normalized.get("net_volume"))
        records = list(aggregated.values())
    valid_stock_ids = None if dataset == "TaiwanStockInfo" else set(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))).all())
    count = 0
    fetched_at = _now()
    for row in records:
        if dataset == "TaiwanStockInfo":
            normalized = normalize_stock(row, fetched_at)
            model, unique, values = Stock, {"stock_id": normalized["stock_id"]} if normalized else None, normalized
        elif dataset == "TaiwanStockInstitutionalInvestorsBuySellWide":
            normalized = normalize_institutional(row, fetched_at)
            model, unique, values = InstitutionalDaily, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"]} if normalized else None, normalized
        elif dataset == "TaiwanStockShareholding":
            normalized = normalize_foreign(row, fetched_at)
            model, unique, values = ForeignShareholdingDaily, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"]} if normalized else None, normalized
        elif dataset == "TaiwanStockHoldingSharesPer":
            normalized = normalize_holding(row, fetched_at)
            model, unique, values = HoldingDistribution, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"], "holding_shares_level": normalized["holding_shares_level"]} if normalized else None, normalized
        elif dataset in {"TaiwanStockTradingDailyReport", "TaiwanStockTradingDailyReportSecIdAgg"}:
            normalized = normalize_broker(row, fetched_at, dataset=dataset)
            model, unique, values = BrokerDaily, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"], "securities_trader_id": normalized["securities_trader_id"]} if normalized else None, normalized
        elif dataset == "TaiwanStockPrice":
            normalized = normalize_price(row, fetched_at)
            model, unique, values = PriceDaily, {"stock_id": normalized["stock_id"], "source_date": normalized["source_date"]} if normalized else None, normalized
        else:
            continue
        if normalized and unique and (valid_stock_ids is None or normalized["stock_id"] in valid_stock_ids):
            _record_revision(db, dataset, normalized, unique, fetched_at)
            _upsert(db, model, unique, {k: v for k, v in values.items() if k not in unique})
            count += 1
    db.commit()
    return count


def sync_universe(db: Session, client: FinMindClient, as_of: date | None = None) -> int:
    """Refresh the dynamic universe without ever activating a failed snapshot."""
    records: list[dict[str, Any]] = []
    try:
        records, meta = client.fetch("TaiwanStockInfo")
        source_date = _as_date(meta.get("source_date"))
        expected = _expected_latest_source_date("TaiwanStockInfo", as_of or source_date or date.today())
        if not records:
            raise FinMindError("EMPTY_RESPONSE_UNVERIFIED", "TaiwanStockInfo returned no rows; universe authority is unproven")
        if meta.get("pagination_complete") is False:
            raise FinMindError("INCOMPLETE_PROVIDER_COVERAGE", "TaiwanStockInfo pagination was not complete; universe authority is unproven")

        rejection_counts: dict[str, int] = {}
        market_counts: dict[str, int] = {}
        groups: dict[str, list[dict[str, Any]]] = {}
        for index, row in enumerate(records):
            raw_id = str(_v(row, "stock_id", "股票代號", "證券代號") or "").strip()
            groups.setdefault(raw_id or f"__missing_{index}", []).append(row)
        duplicate_count = sum(max(0, len(rows) - 1) for key, rows in groups.items() if not key.startswith("__missing_"))
        accepted_ids: set[str] = set()
        for key, rows in groups.items():
            normalized_rows = [normalize_stock(row) for row in rows]
            normalized_rows = [row for row in normalized_rows if row is not None]
            if normalized_rows:
                normalized = max(normalized_rows, key=lambda row: row.get("source_date") or date.min)
                accepted_ids.add(normalized["stock_id"])
                market_counts[normalized["market"]] = market_counts.get(normalized["market"], 0) + 1
            else:
                category = classify_stock_rejection(rows[-1])
                rejection_counts[category] = rejection_counts.get(category, 0) + 1

        count = len(accepted_ids)
        rejected_unique = sum(rejection_counts.values())
        reconciliation = {"raw_count": len(records), "duplicate_count": duplicate_count, "rejected_unique_count": rejected_unique, "accepted_common_count": count, "reconciles": len(records) == duplicate_count + rejected_unique + count}
        if count == 0 or not reconciliation["reconciles"]:
            raise FinMindError("SCHEMA_MISMATCH", "TaiwanStockInfo did not produce a non-empty reconciled common-stock universe")

        # Ingest and deactivate only after the full response has passed all
        # authority checks.  A failed/partial response therefore preserves the
        # previous active universe exactly as-is.
        ingest_records(db, "TaiwanStockInfo", records)
        for existing in db.scalars(select(Stock).where(Stock.is_common_stock.is_(True))).all():
            if existing.stock_id not in accepted_ids:
                existing.is_common_stock = False
        db.commit()
        info_dates = [_as_date(_v(row, "date", "source_date")) for row in records]
        latest = max((value for value in info_dates if value is not None), default=source_date)
        _mark_sync(db, "TaiwanStockInfo", "SUCCESS", count, latest, fetched_at=_now(), expected_latest=expected, rows_received=len(records), rows_accepted=count, rows_rejected=duplicate_count + rejected_unique, stored_total=_stored_rows_total(db, "TaiwanStockInfo"), metadata={"universe": {"candidate_raw_count": len(records), "accepted_common_count": count, "rejection_counts": rejection_counts, "duplicate_stock_ids": duplicate_count, "market_counts": market_counts, "pagination_complete": True, "latest_source_date": latest, "reconciliation": reconciliation}})
        return count
    except Exception as exc:
        db.rollback()
        code = getattr(exc, "code", "UNEXPECTED")
        expected = _expected_latest_source_date("TaiwanStockInfo", as_of or date.today())
        _mark_sync(db, "TaiwanStockInfo", "FAILED", 0, None, code, str(exc), fetched_at=_now(), expected_latest=expected, rows_received=len(records), rows_accepted=0, rows_rejected=0, stored_total=_stored_rows_total(db, "TaiwanStockInfo"), metadata={"universe": {"pagination_complete": False, "authoritative": False, "attempted_rows": len(records)}})
        raise


def sync_stock_dataset(db: Session, client: FinMindClient, dataset: str, stock_id: str, start_date: str, end_date: str) -> int:
    records, meta = client.fetch(dataset, stock_id, start_date, end_date)
    count = ingest_records(db, dataset, records)
    _mark_sync(db, dataset, "SUCCESS" if count else "NO_DATA", count, _as_date(meta.get("source_date")), "NO_DATA" if not count else None, fetched_at=_now())
    return count


_DATASET_MODELS = {
    "TaiwanStockInstitutionalInvestorsBuySellWide": InstitutionalDaily,
    "TaiwanStockShareholding": ForeignShareholdingDaily,
    "TaiwanStockHoldingSharesPer": HoldingDistribution,
    "TaiwanStockTradingDailyReport": BrokerDaily,
    "TaiwanStockTradingDailyReportSecIdAgg": BrokerDaily,
    "TaiwanStockPrice": PriceDaily,
    "TaiwanStockInfo": Stock,
}


def _stored_rows_total(db: Session, dataset: str) -> int:
    model = _DATASET_MODELS.get(dataset)
    if model is None:
        return 0
    query = select(func.count()).select_from(model)
    if model is Stock:
        query = query.where(Stock.is_common_stock.is_(True))
    return int(db.scalar(query) or 0)


def _expected_latest_source_date(dataset: str, as_of: date | None) -> date | None:
    if as_of is None:
        return None
    if dataset == "TaiwanStockHoldingSharesPer":
        # FinMind holding-distribution reports are weekly.  Friday is the
        # observed publication boundary for the current source contract.
        return as_of - timedelta(days=(as_of.weekday() - 4) % 7)
    return expected_trading_sessions(as_of, 1)[-1]


def _mark_sync(db: Session, dataset: str, status: str, records: int, latest: date | None, error_code: str | None = None, error: str | None = None, *, fetched_at: datetime | None = None, metadata: dict[str, Any] | None = None, rows_received: int | None = None, rows_accepted: int | None = None, rows_rejected: int | None = None, rows_versioned: int | None = None, stored_total: int | None = None, expected_latest: date | None = None) -> None:
    item = db.get(DataSyncStatus, dataset)
    if item is None:
        item = DataSyncStatus(dataset=dataset, status=status, records=records)
        db.add(item)
    item.status = status
    item.records = records
    item.usable_records = records
    item.stored_records = stored_total if stored_total is not None else _stored_rows_total(db, dataset)
    item.last_attempt_at = fetched_at or _now()
    item.attempt_latest_source_date = latest
    if latest is not None and (item.latest_source_date is None or latest > item.latest_source_date):
        item.latest_source_date = latest
    item.expected_latest_source_date = expected_latest
    item.source_age_days = (expected_latest - item.latest_source_date).days if expected_latest and item.latest_source_date else None
    item.rows_received_this_attempt = rows_received if rows_received is not None else records
    item.rows_accepted_this_attempt = rows_accepted if rows_accepted is not None else records
    item.rows_rejected_this_attempt = rows_rejected or 0
    item.rows_versioned_this_attempt = rows_versioned if rows_versioned is not None else records
    item.stored_rows_total = item.stored_records
    if status in {"SUCCESS", "REUSED", "PARTIAL", "NO_DATA"}:
        item.last_http_success_at = fetched_at or _now()
        item.last_fetch_at = fetched_at or _now()
    if status in {"SUCCESS", "REUSED"}:
        item.last_successful_sync = fetched_at or _now()
        item.last_fully_successful_sync = fetched_at or _now()
    if status in {"SUCCESS", "REUSED", "PARTIAL"} and records > 0:
        item.last_usable_data_at = fetched_at or _now()
    if status == "FAILED":
        item.staleness_state = "ERROR"
    elif status == "NO_DATA":
        item.staleness_state = "NO_DATA"
    elif item.latest_source_date is None:
        item.staleness_state = "PARTIAL"
    elif expected_latest and item.latest_source_date < expected_latest:
        item.staleness_state = "STALE"
    else:
        item.staleness_state = "FRESH"
    item.last_error_code = error_code
    item.last_error = error[:500] if error else None
    if metadata:
        item.metadata_json = {**(item.metadata_json or {}), **_jsonable(metadata)}
    db.commit()


def _natural_key(dataset: str, row: dict[str, Any]) -> str:
    values: dict[str, Any] = {
        "stock_id": row.get("stock_id"),
        "source_date": row.get("source_date") or row.get("date"),
    }
    if dataset == "TaiwanStockHoldingSharesPer":
        values["holding_shares_level"] = row.get("holding_shares_level")
    elif dataset == "TaiwanStockTradingDailyReport":
        values["securities_trader_id"] = row.get("securities_trader_id")
    return json.dumps(_jsonable(values), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _model_rows(db: Session, model: type[Any], stock_id: str, as_of: date, cutoff: datetime, dataset: str, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Return the latest point-in-time rows, merging revisions with legacy rows.

    SourceRevision was introduced after the first deployment, so a database can
    contain a partial revision history.  Revisions override the same natural
    key, while older model rows remain eligible when their fetch timestamp is
    before the requested cutoff.  This prevents a small incremental fetch from
    erasing the historical window used by a score.
    """
    session_dates: list[date] = []
    if dataset == "TaiwanStockTradingDailyReport":
        session_dates = list(db.scalars(select(model.source_date).where(model.stock_id == stock_id, model.source_date <= as_of, model.fetched_at <= cutoff).distinct().order_by(model.source_date.desc()).limit(20)).all())
    revision_query = select(SourceRevision).where(SourceRevision.dataset == dataset, SourceRevision.stock_id == stock_id, SourceRevision.source_date <= as_of, SourceRevision.fetched_at <= cutoff)
    if session_dates:
        revision_query = revision_query.where(SourceRevision.source_date.in_(session_dates))
    revisions = db.scalars(revision_query.order_by(SourceRevision.fetched_at, SourceRevision.id)).all()
    revision_by_key: dict[str, SourceRevision] = {}
    for revision in revisions:
        revision_by_key[revision.natural_key] = revision

    model_query = select(model).where(model.stock_id == stock_id, model.source_date <= as_of, model.fetched_at <= cutoff).order_by(model.source_date.desc(), model.id.desc())
    if session_dates:
        model_query = select(model).where(model.stock_id == stock_id, model.source_date.in_(session_dates), model.fetched_at <= cutoff).order_by(model.source_date.asc(), model.id.asc())
    model_rows = db.scalars(model_query.limit(limit if not session_dates else 100_000)).all()
    merged: dict[str, tuple[dict[str, Any], str, date | None, int]] = {}
    for row in model_rows:
        payload = {key: getattr(row, key) for key in row.__table__.columns.keys()}
        merged[_natural_key(dataset, payload)] = (payload, _payload_content_hash(payload), row.source_date, row.id)
    for revision in revision_by_key.values():
        payload = dict(revision.payload)
        merged[revision.natural_key] = (payload, _payload_content_hash(payload), revision.source_date, revision.id)

    selection_limit = 100_000 if session_dates else limit
    selected = sorted(merged.values(), key=lambda item: (item[2] or date.min, item[3]))[-selection_limit:]
    return [item[0] for item in selected], [item[1] for item in selected]


def _daily_coverage(rows: list[dict[str, Any]], as_of: date, count: int) -> tuple[bool, list[str]]:
    observed = []
    for row in rows:
        value = row.get("source_date") or row.get("date")
        parsed = _as_date(value)
        if parsed is not None:
            observed.append(parsed)
    missing = missing_sessions(observed, as_of, count)
    return not missing, missing


def calculate_stock_features_and_score(db: Session, stock_id: str, as_of: date | None = None, knowledge_cutoff: datetime | None = None) -> AccumulationScore:
    as_of = as_of or date.today()
    cutoff = knowledge_cutoff or _now()
    inst, inst_hashes = _model_rows(db, InstitutionalDaily, stock_id, as_of, cutoff, "TaiwanStockInstitutionalInvestorsBuySellWide", 20)
    foreign, foreign_hashes = _model_rows(db, ForeignShareholdingDaily, stock_id, as_of, cutoff, "TaiwanStockShareholding", 21)
    holdings, holding_hashes = _model_rows(db, HoldingDistribution, stock_id, as_of, cutoff, "TaiwanStockHoldingSharesPer", 100)
    brokers, broker_hashes = _model_rows(db, BrokerDaily, stock_id, as_of, cutoff, "TaiwanStockTradingDailyReport", 2000)
    prices, price_hashes = _model_rows(db, PriceDaily, stock_id, as_of, cutoff, "TaiwanStockPrice", 21)
    features = build_features(inst, foreign, holdings, brokers, prices)
    inst_ok, inst_missing = _daily_coverage(inst, as_of, 20)
    foreign_ok, foreign_missing = _daily_coverage(foreign, as_of, 21)
    price_ok, price_missing = _daily_coverage(prices, as_of, 21)
    broker_dates = {_as_date(row.get("source_date") or row.get("date")) for row in brokers}
    broker_dates.discard(None)
    expected_broker = expected_trading_sessions(as_of, 20)
    broker_missing = [day.isoformat() for day in expected_broker if day not in broker_dates]
    holding_coverage = features.get("HoldingDistributionCoverage") or {}
    required_validation = {
        "InstitutionalNet20D": {"expected_window": 20, "cadence": "trading_session", "present": features.get("InstitutionalNet20D") is not None, "valid": inst_ok and features.get("InstitutionalNet20D") is not None, "reason": "20 trading sessions with complete institutional net" if inst_ok and features.get("InstitutionalNet20D") is not None else "missing institutional session or null net"},
        "InstitutionalPositiveDayRatio20D": {"expected_window": 20, "cadence": "trading_session", "present": features.get("InstitutionalPositiveDayRatio20D") is not None, "valid": inst_ok and features.get("InstitutionalPositiveDayRatio20D") is not None, "reason": "20 trading sessions" if inst_ok and features.get("InstitutionalPositiveDayRatio20D") is not None else "missing institutional session or null net"},
        "InstitutionalNetSlope20D": {"expected_window": 20, "cadence": "trading_session", "present": features.get("InstitutionalNetSlope20D") is not None, "valid": inst_ok and features.get("InstitutionalNetSlope20D") is not None, "reason": "20 trading sessions" if inst_ok and features.get("InstitutionalNetSlope20D") is not None else "missing institutional session or null net"},
        "InstitutionalOneDaySpikeRatio20D": {"expected_window": 20, "cadence": "trading_session", "present": features.get("InstitutionalOneDaySpikeRatio20D") is not None, "valid": inst_ok and features.get("InstitutionalOneDaySpikeRatio20D") is not None, "reason": "20 trading sessions" if inst_ok and features.get("InstitutionalOneDaySpikeRatio20D") is not None else "missing institutional session or null net"},
        "ForeignShareRatioChange20D": {"expected_window": 21, "cadence": "trading_session", "present": features.get("ForeignShareRatioChange20D") is not None, "valid": foreign_ok and features.get("ForeignShareRatioChange20D") is not None, "reason": "21 trading sessions required for a 20-session change" if foreign_ok and features.get("ForeignShareRatioChange20D") is not None else "missing foreign holding session or ratio"},
        "LargeHolder400Change4W": {"expected_window": 4, "cadence": "weekly_publication", "present": features.get("LargeHolder400Change4W") is not None, "valid": bool(holding_coverage.get("available")) and features.get("LargeHolder400Change4W") is not None, "reason": "4-week holding observation" if bool(holding_coverage.get("available")) and features.get("LargeHolder400Change4W") is not None else "missing 4-week holding bucket"},
        "BrokerPersistenceScore": {"expected_window": 20, "cadence": "trading_session", "present": features.get("BrokerPersistenceScore") is not None, "valid": not broker_missing and features.get("BrokerPersistenceScore") is not None and features.get("BrokerDataContract", {}).get("available", True), "reason": "20 trading sessions with complete broker rows" if not broker_missing and features.get("BrokerPersistenceScore") is not None and features.get("BrokerDataContract", {}).get("available", True) else "missing broker session, null net, or unproven provider completeness"},
        "BrokerOneDaySpikeRatio20D": {"expected_window": 20, "cadence": "trading_session", "present": features.get("BrokerOneDaySpikeRatio20D") is not None, "valid": not broker_missing and features.get("BrokerOneDaySpikeRatio20D") is not None and features.get("BrokerDataContract", {}).get("available", True), "reason": "20 trading sessions" if not broker_missing and features.get("BrokerOneDaySpikeRatio20D") is not None and features.get("BrokerDataContract", {}).get("available", True) else "missing broker session, null net, or unproven provider completeness"},
        "PriceReturn20D": {"expected_window": 21, "cadence": "trading_session", "present": features.get("PriceReturn20D") is not None, "valid": price_ok and features.get("PriceReturn20D") is not None, "reason": "21 trading sessions required for a 20-session return" if price_ok and features.get("PriceReturn20D") is not None else "missing price session or close"},
    }
    coverage = {
        "InstitutionalDataAvailable": all(required_validation[key]["valid"] for key in ("InstitutionalNet20D", "InstitutionalPositiveDayRatio20D", "InstitutionalNetSlope20D", "InstitutionalOneDaySpikeRatio20D")),
        "ForeignHoldingDataAvailable": required_validation["ForeignShareRatioChange20D"]["valid"],
        "HoldingDistributionAvailable": required_validation["LargeHolder400Change4W"]["valid"],
        "BrokerDataAvailable": required_validation["BrokerPersistenceScore"]["valid"] and required_validation["BrokerOneDaySpikeRatio20D"]["valid"],
        "PriceDataAvailable": required_validation["PriceReturn20D"]["valid"],
        "RequiredFeatureValidation": required_validation,
        "missing_reasons": [f"{name}: {item['reason']}" for name, item in required_validation.items() if not item["valid"]],
        "calendar_version": CALENDAR_VERSION,
        "missing_sessions": {"institutional": inst_missing, "foreign_holding": foreign_missing, "broker": broker_missing, "price": price_missing},
        "holding_missing_weeks": holding_coverage.get("missing_weeks", []),
    }
    result = calculate_score(features, coverage)
    now = _now()
    input_hashes = sorted(inst_hashes + foreign_hashes + holding_hashes + broker_hashes + price_hashes)
    snapshot_hash = hashlib.sha256(json.dumps(input_hashes, separators=(",", ":")).encode()).hexdigest()
    s_dates = [str(row.get("source_date") or row.get("date")) for row in inst + foreign + holdings + brokers if row.get("source_date") or row.get("date")]
    feature_filters = {"stock_id": stock_id, "source_date": as_of, "knowledge_cutoff": cutoff}
    _upsert(db, AccumulationFeature, feature_filters, {"values": features, "coverage": coverage, "latest_source_date": max(s_dates, default=None), "calculated_at": now, "input_snapshot_hash": snapshot_hash})
    score_filters = {"stock_id": stock_id, "source_date": as_of, "score_version": SCORE_VERSION, "knowledge_cutoff": cutoff}
    score = _upsert(db, AccumulationScore, score_filters, {"score": result.score, "status": result.status, "components": result.components, "explanation": result.explanation, "coverage": coverage, "calculated_at": now, "input_snapshot_hash": snapshot_hash, "input_source_hashes": input_hashes, "formula_hash": FORMULA_HASH})
    db.commit()
    return score


def seed_score_version(db: Session) -> None:
    current = db.get(ScoreVersion, SCORE_VERSION)
    if current is None:
        db.add(ScoreVersion(version=SCORE_VERSION, config=SCORE_MANIFEST, manifest_hash=FORMULA_HASH, explanation="Canonical S-only v1 manifest; price/volume is supporting only.", created_at=_now()))
        db.commit()
    elif current.manifest_hash not in {None, FORMULA_HASH}:
        raise RuntimeError("score version manifest mismatch; deploy a new score version before starting")
    elif current.manifest_hash is None:
        raise RuntimeError("score version manifest provenance is missing; create an explicit new score version")


async def catch_up(db: Session, client: FinMindClient, end_date: date | None = None, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run the complete scheduled pipeline for the dynamic common-stock universe."""
    def progress(phase: str) -> None:
        if progress_callback:
            progress_callback(phase)

    end = end_date or date.today()
    start = expected_trading_sessions(end, 20)[0]
    result: dict[str, Any] = {"status": "SUCCESS", "datasets": {}, "scores": {}, "source_coverage": {}}
    required = ["TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding", "TaiwanStockHoldingSharesPer", "TaiwanStockPrice"]
    progress("TaiwanStockInfo")
    info_job = _job_start(db, "TaiwanStockInfo", end, end)
    try:
        info_count = sync_universe(db, client, as_of=end)
        _job_finish(db, info_job, "SUCCESS" if info_count else "PARTIAL", records=info_count, stocks_completed=info_count)
        result["datasets"]["TaiwanStockInfo"] = {"status": "SUCCESS" if info_count else "PARTIAL", "records": info_count}
    except Exception as exc:
        code = getattr(exc, "code", "UNEXPECTED")
        _job_finish(db, info_job, "FAILED", error_code=code, error=str(exc))
        result["datasets"]["TaiwanStockInfo"] = {"status": "FAILED", "error_code": code}
        result["status"] = "PARTIAL"
        result["fatal_code"] = code
        result["provider_work_deferred"] = {"reason": "dynamic universe refresh failed; later provider work and scoring were not launched", "error_code": code}
        return result
    stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))).all())
    for dataset in required:
        progress(dataset)
        job = _job_start(db, dataset, start, end, stocks_attempted=len(stock_ids))
        received = accepted = 0
        latest_dates: list[date] = []
        def sink(rows: list[dict[str, Any]]) -> int:
            nonlocal accepted
            accepted += ingest_records(db, dataset, rows)
            return accepted

        try:
            if hasattr(client, "fetch_stocks_dataset"):
                metrics = await client.fetch_stocks_dataset(stock_ids, dataset, (start - timedelta(days=30)).isoformat(), end.isoformat(), record_sink=sink)
                received = int(metrics.get("rows", 0))
                latest_dates = [_as_date(item.get("last_source_date")) for item in metrics.get("per_stock", {}).values() if item.get("last_source_date")]
                latest = max(latest_dates, default=None)
                newly_fetched = int(metrics.get("newly_fetched", metrics.get("success", 0)))
                reused_complete = int(metrics.get("reused_complete", 0))
                reused_valid_no_data = int(metrics.get("reused_valid_no_data", 0))
                valid_no_data = int(metrics.get("no_data", 0))
                retryable_pending = int(metrics.get("retryable_pending", 0))
                permanent_failed = int(metrics.get("permanent_failed", 0))
                physical_requests = int(metrics.get("physical_requests", 0))
                satisfied = not metrics.get("fatal_code") and retryable_pending == 0 and permanent_failed == 0 and int(metrics.get("success", 0)) >= len(stock_ids)
                status = "FAILED" if metrics.get("fatal_code") else ("REUSED" if satisfied and physical_requests == 0 and len(stock_ids) > 0 else ("SUCCESS" if satisfied else "PARTIAL"))
                code = metrics.get("fatal_code") or ("STOCK_PARTIAL" if not satisfied and (retryable_pending or permanent_failed) else None)
                coverage = {"requested": len(stock_ids), "success": int(metrics.get("success", 0)), "newly_fetched": newly_fetched, "reused_complete": reused_complete, "reused_valid_no_data": reused_valid_no_data, "valid_no_data": valid_no_data, "retryable_pending": retryable_pending, "permanent_failed": permanent_failed, "physical_requests": physical_requests, "failed": int(metrics.get("failed", 0)), "rows": received, "fatal_code": metrics.get("fatal_code"), "checkpoint_state": metrics.get("checkpoint_state"), "selection_policy": metrics.get("selection_policy")}
            else:
                records, meta = client.fetch(dataset, start_date=(start - timedelta(days=30)).isoformat(), end_date=end.isoformat())
                received = len(records)
                accepted = ingest_records(db, dataset, records)
                latest = _as_date(meta.get("source_date"))
                status = "SUCCESS" if accepted else "PARTIAL"
                code = None if accepted else "NO_DATA"
                coverage = {"mode": "fallback-broad", "requested": len(stock_ids), "rows": received}
            expected = _expected_latest_source_date(dataset, end)
            _mark_sync(db, dataset, status, accepted, latest, code, fetched_at=_now(), expected_latest=expected, rows_received=received, rows_accepted=accepted, rows_rejected=max(0, received - accepted), rows_versioned=accepted, stored_total=_stored_rows_total(db, dataset), metadata={"requested_start": (start - timedelta(days=30)).isoformat(), "requested_end": end.isoformat(), "query_mode": "per_stock_date_range" if hasattr(client, "fetch_stocks_dataset") else "fallback_broad", "coverage": coverage})
            _job_finish(db, job, status, records=accepted, stocks_completed=coverage.get("success", 0), stocks_failed=coverage.get("failed", 0), error_code=code, checkpoint_state=coverage)
            result["datasets"][dataset] = {"status": status, "records_received": received, "records_accepted": accepted, "stored_rows_total": _stored_rows_total(db, dataset), "coverage": coverage}
            result["source_coverage"][dataset] = coverage
            if status not in {"SUCCESS", "REUSED"}:
                result["status"] = "PARTIAL"
            if coverage.get("fatal_code"):
                result["fatal_code"] = coverage["fatal_code"]
                break
        except Exception as exc:
            db.rollback()
            code = getattr(exc, "code", "UNEXPECTED")
            _job_finish(db, job, "FAILED", error_code=code, error=str(exc), stocks_failed=len(stock_ids))
            _mark_sync(db, dataset, "FAILED", accepted, None, code, str(exc), fetched_at=_now(), expected_latest=_expected_latest_source_date(dataset, end), rows_received=received, rows_accepted=accepted, rows_rejected=max(0, received - accepted), stored_total=_stored_rows_total(db, dataset))
            result["datasets"][dataset] = {"status": "FAILED", "error_code": code}
            result["status"] = "PARTIAL"
            if code in GLOBAL_PROVIDER_FAILURE_CODES:
                result["fatal_code"] = code
                break
    fatal_code = result.get("fatal_code")
    if fatal_code:
        result["provider_work_deferred"] = {"reason": "global provider failure; later source and broker requests were not launched", "error_code": fatal_code}
        return result
    broker_start = expected_trading_sessions(end, 20)[0]
    progress("TaiwanStockTradingDailyReport")
    broker_job = _job_start(db, "TaiwanStockTradingDailyReport", broker_start, end, stocks_attempted=len(stock_ids))
    broker_metrics: dict[str, Any] = {}
    broker_buffer: list[dict[str, Any]] = []
    stored = 0

    def broker_sink(rows: list[dict[str, Any]]) -> int:
        nonlocal stored
        broker_buffer.extend(rows)
        if len(broker_buffer) >= 5000:
            stored += ingest_records(db, "TaiwanStockTradingDailyReport", broker_buffer[:])
            broker_buffer.clear()
        return stored

    try:
        broker_metrics = await client.fetch_broker_stocks(stock_ids, broker_start.isoformat(), end.isoformat(), record_sink=broker_sink)
        if broker_buffer:
            stored += ingest_records(db, "TaiwanStockTradingDailyReport", broker_buffer)
            broker_buffer.clear()
        checkpoint_complete = broker_metrics.get("skipped_checkpoint", 0) >= broker_metrics.get("requested_keys", len(stock_ids) * 20)
        no_work_reused = checkpoint_complete and broker_metrics.get("success", 0) == 0 and broker_metrics.get("rows", 0) == 0 and broker_metrics.get("failed", 0) == 0
        broker_status = "REUSED" if no_work_reused else ("SUCCESS" if broker_metrics.get("failed", 0) == 0 and (broker_metrics.get("rows", 0) > 0 or not stock_ids) else "PARTIAL")
        if no_work_reused:
            broker_metrics["reuse_reason"] = "all requested stock-session keys already completed; no new physical requests"
        broker_error = None if broker_status in {"SUCCESS", "REUSED"} else (broker_metrics.get("fatal_code") or "BROKER_PARTIAL")
        _mark_sync(db, "TaiwanStockTradingDailyReport", broker_status, stored, end if stored else None, broker_error, fetched_at=_now(), expected_latest=_expected_latest_source_date("TaiwanStockTradingDailyReport", end), rows_received=broker_metrics.get("rows", 0), rows_accepted=stored, rows_rejected=max(0, broker_metrics.get("rows", 0) - stored), rows_versioned=stored, stored_total=_stored_rows_total(db, "TaiwanStockTradingDailyReport"), metadata={"query_mode": "per_stock_per_session", **broker_metrics})
        _job_finish(db, broker_job, broker_status, records=stored, retry_count=broker_metrics.get("retries", 0), stocks_completed=broker_metrics.get("stocks_completed", 0), stocks_failed=broker_metrics.get("stocks_failed", 0), error_code=broker_error, checkpoint_state=broker_metrics)
        result["datasets"]["TaiwanStockTradingDailyReport"] = {**broker_metrics, "stored_records": stored, "status": broker_status}
        if broker_status not in {"SUCCESS", "REUSED"}:
            result["status"] = "PARTIAL"
        if broker_metrics.get("fatal_code"):
            result["fatal_code"] = broker_metrics["fatal_code"]
            result["provider_work_deferred"] = {
                "reason": "global provider failure; scoring was not launched after broker work stopped",
                "error_code": broker_metrics["fatal_code"],
            }
            return result
    except Exception as exc:
        db.rollback()
        code = getattr(exc, "code", "UNEXPECTED")
        _job_finish(db, broker_job, "FAILED", error_code=code, error=str(exc), stocks_failed=len(stock_ids))
        result["datasets"]["TaiwanStockTradingDailyReport"] = {"status": "FAILED", "error_code": code}
        result["status"] = "PARTIAL"
    existing_scores = db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == end, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None))).all()
    reuse_ready = len(existing_scores) >= len(stock_ids) and all(len(score.input_source_hashes or []) >= 20 for score in existing_scores)
    if broker_metrics.get("skipped_checkpoint", 0) >= len(stock_ids) * 20 and reuse_ready:
        score_job = _job_start(db, "score", end, end, stocks_attempted=len(stock_ids))
        _job_finish(db, score_job, "REUSED", stocks_completed=len(stock_ids), checkpoint_state={"reused_existing_scores": True})
        result["scores"] = {status: count for status, count in db.execute(select(AccumulationScore.status, func.count()).where(AccumulationScore.source_date == end, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None)).group_by(AccumulationScore.status)).all()}
        return result
    score_job = _job_start(db, "score", end, end, stocks_attempted=len(stock_ids))
    progress("score")
    for stock_id in stock_ids:
        try:
            score = calculate_stock_features_and_score(db, stock_id, end)
            result["scores"][score.status] = result["scores"].get(score.status, 0) + 1
        except Exception:
            result["status"] = "PARTIAL"
            result["scores"]["FAILED"] = result["scores"].get("FAILED", 0) + 1
    failures = result["scores"].get("FAILED", 0)
    _job_finish(db, score_job, "SUCCESS" if failures == 0 else "PARTIAL", stocks_completed=len(stock_ids) - failures, stocks_failed=failures, checkpoint_state={"scores": result["scores"]})
    return result


def _job_start(db: Session, dataset: str, start: date, end: date, stocks_attempted: int = 0) -> JobRun:
    job = JobRun(dataset=dataset, requested_date=end, requested_start_date=start, requested_end_date=end, status="RUNNING", started_at=_now(), stocks_attempted=stocks_attempted)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _job_finish(db: Session, job: JobRun, status: str, *, records: int = 0, retry_count: int = 0, stocks_completed: int = 0, stocks_failed: int = 0, error_code: str | None = None, error: str | None = None, checkpoint_state: dict[str, Any] | None = None) -> None:
    finished = _now()
    job.status = status
    job.finished_at = finished
    started = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=timezone.utc)
    job.duration_ms = max(0, int((finished - started).total_seconds() * 1000))
    job.records = records
    job.retry_count = retry_count
    job.stocks_completed = stocks_completed
    job.stocks_failed = stocks_failed
    job.error_code = error_code
    job.error = error[:500] if error else None
    job.checkpoint_state = checkpoint_state or {}
    db.commit()
