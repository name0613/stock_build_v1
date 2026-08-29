from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import uuid
from typing import Any, Callable

from sqlalchemy import func, inspect, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .calendar import CALENDAR_HASH, CALENDAR_VERSION, completed_source_end_date, expected_trading_sessions, missing_sessions
from .features import build_features
from .finmind import CAPABILITY_ONLY_DATASETS, GLOBAL_PROVIDER_FAILURE_CODES, FinMindClient, FinMindError, SchemaMismatch
from .models import (
    AccumulationFeature, AccumulationScore, BrokerDaily, DataSyncStatus, ForeignShareholdingDaily,
    HoldingDistribution, InstitutionalDaily, JobRun, PriceDaily, ScoreVersion, SourceRevision, Stock,
)
from .scoring import FORMULA_HASH, HOLDING_CANONICAL_THRESHOLDS, SCORE_MANIFEST, SCORE_VERSION, calculate_score, holding_schema_state

PIPELINE_ADVISORY_LOCK_KEY = 8_202_608_210_001


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
    if dataset != "TaiwanStockTradingDailyReport":
        raise FinMindError("CAPABILITY_ONLY_DATASET", f"{dataset} cannot enter production broker normalization")
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
    return {"stock_id": stock_id, "source_date": source_date, "securities_trader_id": trader_id, "securities_trader_name": _v(row, "securities_trader_name", "securities_trader", "券商名稱"), "buy_volume": buy, "sell_volume": sell, "net_volume": net_value, "buy_amount": buy * price if buy is not None and price is not None else _num(_v(row, "buy_amount", "買進金額")), "sell_amount": sell * price if sell is not None and price is not None else _num(_v(row, "sell_amount", "賣出金額")), "avg_buy_price": avg_buy if avg_buy is not None else price, "avg_sell_price": avg_sell if avg_sell is not None else price, "source_dataset": dataset, "provider_report_complete": False, "provider_contract_version": None, "provider_row_validated": _v(row, "provider_row_validated") is True, "provider_row_contract_version": _v(row, "provider_row_contract_version"), "fetched_at": fetched_at or _now()}


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
    if dataset in CAPABILITY_ONLY_DATASETS:
        raise FinMindError("CAPABILITY_ONLY_DATASET", f"{dataset} is probe-only and cannot enter production ingestion")
    if dataset == "TaiwanStockHoldingSharesPer":
        seen_buckets: set[tuple[str, str, str]] = set()
        parsed_records: list[tuple[str, str, int | None, Any]] = []
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
            parsed_records.append((stock, source_day, int(parsed_threshold) if parsed_threshold is not None else None, level))
        scopes = {(stock, parsed_day) for stock, source_day, _, _ in parsed_records if stock and (parsed_day := _as_date(source_day)) is not None}
        existing_rows = db.scalars(
            select(HoldingDistribution).where(
                tuple_(HoldingDistribution.stock_id, HoldingDistribution.source_date).in_(scopes)
            )
        ).all() if scopes else []
        existing_by_bucket = {(row.stock_id, row.source_date.isoformat(), row.holding_shares_threshold): row for row in existing_rows if row.holding_shares_threshold is not None}
        for stock, source_day, parsed_threshold, level in parsed_records:
            if parsed_threshold is None:
                continue
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
    if dataset == "TaiwanStockTradingDailyReport":
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
        elif dataset == "TaiwanStockTradingDailyReport":
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
        _mark_sync(db, "TaiwanStockInfo", "FAILED", 0, None, code, str(exc), fetched_at=_now(), expected_latest=expected, rows_received=len(records), rows_accepted=0, rows_rejected=len(records), stored_total=_stored_rows_total(db, "TaiwanStockInfo"), metadata={"universe": {"pagination_complete": False, "authoritative": False, "attempted_rows": len(records)}})
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
    "TaiwanStockPrice": PriceDaily,
    "TaiwanStockInfo": Stock,
}


def prioritize_stock_ids(db: Session, stock_ids: list[str], dataset: str) -> list[str]:
    """Return a durable per-dataset refresh order.

    Stocks with no persisted rows are always first. The remaining stocks are
    ordered by their oldest persisted write time, then source date and code.
    The worker passes this order into the checkpointed FinMind client, which
    resumes from its durable cursor and therefore cycles fairly as quota is
    consumed.
    """
    unique_ids = list(dict.fromkeys(str(stock_id) for stock_id in stock_ids))
    if len(unique_ids) <= 1:
        return unique_ids
    model = _DATASET_MODELS.get(dataset)
    if model is None:
        return unique_ids
    query = select(model.stock_id, func.max(model.source_date), func.max(model.fetched_at)).where(model.stock_id.in_(unique_ids))
    if model is Stock:
        query = query.where(Stock.is_common_stock.is_(True))
    else:
        query = query.where(model.source_dataset == dataset)
    persisted = {
        str(stock_id): (latest_source_date, latest_fetched_at)
        for stock_id, latest_source_date, latest_fetched_at in db.execute(query.group_by(model.stock_id)).all()
    }

    def sort_key(stock_id: str) -> tuple[int, str, str, str]:
        latest_source_date, latest_fetched_at = persisted.get(stock_id, (None, None))
        has_data = 1 if stock_id in persisted else 0
        # ISO strings preserve chronological ordering for date/datetime values
        # and avoid mixing timezone-aware and naive database timestamps.
        fetched_key = latest_fetched_at.isoformat() if latest_fetched_at is not None else ""
        source_key = latest_source_date.isoformat() if latest_source_date is not None else ""
        return has_data, fetched_key, source_key, stock_id

    return sorted(unique_ids, key=sort_key)


def _stored_rows_total(db: Session, dataset: str) -> int:
    model = _DATASET_MODELS.get(dataset)
    if model is None:
        return 0
    query = select(func.count()).select_from(model)
    if model is Stock:
        query = query.where(Stock.is_common_stock.is_(True))
    elif hasattr(model, "source_dataset"):
        query = query.where(model.source_dataset == dataset)
    return int(db.scalar(query) or 0)


def _pending_broker_rebuild_stock_ids(db: Session) -> list[str]:
    """Return migration-quarantined stocks that still need official rebuild."""
    if not inspect(db.get_bind()).has_table("broker_source_affected_stocks"):
        return []
    return list(db.scalars(text(
        "SELECT stock_id FROM broker_source_affected_stocks "
        "WHERE remediation_state <> 'REBUILT_FROM_OFFICIAL_SOURCE' ORDER BY stock_id"
    )).all())


def _mark_broker_rebuilds_complete(db: Session, stock_ids: list[str], as_of: date) -> list[str]:
    """Close quarantine records only after official history and score rebuild."""
    if not stock_ids or not inspect(db.get_bind()).has_table("broker_source_affected_stocks"):
        return []
    expected = set(expected_trading_sessions(as_of, 20))
    completed: list[str] = []
    for stock_id in stock_ids:
        observed = set(db.scalars(select(BrokerDaily.source_date).where(
            BrokerDaily.stock_id == stock_id,
            BrokerDaily.source_dataset == "TaiwanStockTradingDailyReport",
            BrokerDaily.source_date.in_(expected),
        )).all())
        score_exists = db.scalar(select(AccumulationScore.id).where(
            AccumulationScore.stock_id == stock_id,
            AccumulationScore.source_date == as_of,
            AccumulationScore.score_version == SCORE_VERSION,
            AccumulationScore.knowledge_cutoff.is_not(None),
        ).limit(1)) is not None
        if observed == expected and score_exists:
            db.execute(text(
                "UPDATE broker_source_affected_stocks "
                "SET remediation_state = 'REBUILT_FROM_OFFICIAL_SOURCE', remediated_at = :remediated_at "
                "WHERE stock_id = :stock_id"
            ), {"stock_id": stock_id, "remediated_at": _now()})
            completed.append(stock_id)
    db.commit()
    return completed


def _expected_latest_source_date(dataset: str, as_of: date | None) -> date | None:
    if as_of is None:
        return None
    if dataset == "TaiwanStockHoldingSharesPer":
        # FinMind holding-distribution reports are weekly.  Friday is the
        # observed publication boundary for the current source contract.
        return as_of - timedelta(days=(as_of.weekday() - 4) % 7)
    return expected_trading_sessions(as_of, 1)[-1]


def authoritative_expected_latest_source_date(dataset: str, now: datetime | None = None) -> date | None:
    """Return the current freshness target, independent of an attempt target.

    ``catch_up(end_date=...)`` may intentionally backfill an older date.  That
    date is attempt provenance only; current readiness must follow the
    publication/calendar policy at evaluation time.  Holding distribution is
    still projected through its weekly publication rule by
    ``_expected_latest_source_date``.
    """
    return _expected_latest_source_date(dataset, completed_source_end_date(now))


SYNC_COUNTER_SEMANTICS_VERSION = "attempt-v5-reconciled-v1"


def _mark_sync(db: Session, dataset: str, status: str, records: int, latest: date | None, error_code: str | None = None, error: str | None = None, *, fetched_at: datetime | None = None, metadata: dict[str, Any] | None = None, rows_received: int | None = None, rows_accepted: int | None = None, rows_rejected: int | None = None, rows_versioned: int | None = None, observations_reused: int = 0, physical_requests: int = 1, stored_total: int | None = None, expected_latest: date | None = None) -> None:
    received_count = rows_received if rows_received is not None else records
    accepted_count = rows_accepted if rows_accepted is not None else records
    rejected_count = rows_rejected or 0
    versioned_count = rows_versioned if rows_versioned is not None else accepted_count
    if min(received_count, accepted_count, rejected_count, versioned_count, observations_reused, physical_requests) < 0:
        raise ValueError("sync counters cannot be negative")
    if accepted_count + rejected_count != received_count:
        raise ValueError("sync counters do not reconcile: accepted + rejected must equal received")
    if versioned_count > accepted_count:
        raise ValueError("sync counters do not reconcile: versioned exceeds accepted")
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
    current_expected = authoritative_expected_latest_source_date(dataset)
    # The caller's value is derived from the attempt end date and is retained
    # only as a compatibility fallback.  Never let a historical attempt move
    # the durable authoritative expectation backwards.
    candidate_expected = current_expected or expected_latest
    if item.expected_latest_source_date and candidate_expected:
        candidate_expected = max(item.expected_latest_source_date, candidate_expected)
    item.expected_latest_source_date = candidate_expected
    item.source_age_days = (candidate_expected - item.latest_source_date).days if candidate_expected and item.latest_source_date else None
    item.rows_received_this_attempt = received_count
    item.rows_accepted_this_attempt = accepted_count
    item.rows_rejected_this_attempt = rejected_count
    item.rows_versioned_this_attempt = versioned_count
    item.observations_reused_this_attempt = observations_reused
    item.physical_requests_this_attempt = physical_requests
    item.stored_rows_total = item.stored_records
    item.counter_attempt_id = uuid.uuid4().hex
    item.counter_semantics_version = SYNC_COUNTER_SEMANTICS_VERSION
    item.counters_are_current_attempt = True
    if status in {"SUCCESS", "REUSED", "PARTIAL", "NO_DATA", "WAITING_FOR_PROVIDER_PUBLICATION"}:
        item.last_http_success_at = fetched_at or _now()
        item.last_fetch_at = fetched_at or _now()
    if status in {"SUCCESS", "REUSED"}:
        item.last_successful_sync = fetched_at or _now()
        item.last_fully_successful_sync = fetched_at or _now()
    if status in {"SUCCESS", "REUSED", "PARTIAL"} and records > 0:
        item.last_usable_data_at = fetched_at or _now()
    if status == "FAILED":
        item.staleness_state = "ERROR"
    elif status == "WAITING_FOR_PROVIDER_PUBLICATION":
        item.staleness_state = "WAITING_FOR_PROVIDER_PUBLICATION"
    elif status == "QUOTA_EXHAUSTED":
        item.staleness_state = "QUOTA_EXHAUSTED"
    elif status == "NO_DATA":
        item.staleness_state = "NO_DATA"
    elif status == "PARTIAL":
        item.staleness_state = "PARTIAL"
    elif item.latest_source_date is None:
        item.staleness_state = "PARTIAL"
    elif candidate_expected and item.latest_source_date < candidate_expected:
        item.staleness_state = "STALE"
    else:
        item.staleness_state = "FRESH"
    item.last_error_code = error_code
    item.last_error = error[:500] if error else None
    item.metadata_json = {
        **(item.metadata_json or {}),
        **_jsonable(metadata or {}),
        "counter_contract": {
            "version": SYNC_COUNTER_SEMANTICS_VERSION,
            "attempt_id": item.counter_attempt_id,
            "reconciliation": "rows_received = rows_accepted + rows_rejected; rows_versioned <= rows_accepted",
            "historical_pre_v5_snapshot_preserved": bool((item.metadata_json or {}).get("legacy_pre_v5_counter_snapshot")),
        },
    }
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
    source_bound = hasattr(model, "source_dataset")
    if dataset == "TaiwanStockTradingDailyReport":
        session_query = select(model.source_date).where(model.stock_id == stock_id, model.source_date <= as_of, model.fetched_at <= cutoff)
        if source_bound:
            session_query = session_query.where(model.source_dataset == dataset)
        session_dates = list(db.scalars(session_query.distinct().order_by(model.source_date.desc()).limit(20)).all())
    revision_query = select(SourceRevision).where(SourceRevision.dataset == dataset, SourceRevision.stock_id == stock_id, SourceRevision.source_date <= as_of, SourceRevision.fetched_at <= cutoff)
    if session_dates:
        revision_query = revision_query.where(SourceRevision.source_date.in_(session_dates))
    revisions = db.scalars(revision_query.order_by(SourceRevision.fetched_at, SourceRevision.id)).all()
    revision_by_key: dict[str, SourceRevision] = {}
    for revision in revisions:
        # Older deployments persisted the same natural key with a different
        # JSON field order.  Rebuild the canonical key from the normalized
        # payload before merging revisions with the typed model rows; without
        # this, one observation could consume two slots in a 20/21-day window.
        try:
            canonical_key = _natural_key(dataset, revision.payload)
        except (AttributeError, TypeError, ValueError):
            canonical_key = revision.natural_key
        revision_by_key[canonical_key] = revision

    model_query = select(model).where(model.stock_id == stock_id, model.source_date <= as_of, model.fetched_at <= cutoff)
    if source_bound:
        model_query = model_query.where(model.source_dataset == dataset)
    model_query = model_query.order_by(model.source_date.desc(), model.id.desc())
    if session_dates:
        model_query = select(model).where(model.stock_id == stock_id, model.source_date.in_(session_dates), model.fetched_at <= cutoff)
        if source_bound:
            model_query = model_query.where(model.source_dataset == dataset)
        model_query = model_query.order_by(model.source_date.asc(), model.id.asc())
    model_rows = db.scalars(model_query.limit(limit if not session_dates else 100_000)).all()
    merged: dict[str, tuple[dict[str, Any], str, date | None, int]] = {}
    for row in model_rows:
        payload = {key: getattr(row, key) for key in row.__table__.columns.keys()}
        merged[_natural_key(dataset, payload)] = (payload, _payload_content_hash(payload), row.source_date, row.id)
    for canonical_key, revision in revision_by_key.items():
        payload = dict(revision.payload)
        merged[canonical_key] = (payload, _payload_content_hash(payload), revision.source_date, revision.id)

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


def _evaluate_stock_inputs(db: Session, stock_id: str, as_of: date, knowledge_cutoff: datetime) -> dict[str, Any]:
    """Evaluate one stock against the authoritative scoring contract.

    This is deliberately side-effect free.  Both the production scoring loop
    and the readiness audit use this function so that a stock cannot be
    reported as ready by an audit while being rejected by scoring (or vice
    versa).  The point-in-time cutoff is supplied by the caller and is shared
    by a complete scoring run.
    """
    inst, inst_hashes = _model_rows(db, InstitutionalDaily, stock_id, as_of, knowledge_cutoff, "TaiwanStockInstitutionalInvestorsBuySellWide", 20)
    foreign, foreign_hashes = _model_rows(db, ForeignShareholdingDaily, stock_id, as_of, knowledge_cutoff, "TaiwanStockShareholding", 21)
    holdings, holding_hashes = _model_rows(db, HoldingDistribution, stock_id, as_of, knowledge_cutoff, "TaiwanStockHoldingSharesPer", 100)
    brokers, broker_hashes = _model_rows(db, BrokerDaily, stock_id, as_of, knowledge_cutoff, "TaiwanStockTradingDailyReport", 2000)
    prices, price_hashes = _model_rows(db, PriceDaily, stock_id, as_of, knowledge_cutoff, "TaiwanStockPrice", 21)
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
        "BrokerPersistenceScore": {"expected_window": 20, "cadence": "trading_session", "present": features.get("BrokerPersistenceScore") is not None, "valid": not broker_missing and features.get("BrokerPersistenceScore") is not None and features.get("BrokerDataContract", {}).get("available", True), "reason": "20 sessions of schema-valid observed broker rows; omitted branches remain unknown" if not broker_missing and features.get("BrokerPersistenceScore") is not None and features.get("BrokerDataContract", {}).get("available", True) else "missing broker session, invalid row, or unproven observed-row contract"},
        "PriceReturn20D": {"expected_window": 21, "cadence": "trading_session", "present": features.get("PriceReturn20D") is not None, "valid": price_ok and features.get("PriceReturn20D") is not None, "reason": "21 trading sessions required for a 20-session return" if price_ok and features.get("PriceReturn20D") is not None else "missing price session or close"},
    }
    coverage = {
        "InstitutionalDataAvailable": all(required_validation[key]["valid"] for key in ("InstitutionalNet20D", "InstitutionalPositiveDayRatio20D", "InstitutionalNetSlope20D", "InstitutionalOneDaySpikeRatio20D")),
        "ForeignHoldingDataAvailable": required_validation["ForeignShareRatioChange20D"]["valid"],
        "HoldingDistributionAvailable": required_validation["LargeHolder400Change4W"]["valid"],
        "BrokerDataAvailable": required_validation["BrokerPersistenceScore"]["valid"],
        "PriceDataAvailable": required_validation["PriceReturn20D"]["valid"],
        "RequiredFeatureValidation": required_validation,
        "missing_reasons": [f"{name}: {item['reason']}" for name, item in required_validation.items() if not item["valid"]],
        "calendar_version": CALENDAR_VERSION,
        "missing_sessions": {"institutional": inst_missing, "foreign_holding": foreign_missing, "broker": broker_missing, "price": price_missing},
        "holding_missing_weeks": holding_coverage.get("missing_weeks", []),
    }
    input_hashes = sorted(inst_hashes + foreign_hashes + holding_hashes + broker_hashes + price_hashes)
    s_dates = [str(row.get("source_date") or row.get("date")) for row in inst + foreign + holdings + brokers if row.get("source_date") or row.get("date")]
    readiness_reason_codes: list[str] = []
    if not coverage["InstitutionalDataAvailable"]:
        readiness_reason_codes.append("missing_institutional")
    if not coverage["ForeignHoldingDataAvailable"]:
        readiness_reason_codes.append("missing_foreign_holding")
    if not coverage["HoldingDistributionAvailable"]:
        readiness_reason_codes.append("tdcc_required_buckets_incomplete")
    if not coverage["BrokerDataAvailable"]:
        readiness_reason_codes.append("missing_broker")
    if not coverage["PriceDataAvailable"]:
        readiness_reason_codes.append("missing_price")
    # The detailed session lists are the authoritative explanation for a
    # stock-level miss.  Keep a stable top-level code for machine-auditable
    # metrics while retaining the existing human-readable reasons.
    coverage["readiness_reason_codes"] = readiness_reason_codes
    score_result = calculate_score(features, coverage)
    return {
        "stock_id": stock_id,
        "as_of": as_of,
        "knowledge_cutoff": knowledge_cutoff,
        "features": features,
        "coverage": coverage,
        "score_result": score_result,
        "input_hashes": input_hashes,
        "snapshot_hash": hashlib.sha256(json.dumps(input_hashes, separators=(",", ":")).encode()).hexdigest(),
        "latest_source_date": max(s_dates, default=None),
        "ready": score_result.score is not None,
        "missing_reasons": readiness_reason_codes,
    }


def evaluate_stock_readiness(db: Session, stock_id: str, as_of: date, knowledge_cutoff: datetime | None = None) -> dict[str, Any]:
    """Return deterministic, side-effect-free readiness for one stock."""
    evaluation = _evaluate_stock_inputs(db, stock_id, as_of, knowledge_cutoff or _now())
    return {
        "stock_id": stock_id,
        "ready": evaluation["ready"],
        "missing_reasons": list(evaluation["missing_reasons"]),
        "coverage": evaluation["coverage"],
        "source_date": as_of.isoformat(),
        "knowledge_cutoff": evaluation["knowledge_cutoff"].isoformat(),
    }


def evaluate_universe_readiness(db: Session, stock_ids: list[str], as_of: date, knowledge_cutoff: datetime | None = None) -> dict[str, Any]:
    """Audit every eligible stock without creating feature or score rows."""
    cutoff = knowledge_cutoff or _now()
    items: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for stock_id in stock_ids:
        item = evaluate_stock_readiness(db, stock_id, as_of, cutoff)
        items.append(item)
        for reason in item["missing_reasons"]:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    ready = sum(1 for item in items if item["ready"])
    return {
        "source_date": as_of.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "evaluated_stock_count": len(items),
        "ready_stock_count": ready,
        "not_ready_stock_count": len(items) - ready,
        "accounting_invariant": ready + (len(items) - ready) == len(items),
        "missing_reason_counts": reason_counts,
        "items": items,
    }


def _persist_stock_evaluation(db: Session, evaluation: dict[str, Any]) -> AccumulationScore:
    """Persist one evaluated stock, including explicit fail-closed results."""
    now = _now()
    stock_id = evaluation["stock_id"]
    as_of = evaluation["as_of"]
    cutoff = evaluation["knowledge_cutoff"]
    feature_filters = {"stock_id": stock_id, "source_date": as_of, "knowledge_cutoff": cutoff}
    _upsert(db, AccumulationFeature, feature_filters, {"values": evaluation["features"], "coverage": evaluation["coverage"], "latest_source_date": evaluation["latest_source_date"], "calculated_at": now, "input_snapshot_hash": evaluation["snapshot_hash"]})
    result = evaluation["score_result"]
    score_filters = {"stock_id": stock_id, "source_date": as_of, "score_version": SCORE_VERSION, "knowledge_cutoff": cutoff}
    score = _upsert(db, AccumulationScore, score_filters, {"score": result.score, "status": result.status, "components": result.components, "explanation": result.explanation, "coverage": evaluation["coverage"], "calculated_at": now, "input_snapshot_hash": evaluation["snapshot_hash"], "input_source_hashes": evaluation["input_hashes"], "formula_hash": FORMULA_HASH})
    db.commit()
    return score


def calculate_stock_features_and_score(db: Session, stock_id: str, as_of: date | None = None, knowledge_cutoff: datetime | None = None) -> AccumulationScore:
    """Evaluate and persist one stock using the shared readiness source of truth."""
    evaluation = _evaluate_stock_inputs(db, stock_id, as_of or date.today(), knowledge_cutoff or _now())
    return _persist_stock_evaluation(db, evaluation)


TARGETED_STOCK_SYNC_DATASET = "targeted_stock_sync_score"
TARGETED_SCORE_FALLBACK_SESSIONS = 20


def _targeted_readiness_payload(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Return JSON-safe readiness evidence for a targeted stock run."""
    return {
        "stock_id": evaluation["stock_id"],
        "ready": bool(evaluation["ready"]),
        "missing_reasons": list(evaluation["missing_reasons"]),
        "coverage": evaluation["coverage"],
        "source_date": evaluation["as_of"].isoformat(),
        "knowledge_cutoff": evaluation["knowledge_cutoff"].isoformat(),
    }


def latest_ready_stock_evaluation(
    db: Session,
    stock_id: str,
    target: date,
    knowledge_cutoff: datetime,
    *,
    initial_evaluation: dict[str, Any] | None = None,
    max_fallback_sessions: int = TARGETED_SCORE_FALLBACK_SESSIONS,
) -> tuple[dict[str, Any], bool]:
    """Find the newest complete evaluation at or before ``target``.

    Daily FinMind datasets can publish at different times.  A targeted run
    first attempts the current target, then falls back to the newest prior
    trading session whose complete point-in-time inputs are already persisted.
    The returned boolean records whether that fallback was needed.
    """
    current = initial_evaluation or _evaluate_stock_inputs(db, stock_id, target, knowledge_cutoff)
    if current["ready"]:
        return current, False
    sessions = expected_trading_sessions(target, max(1, max_fallback_sessions + 1))
    for candidate in reversed(sessions[:-1]):
        evaluation = _evaluate_stock_inputs(db, stock_id, candidate, knowledge_cutoff)
        if evaluation["ready"]:
            return evaluation, True
    return current, False


async def fetch_and_score_stock(
    db: Session,
    client: FinMindClient,
    stock_id: str,
    as_of: date | None = None,
    *,
    job: JobRun | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch one stock's missing scoring inputs, then score it immediately.

    The provider client owns durable observation checkpoints.  Each request
    therefore receives the complete scoring window, while already-covered
    observations are reused and only unresolved observations consume provider
    calls.  A failed source fetch never fabricates zeros: the final local
    evaluation still records the exact remaining readiness reasons.
    """
    stock_id = str(stock_id).strip()
    stock = db.get(Stock, stock_id)
    if stock is None or not stock.is_common_stock:
        raise ValueError("stock not found")
    target = as_of or completed_source_end_date(_now())
    score_cutoff = _now()
    score_job = job or _job_start(db, TARGETED_STOCK_SYNC_DATASET, target, target, stocks_attempted=1)
    datasets: dict[str, Any] = {}
    fetch_errors: list[dict[str, str]] = []

    def checkpoint(phase: str, **extra: Any) -> None:
        state = score_job.checkpoint_state if isinstance(score_job.checkpoint_state, dict) else {}
        score_job.checkpoint_state = {
            **state,
            "run_mode": "targeted_fetch_and_score",
            "stock_id": stock_id,
            "target_date": target.isoformat(),
            "phase": phase,
            "datasets": datasets,
            **extra,
        }
        db.commit()
        if progress_callback:
            progress_callback(phase)

    pre_evaluation = _evaluate_stock_inputs(db, stock_id, target, score_cutoff)
    pre_readiness = _targeted_readiness_payload(pre_evaluation)
    checkpoint("preflight", pre_readiness=pre_readiness, progress={"completed": 0, "total": 5})

    # A single quota probe is recorded before any uncached provider work.  It
    # is advisory here; the broker client still applies its own reserve-aware
    # key selection, and all quota fields are sanitized to non-secret values.
    quota: dict[str, Any] = {"status": "NOT_CONFIGURED"}
    quota_probe = getattr(client, "provider_quota", None)
    client_settings = getattr(client, "settings", None)
    if pre_evaluation["missing_reasons"] and callable(quota_probe) and getattr(client_settings, "finmind_api_token", None):
        try:
            raw_quota = quota_probe(source_revision=getattr(client_settings, "source_revision", "runtime"))
            quota = {
                "status": "PASS",
                "remaining": raw_quota.get("provider_reported_remaining"),
                "limit_per_hour": raw_quota.get("provider_reported_limit_per_hour"),
                "plan": raw_quota.get("plan"),
            }
        except FinMindError as exc:
            quota = {"status": "FAILED", "error_code": exc.code}
            fetch_errors.append({"dataset": "provider_quota", "error_code": exc.code})
    checkpoint("quota_checked", quota=quota, pre_readiness=pre_readiness, progress={"completed": 0, "total": 5})

    expected_daily = {
        "TaiwanStockInstitutionalInvestorsBuySellWide": (20, "missing_institutional"),
        "TaiwanStockShareholding": (21, "missing_foreign_holding"),
        "TaiwanStockPrice": (21, "missing_price"),
    }
    plan: list[tuple[str, str, date, date, str]] = []
    for dataset, (window, reason) in expected_daily.items():
        start = expected_trading_sessions(target, window)[0]
        plan.append((dataset, reason, start, target, "source"))
    holding_target = _expected_latest_source_date("TaiwanStockHoldingSharesPer", target) or target
    plan.append(("TaiwanStockHoldingSharesPer", "tdcc_required_buckets_incomplete", holding_target - timedelta(days=56), holding_target, "source"))
    broker_start = expected_trading_sessions(target, 20)[0]
    plan.append(("TaiwanStockTradingDailyReport", "missing_broker", broker_start, target, "broker"))

    completed = 0
    for dataset, reason, start, end, method in plan:
        completed += 1
        if reason not in set(pre_evaluation["missing_reasons"]):
            datasets[dataset] = {"status": "REUSED_LOCAL", "physical_requests": 0, "reason": "stock readiness already satisfied before fetch"}
            checkpoint(f"reused:{dataset}", pre_readiness=pre_readiness, quota=quota, progress={"completed": completed, "total": len(plan)})
            continue
        checkpoint(f"fetching:{dataset}", pre_readiness=pre_readiness, quota=quota, progress={"completed": completed - 1, "total": len(plan)})
        accepted = 0
        versioned = 0

        def sink(rows: list[dict[str, Any]]) -> dict[str, Any]:
            nonlocal accepted, versioned
            before = int(db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == dataset)) or 0)
            accepted_now = ingest_records(db, dataset, rows)
            after = int(db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == dataset)) or 0)
            accepted += accepted_now
            versioned += max(0, after - before)
            dates = sorted({value for value in (_as_date(_v(row, "date", "source_date")) for row in rows) if value is not None})
            return {"accepted_count": accepted_now, "versioned_count": max(0, after - before), "accepted_dates": [value.isoformat() for value in dates]}

        try:
            def provider_progress(message: str) -> None:
                if progress_callback:
                    progress_callback(f"{dataset}:{message}")

            if method == "broker":
                metrics = await client.fetch_broker_stocks([stock_id], start.isoformat(), end.isoformat(), record_sink=sink, progress_callback=provider_progress, retry_deferred=True)
            else:
                metrics = await client.fetch_stocks_dataset([stock_id], dataset, start.isoformat(), end.isoformat(), record_sink=sink, progress_callback=provider_progress, retry_provider_missing=True)
            datasets[dataset] = {**metrics, "records_accepted": accepted, "rows_versioned": versioned}
            fatal = metrics.get("fatal_code") if isinstance(metrics, dict) else None
            if fatal:
                fetch_errors.append({"dataset": dataset, "error_code": str(fatal)})
                if fatal in GLOBAL_PROVIDER_FAILURE_CODES:
                    checkpoint("provider_blocked", datasets=datasets, fetch_errors=fetch_errors, quota=quota, progress={"completed": completed, "total": len(plan)})
                    break
        except FinMindError as exc:
            datasets[dataset] = {"status": "FAILED", "error_code": exc.code, "records_accepted": accepted, "rows_versioned": versioned}
            fetch_errors.append({"dataset": dataset, "error_code": exc.code})
            if exc.code in GLOBAL_PROVIDER_FAILURE_CODES:
                checkpoint("provider_blocked", datasets=datasets, fetch_errors=fetch_errors, quota=quota, progress={"completed": completed, "total": len(plan)})
                break
        except Exception as exc:
            code = getattr(exc, "code", "UNEXPECTED")
            datasets[dataset] = {"status": "FAILED", "error_code": code, "records_accepted": accepted, "rows_versioned": versioned}
            fetch_errors.append({"dataset": dataset, "error_code": code})
        checkpoint(f"fetched:{dataset}", datasets=datasets, fetch_errors=fetch_errors, quota=quota, progress={"completed": completed, "total": len(plan)})

    checkpoint("scoring", datasets=datasets, fetch_errors=fetch_errors, quota=quota, progress={"completed": len(plan), "total": len(plan)})
    evaluation_cutoff = _now()
    target_evaluation = _evaluate_stock_inputs(db, stock_id, target, evaluation_cutoff)
    final_evaluation, fallback_applied = latest_ready_stock_evaluation(
        db,
        stock_id,
        target,
        evaluation_cutoff,
        initial_evaluation=target_evaluation,
    )
    score = _persist_stock_evaluation(db, final_evaluation)
    readiness = _targeted_readiness_payload(final_evaluation)
    status = "SUCCESS" if final_evaluation["ready"] else "DATA_INSUFFICIENT"
    result = {
        "job_id": score_job.id,
        "stock_id": stock_id,
        "target_date": target.isoformat(),
        "evaluated_source_date": final_evaluation["as_of"].isoformat(),
        "fallback_applied": fallback_applied,
        "fallback_reason": "TARGET_DATE_SOURCE_INCOMPLETE" if fallback_applied else None,
        "status": status,
        "score": {"score": score.score, "status": score.status, "score_version": score.score_version, "formula_hash": score.formula_hash, "coverage": score.coverage, "components": score.components, "explanation": score.explanation, "source_date": score.source_date.isoformat() if score.source_date else None, "knowledge_cutoff": score.knowledge_cutoff.isoformat() if score.knowledge_cutoff else None, "calculated_at": score.calculated_at.isoformat() if score.calculated_at else None},
        "pre_readiness": pre_readiness,
        "readiness": readiness,
        "datasets": datasets,
        "fetch_errors": fetch_errors,
        "quota": quota,
    }
    _job_finish(db, score_job, status, records=sum(int(item.get("records_accepted", 0)) for item in datasets.values()), stocks_completed=1 if final_evaluation["ready"] else 0, stocks_failed=0, error_code=(fetch_errors[0]["error_code"] if fetch_errors else None), checkpoint_state=_jsonable({"run_mode": "targeted_fetch_and_score", **result, "phase": "completed", "progress": {"completed": len(plan), "total": len(plan)}}))
    return result


def score_existing_data(
    db: Session,
    as_of: date,
    stock_ids: list[str] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    job: JobRun | None = None,
) -> dict[str, Any]:
    """Score persisted inputs only; never call FinMind or refresh source data.

    This powers the manual UI action.  Every eligible stock gets an explicit
    score row: numeric when its point-in-time inputs satisfy the contract, or
    ``DATA_INSUFFICIENT`` with readiness evidence when they do not.
    """
    ids = stock_ids or list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True)).order_by(Stock.stock_id)).all())
    score_job = job or _job_start(db, "score", as_of, as_of, stocks_attempted=len(ids))
    score_cutoff = _now()
    scores: dict[str, int] = {}
    ready_stock_count = 0
    not_ready_stock_count = 0
    score_rows_failed = 0
    reason_counts: dict[str, int] = {}

    for index, stock_id in enumerate(ids, start=1):
        try:
            evaluation = _evaluate_stock_inputs(db, stock_id, as_of, score_cutoff)
            if evaluation["ready"]:
                ready_stock_count += 1
            else:
                not_ready_stock_count += 1
                for reason in evaluation["missing_reasons"]:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
            score = _persist_stock_evaluation(db, evaluation)
            scores[score.status] = scores.get(score.status, 0) + 1
        except Exception:
            db.rollback()
            not_ready_stock_count += 1
            score_rows_failed += 1
            reason_counts["score_evaluation_failed"] = reason_counts.get("score_evaluation_failed", 0) + 1
            # Keep the accounting explicit even if one isolated stock fails.
            try:
                failure_coverage = {
                    "InstitutionalDataAvailable": False,
                    "ForeignHoldingDataAvailable": False,
                    "HoldingDistributionAvailable": False,
                    "BrokerDataAvailable": False,
                    "PriceDataAvailable": False,
                    "RequiredFeatureValidation": {},
                    "missing_reasons": ["score evaluation failed before readiness could be proven"],
                    "readiness_reason_codes": ["score_evaluation_failed"],
                    "calendar_version": CALENDAR_VERSION,
                }
                failure_evaluation = {
                    "stock_id": stock_id,
                    "as_of": as_of,
                    "knowledge_cutoff": score_cutoff,
                    "features": {},
                    "coverage": failure_coverage,
                    "score_result": calculate_score({}, failure_coverage),
                    "input_hashes": [],
                    "snapshot_hash": hashlib.sha256(b"[]").hexdigest(),
                    "latest_source_date": None,
                    "ready": False,
                    "missing_reasons": ["score_evaluation_failed"],
                }
                _persist_stock_evaluation(db, failure_evaluation)
                scores["DATA_INSUFFICIENT"] = scores.get("DATA_INSUFFICIENT", 0) + 1
            except Exception:
                db.rollback()
                scores["FAILED"] = scores.get("FAILED", 0) + 1

        if progress_callback:
            progress_callback(index, len(ids))
        if index == len(ids) or index % 25 == 0:
            score_job.checkpoint_state = {
                "run_mode": "existing_data",
                "target_date": as_of.isoformat(),
                "processed_stock_count": index,
                "universe_stock_count": len(ids),
                "scores": scores,
                "score_metrics": {
                    "universe_stock_count": len(ids),
                    "evaluated_stock_count": index,
                    "ready_stock_count": ready_stock_count,
                    "not_ready_stock_count": not_ready_stock_count,
                    "score_rows_processed": sum(count for status, count in scores.items() if status not in {"DATA_INSUFFICIENT", "FAILED"}),
                    "score_rows_data_insufficient": scores.get("DATA_INSUFFICIENT", 0),
                    "score_rows_failed": score_rows_failed,
                    "missing_reason_counts": reason_counts,
                    "accounting_invariant": ready_stock_count + not_ready_stock_count == index,
                },
            }
            db.commit()

    score_rows_data_insufficient = scores.get("DATA_INSUFFICIENT", 0)
    score_rows_processed = sum(count for status, count in scores.items() if status not in {"DATA_INSUFFICIENT", "FAILED"})
    metrics = {
        "universe_stock_count": len(ids),
        "evaluated_stock_count": len(ids),
        "ready_stock_count": ready_stock_count,
        "not_ready_stock_count": not_ready_stock_count,
        "score_rows_processed": score_rows_processed,
        "score_rows_data_insufficient": score_rows_data_insufficient,
        "score_rows_failed": score_rows_failed,
        "missing_reason_counts": reason_counts,
        "accounting_invariant": ready_stock_count + not_ready_stock_count == len(ids),
    }
    status = "SCORE_BLOCKED_BY_SOURCE_COVERAGE" if ready_stock_count == 0 and score_rows_failed == 0 else ("SUCCESS" if score_rows_failed == 0 else "PARTIAL")
    checkpoint = {
        "run_mode": "existing_data",
        "target_date": as_of.isoformat(),
        "scores": scores,
        "score_metrics": metrics,
        **score_readiness_checkpoint(db, as_of, len(ids)),
    }
    _job_finish(db, score_job, status, stocks_completed=len(ids) - score_rows_failed, stocks_failed=score_rows_failed, checkpoint_state=checkpoint)
    return {"job_id": score_job.id, "status": status, "target_date": as_of.isoformat(), "scores": scores, "score_metrics": metrics}


def seed_score_version(db: Session) -> None:
    current = db.get(ScoreVersion, SCORE_VERSION)
    if current is None:
        db.add(ScoreVersion(version=SCORE_VERSION, config=SCORE_MANIFEST, manifest_hash=FORMULA_HASH, explanation="Canonical S-only v1 manifest; price/volume is supporting only.", created_at=_now()))
        try:
            db.commit()
        except IntegrityError:
            # API and worker may start simultaneously on a new score version.
            # A unique-key loser must verify the committed winner rather than
            # crash and rely on the container restart policy.
            db.rollback()
        current = db.get(ScoreVersion, SCORE_VERSION)
        if current is None:
            raise RuntimeError("score version seed disappeared after concurrent insert")
    if current.manifest_hash not in {None, FORMULA_HASH}:
        raise RuntimeError("score version manifest mismatch; deploy a new score version before starting")
    if current.manifest_hash is None:
        raise RuntimeError("score version manifest provenance is missing; create an explicit new score version")


SCORE_READINESS_DATASETS = (
    "TaiwanStockInfo",
    "TaiwanStockInstitutionalInvestorsBuySellWide",
    "TaiwanStockShareholding",
    "TaiwanStockHoldingSharesPer",
    "TaiwanStockTradingDailyReport",
    "TaiwanStockPrice",
)


def authoritative_source_state_hash(db: Session) -> str:
    """Bind a score run to the exact authoritative sync snapshot."""
    payload = []
    for dataset in SCORE_READINESS_DATASETS:
        row = db.get(DataSyncStatus, dataset)
        payload.append({
            "dataset": dataset,
            "status": row.status if row else None,
            "latest_source_date": row.latest_source_date.isoformat() if row and row.latest_source_date else None,
            "expected_latest_source_date": row.expected_latest_source_date.isoformat() if row and row.expected_latest_source_date else None,
            "last_fully_successful_sync": row.last_fully_successful_sync.isoformat() if row and row.last_fully_successful_sync else None,
            "staleness_state": row.staleness_state if row else None,
        })
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def score_snapshot_state(db: Session, target_date: date) -> dict[str, Any]:
    """Hash the latest immutable score row selected for every stock."""
    rows = db.scalars(
        select(AccumulationScore)
        .where(AccumulationScore.source_date == target_date, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None))
        .order_by(AccumulationScore.stock_id, AccumulationScore.calculated_at.desc(), AccumulationScore.id.desc())
    ).all()
    latest_by_stock: dict[str, AccumulationScore] = {}
    for row in rows:
        latest_by_stock.setdefault(row.stock_id, row)
    payload = [
        {
            "stock_id": stock_id,
            "input_snapshot_hash": row.input_snapshot_hash,
            "formula_hash": row.formula_hash,
            "status": row.status,
            "score": row.score,
            "knowledge_cutoff": row.knowledge_cutoff.isoformat() if row.knowledge_cutoff else None,
        }
        for stock_id, row in sorted(latest_by_stock.items())
    ]
    return {
        "score_rows_count": len(payload),
        "numeric_score_count": sum(1 for item in payload if item["score"] is not None),
        "score_snapshot_hash": hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "formula_hashes_match": all(item["formula_hash"] == FORMULA_HASH for item in payload),
        "input_snapshots_bound": all(bool(item["input_snapshot_hash"]) and bool(item["knowledge_cutoff"]) for item in payload),
    }


def score_readiness_checkpoint(db: Session, target_date: date, stock_count: int) -> dict[str, Any]:
    return {
        "target_date": target_date.isoformat(),
        "score_version": SCORE_VERSION,
        "formula_hash": FORMULA_HASH,
        "calendar_hash": CALENDAR_HASH,
        "source_state_hash": authoritative_source_state_hash(db),
        "stock_count": stock_count,
        **score_snapshot_state(db, target_date),
    }


def holding_coverage_state(db: Session, stock_ids: list[str], target_date: date) -> dict[str, Any]:
    """Validate the complete weekly holding schema for the requested date."""
    rows = db.scalars(
        select(HoldingDistribution).where(
            HoldingDistribution.stock_id.in_(stock_ids),
            HoldingDistribution.source_date == target_date,
            HoldingDistribution.source_dataset == "TaiwanStockHoldingSharesPer",
        )
    ).all()
    by_stock: dict[str, list[dict[str, Any]]] = {stock_id: [] for stock_id in stock_ids}
    for row in rows:
        by_stock.setdefault(row.stock_id, []).append({
            "holding_shares_level": row.holding_shares_level,
            "holding_shares_threshold": row.holding_shares_threshold,
            "percent": row.percent,
            "people": row.people,
            "shares": row.shares,
        })
    states = {stock_id: holding_schema_state(payload) for stock_id, payload in by_stock.items()}
    complete_ids = [stock_id for stock_id, state in states.items() if state["available"] is True]
    incomplete_ids = [stock_id for stock_id in stock_ids if stock_id not in complete_ids]
    missing_bucket_counts = {
        stock_id: len(states[stock_id].get("missing_thresholds", []))
        for stock_id in incomplete_ids
        if states[stock_id].get("missing_thresholds")
    }
    return {
        "target_date": target_date.isoformat(),
        "required_bucket_count": len(HOLDING_CANONICAL_THRESHOLDS),
        "requested_stocks": len(stock_ids),
        "stocks_with_rows": sum(1 for payload in by_stock.values() if payload),
        "complete_stocks": len(complete_ids),
        "incomplete_stocks": len(incomplete_ids),
        "complete": len(complete_ids) == len(stock_ids),
        "missing_bucket_stocks": len(missing_bucket_counts),
        "incomplete_stock_sample": incomplete_ids[:20],
        "missing_bucket_count_sample": dict(list(missing_bucket_counts.items())[:20]),
    }


def score_source_coverage_gate(db: Session, target_date: date) -> dict[str, Any]:
    """Report global source coverage as advisory observability.

    This intentionally does not decide whether the production score loop may
    run.  Individual stock readiness is evaluated by
    :func:`_evaluate_stock_inputs` for every eligible stock.
    """
    blockers: list[dict[str, Any]] = []
    expected_by_dataset = {dataset: authoritative_expected_latest_source_date(dataset) for dataset in SCORE_READINESS_DATASETS}
    for dataset in SCORE_READINESS_DATASETS:
        row = db.get(DataSyncStatus, dataset)
        if row is None:
            blockers.append({"dataset": dataset, "reason_code": "MISSING_REQUIRED_SOURCE_STATUS"})
            continue
        metadata = row.metadata_json or {}
        coverage = metadata.get("coverage") if isinstance(metadata, dict) else {}
        if not isinstance(coverage, dict):
            coverage = metadata if isinstance(metadata, dict) else {}
        if row.status == "WAITING_FOR_PROVIDER_PUBLICATION":
            blockers.append({"dataset": dataset, "reason_code": "WAITING_FOR_PROVIDER_PUBLICATION", "expected_source_date": expected_by_dataset[dataset]})
            continue
        if row.status == "QUOTA_EXHAUSTED" or row.last_error_code == "QUOTA_EXHAUSTED" or coverage.get("fatal_code") == "QUOTA_EXHAUSTED":
            blockers.append({"dataset": dataset, "reason_code": "QUOTA_EXHAUSTED"})
            continue
        retryable_pending = int(coverage.get("retryable_pending", 0) or 0)
        if dataset == "TaiwanStockTradingDailyReport" and retryable_pending:
            blockers.append({"dataset": dataset, "reason_code": "BROKER_RETRY_PENDING", "retryable_pending": retryable_pending, "next_retry_at": coverage.get("next_retry_at")})
        if row.status not in {"SUCCESS", "REUSED"}:
            partial_reason = "HOLDING_PUBLICATION_PARTIAL" if dataset == "TaiwanStockHoldingSharesPer" and coverage.get("publication_state") == "HOLDING_PUBLICATION_PARTIAL" else None
            blockers.append({"dataset": dataset, "reason_code": partial_reason or row.last_error_code or f"SOURCE_{row.status}", "status": row.status})
            continue
        expected = expected_by_dataset[dataset]
        if expected is None or row.latest_source_date is None:
            blockers.append({"dataset": dataset, "reason_code": "SOURCE_DATE_MISSING", "expected_source_date": expected, "actual_source_date": row.latest_source_date})
        elif row.latest_source_date < expected or row.staleness_state != "FRESH":
            blockers.append({"dataset": dataset, "reason_code": "SOURCE_DATE_STALE", "expected_source_date": expected, "actual_source_date": row.latest_source_date, "staleness": row.staleness_state})
        if dataset == "TaiwanStockHoldingSharesPer":
            holding_coverage = coverage.get("holding_schema")
            if not isinstance(holding_coverage, dict) and expected is not None:
                stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))).all())
                holding_coverage = holding_coverage_state(db, stock_ids, expected)
            if not isinstance(holding_coverage, dict):
                blockers.append({"dataset": dataset, "reason_code": "HOLDING_COVERAGE_NOT_VERIFIED"})
            elif holding_coverage.get("complete") is not True:
                blockers.append({"dataset": dataset, "reason_code": "HOLDING_BUCKETS_INCOMPLETE", **holding_coverage})
    if blockers:
        reason_code = next((item["reason_code"] for item in blockers if item["reason_code"] in {"WAITING_FOR_PROVIDER_PUBLICATION", "HOLDING_PUBLICATION_PARTIAL", "QUOTA_EXHAUSTED", "BROKER_RETRY_PENDING", "HOLDING_BUCKETS_INCOMPLETE"}), blockers[0]["reason_code"])
        return {"ready": False, "status": "SCORE_BLOCKED_BY_SOURCE_COVERAGE", "reason_code": reason_code, "target_date": target_date.isoformat(), "blocking_sources": blockers, "score_rows_processed": 0}
    return {"ready": True, "status": "READY", "reason_code": None, "target_date": target_date.isoformat(), "blocking_sources": [], "score_rows_processed": 0}


def record_score_blocked(db: Session, target_date: date, gate: dict[str, Any]) -> dict[str, Any]:
    """Persist one explicit global Score block without creating stock rows."""
    score_job = _job_start(db, "score", target_date, target_date, stocks_attempted=0)
    _job_finish(db, score_job, "SCORE_BLOCKED_BY_SOURCE_COVERAGE", error_code=gate["reason_code"], checkpoint_state=_jsonable(gate))
    return {"SCORE_BLOCKED_BY_SOURCE_COVERAGE": 0}


async def intraday_sync(db: Session, client: FinMindClient, end_date: date | None = None, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Refresh only current daily sources during the open-market window."""
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        with bind.connect() as lock_connection:
            acquired = lock_connection.scalar(text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": PIPELINE_ADVISORY_LOCK_KEY}) is True
            if not acquired:
                return {"status": "SKIPPED_CONCURRENT_RUN", "reason_code": "PIPELINE_ADVISORY_LOCK_HELD", "mode": "intraday", "datasets": {}}
            try:
                return await _intraday_sync_locked(db, client, end_date, progress_callback)
            finally:
                lock_connection.scalar(text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": PIPELINE_ADVISORY_LOCK_KEY})
    return await _intraday_sync_locked(db, client, end_date, progress_callback)


async def _intraday_sync_locked(db: Session, client: FinMindClient, end_date: date | None = None, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
    end = end_date or date.today()
    stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))).all())
    result: dict[str, Any] = {"status": "SUCCESS", "mode": "intraday", "datasets": {}}
    if not stock_ids:
        return {"status": "DEFERRED_NO_UNIVERSE", "mode": "intraday", "datasets": {}}
    for dataset in ("TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding", "TaiwanStockPrice"):
        if progress_callback:
            progress_callback(dataset)
        job = _job_start(db, dataset, end, end, stocks_attempted=len(stock_ids))
        accepted = versioned = 0
        try:
            def sink(rows: list[dict[str, Any]]) -> dict[str, Any]:
                nonlocal accepted, versioned
                before = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == dataset)) or 0
                accepted_now = ingest_records(db, dataset, rows)
                after = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == dataset)) or 0
                accepted += accepted_now
                versioned += max(0, int(after) - int(before))
                return {"accepted_count": accepted_now, "versioned_count": max(0, int(after) - int(before)), "accepted_dates": sorted({str(_as_date(_v(row, "date", "source_date"))) for row in rows if _as_date(_v(row, "date", "source_date")) is not None})}

            if hasattr(client, "fetch_stocks_dataset"):
                metrics = await client.fetch_stocks_dataset(stock_ids, dataset, end.isoformat(), end.isoformat(), record_sink=sink, progress_callback=progress_callback)
                received = int(metrics.get("rows_received", metrics.get("rows", 0)))
                accepted = int(metrics.get("rows_accepted", accepted))
                versioned = int(metrics.get("rows_versioned", versioned))
                latest_dates = [_as_date(item.get("last_source_date")) for item in metrics.get("per_stock", {}).values() if item.get("last_source_date")]
                latest = max(latest_dates, default=None)
                coverage = {"mode": "intraday_current_session", **{key: metrics.get(key) for key in ("requested", "success", "failed", "retryable_pending", "permanent_failed", "physical_requests", "rows_received", "rows_accepted", "rows_versioned", "checkpoint_state", "checkpoint_manifest_hash", "checkpoint_content_hash_before", "checkpoint_content_hash_after", "unresolved_observations")}}
                status = "SUCCESS" if not metrics.get("fatal_code") and int(metrics.get("retryable_pending", 0)) == 0 and int(metrics.get("permanent_failed", 0)) == 0 and int(metrics.get("success", 0)) >= len(stock_ids) else ("FAILED" if metrics.get("fatal_code") else "PARTIAL")
                code = metrics.get("fatal_code") or (None if status == "SUCCESS" else "INTRADAY_PARTIAL")
                received = int(metrics.get("rows_received", metrics.get("rows", 0)))
            else:
                records, meta = client.fetch(dataset, start_date=end.isoformat(), end_date=end.isoformat())
                received = len(records)
                accepted = ingest_records(db, dataset, records)
                latest = _as_date(meta.get("source_date"))
                coverage = {"mode": "intraday_fallback", "requested": len(stock_ids), "rows_received": received, "rows_accepted": accepted, "physical_requests": 1}
                status = "SUCCESS" if accepted else "PARTIAL"
                code = None if accepted else "NO_DATA"
            expected = _expected_latest_source_date(dataset, end)
            _mark_sync(db, dataset, status, accepted, latest, code, fetched_at=_now(), expected_latest=expected, rows_received=received, rows_accepted=accepted, rows_rejected=max(0, received - accepted), rows_versioned=versioned, physical_requests=int(coverage.get("physical_requests") or 0), stored_total=_stored_rows_total(db, dataset), metadata={"query_mode": "intraday_current_session", "coverage": coverage})
            _job_finish(db, job, status, records=accepted, stocks_completed=int(coverage.get("success") or 0), stocks_failed=int(coverage.get("failed") or 0), error_code=code, checkpoint_state=coverage)
            result["datasets"][dataset] = {"status": status, "coverage": coverage}
            if status != "SUCCESS":
                result["status"] = "PARTIAL"
        except Exception as exc:
            db.rollback()
            code = getattr(exc, "code", "UNEXPECTED")
            _job_finish(db, job, "FAILED", error_code=code, error=str(exc), stocks_failed=len(stock_ids))
            result["datasets"][dataset] = {"status": "FAILED", "error_code": code}
            result["status"] = "PARTIAL"
    return result


async def catch_up(db: Session, client: FinMindClient, end_date: date | None = None, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run one globally serialized production pipeline attempt."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return await _catch_up_locked(db, client, end_date, progress_callback)
    with bind.connect() as lock_connection:
        acquired = lock_connection.scalar(text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": PIPELINE_ADVISORY_LOCK_KEY}) is True
        if not acquired:
            return {"status": "SKIPPED_CONCURRENT_RUN", "reason_code": "PIPELINE_ADVISORY_LOCK_HELD", "datasets": {}, "scores": {}, "source_coverage": {}}
        try:
            return await _catch_up_locked(db, client, end_date, progress_callback)
        finally:
            lock_connection.scalar(text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": PIPELINE_ADVISORY_LOCK_KEY})


async def _catch_up_locked(db: Session, client: FinMindClient, end_date: date | None = None, progress_callback: Callable[[str], None] | None = None) -> dict[str, Any]:
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
        result["provider_work_deferred"] = {"reason": "dynamic universe refresh failed; existing local universe will be evaluated", "error_code": code}
        # Do not spend provider quota after a systemic universe failure.  The
        # existing active universe remains eligible for local readiness and
        # scoring, exactly like a quota-exhausted source run.
        required = []
    stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))).all())
    for dataset in required:
        refresh_stock_ids = prioritize_stock_ids(db, stock_ids, dataset)
        progress(dataset)
        job = _job_start(db, dataset, start, end, stocks_attempted=len(stock_ids))
        received = accepted = versioned = 0
        metrics: dict[str, Any] = {}
        latest_dates: list[date] = []
        def sink(rows: list[dict[str, Any]]) -> dict[str, Any]:
            nonlocal accepted, versioned
            revisions_before = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == dataset)) or 0
            accepted_now = ingest_records(db, dataset, rows)
            revisions_after = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == dataset)) or 0
            versioned_now = max(0, int(revisions_after) - int(revisions_before))
            accepted += accepted_now
            versioned += versioned_now
            model = _DATASET_MODELS[dataset]
            stock_values = {str(_v(row, "stock_id", "證券代號") or "").strip() for row in rows}
            source_dates = {_as_date(_v(row, "date", "source_date")) for row in rows}
            stock_values.discard("")
            source_dates.discard(None)
            accepted_dates: list[str] = []
            if accepted_now and stock_values and source_dates:
                query = select(model.source_date).where(model.stock_id.in_(stock_values), model.source_date.in_(source_dates), model.source_dataset == dataset).distinct()
                accepted_dates = sorted(day.isoformat() for day in db.scalars(query).all())
            return {"accepted_count": accepted_now, "versioned_count": versioned_now, "accepted_dates": accepted_dates}

        try:
            if hasattr(client, "fetch_stocks_dataset"):
                metrics = await client.fetch_stocks_dataset(refresh_stock_ids, dataset, (start - timedelta(days=30)).isoformat(), end.isoformat(), record_sink=sink, progress_callback=progress)
                received = int(metrics.get("rows_received", metrics.get("rows", 0)))
                accepted = int(metrics.get("rows_accepted", accepted))
                versioned = int(metrics.get("rows_versioned", versioned))
                latest_dates = [_as_date(item.get("last_source_date")) for item in metrics.get("per_stock", {}).values() if item.get("last_source_date")]
                latest = max(latest_dates, default=None)
                newly_fetched = int(metrics.get("newly_fetched", metrics.get("success", 0)))
                reused_complete = int(metrics.get("reused_complete", 0))
                reused_valid_no_data = int(metrics.get("reused_valid_no_data", 0))
                valid_no_data = int(metrics.get("no_data", 0))
                retryable_pending = int(metrics.get("retryable_pending", 0))
                permanent_failed = int(metrics.get("permanent_failed", 0))
                physical_requests = int(metrics.get("physical_requests", 0))
                expected = _expected_latest_source_date(dataset, end)
                satisfied = not metrics.get("fatal_code") and retryable_pending == 0 and permanent_failed == 0 and int(metrics.get("success", 0)) >= len(stock_ids)
                status = "FAILED" if metrics.get("fatal_code") else ("REUSED" if satisfied and physical_requests == 0 and len(stock_ids) > 0 else ("SUCCESS" if satisfied else "PARTIAL"))
                code = metrics.get("fatal_code") or ("STOCK_PARTIAL" if not satisfied and (retryable_pending or permanent_failed) else None)
                coverage = {"requested": len(stock_ids), "success": int(metrics.get("success", 0)), "newly_fetched": newly_fetched, "reused_complete": reused_complete, "reused_valid_no_data": reused_valid_no_data, "valid_no_data": valid_no_data, "retryable_pending": retryable_pending, "permanent_failed": permanent_failed, "physical_requests": physical_requests, "failed": int(metrics.get("failed", 0)), "rows_received": received, "rows_accepted": accepted, "rows_rejected": int(metrics.get("rows_rejected", max(0, received - accepted))), "rows_versioned": versioned, "observations_reused": int(metrics.get("observations_reused", 0)), "provider_missing_observations": int(metrics.get("provider_missing_observations", 0)), "provider_missing_observations_reused": int(metrics.get("provider_missing_observations_reused", 0)), "missing_values_imputed_as_zero": int(metrics.get("missing_values_imputed_as_zero", 0)), "fatal_code": metrics.get("fatal_code"), "checkpoint_state": metrics.get("checkpoint_state"), "checkpoint_manifest_hash": metrics.get("checkpoint_manifest_hash"), "checkpoint_content_hash_before": metrics.get("checkpoint_content_hash_before"), "checkpoint_content_hash_after": metrics.get("checkpoint_content_hash_after"), "selection_policy": metrics.get("selection_policy"), "fair_cursor_start_stock_id": metrics.get("fair_cursor_start_stock_id"), "fair_cursor_end_stock_id": metrics.get("fair_cursor_end_stock_id"), "observation_cadence": metrics.get("observation_cadence"), "expected_observations_per_stock": metrics.get("expected_observations_per_stock"), "verified_observations": metrics.get("verified_observations"), "unresolved_observations": metrics.get("unresolved_observations"), "partial_responses": metrics.get("partial_responses", 0)}
                if dataset == "TaiwanStockHoldingSharesPer":
                    holding_schema = holding_coverage_state(db, stock_ids, expected)
                    coverage["holding_schema"] = holding_schema
                    coverage["publication_state"] = metrics.get("publication_state")
                    coverage["next_publication_check_at"] = metrics.get("next_publication_check_at")
                    coverage["publication_target_date"] = metrics.get("publication_target_date")
                    coverage["publication_target_records"] = metrics.get("publication_target_records")
                    coverage["publication_probe_requests"] = int(metrics.get("publication_probe_requests", 0))
                    coverage["publication_probe"] = metrics.get("publication_probe")
                    coverage["publication_check_performed"] = bool(metrics.get("publication_check_performed", False))
                    coverage["publication_recheck_due"] = bool(metrics.get("publication_recheck_due", False))
                    coverage["publication_last_check_result"] = metrics.get("publication_last_check_result")
                    coverage["publication_evidence_source"] = metrics.get("publication_evidence_source")
                    coverage["publication_wait_invalidated"] = bool(metrics.get("publication_wait_invalidated", False))
                    if holding_schema["complete"]:
                        status = "REUSED" if physical_requests == 0 and len(stock_ids) > 0 else "SUCCESS"
                        code = None
                    elif metrics.get("publication_state") == "WAITING_FOR_PROVIDER_PUBLICATION" or (holding_schema["stocks_with_rows"] == 0 and not metrics.get("fatal_code") and retryable_pending == 0 and (latest is None or latest < expected)):
                        status = "WAITING_FOR_PROVIDER_PUBLICATION"
                        code = "WAITING_FOR_PROVIDER_PUBLICATION"
                    else:
                        status = "PARTIAL"
                        code = "HOLDING_PUBLICATION_PARTIAL" if metrics.get("publication_state") == "HOLDING_PUBLICATION_PARTIAL" else "HOLDING_BUCKETS_INCOMPLETE"
            else:
                records, meta = client.fetch(dataset, start_date=(start - timedelta(days=30)).isoformat(), end_date=end.isoformat())
                received = len(records)
                revisions_before = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == dataset)) or 0
                accepted = ingest_records(db, dataset, records)
                revisions_after = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == dataset)) or 0
                versioned = max(0, int(revisions_after) - int(revisions_before))
                latest = _as_date(meta.get("source_date"))
                status = "SUCCESS" if accepted else "PARTIAL"
                code = None if accepted else "NO_DATA"
                coverage = {"mode": "fallback-broad", "requested": len(stock_ids), "rows_received": received, "rows_accepted": accepted, "rows_rejected": max(0, received - accepted), "rows_versioned": versioned, "observations_reused": 0}
                expected = _expected_latest_source_date(dataset, end)
            _mark_sync(db, dataset, status, accepted, latest, code, fetched_at=_now(), expected_latest=expected, rows_received=received, rows_accepted=accepted, rows_rejected=int(coverage.get("rows_rejected", max(0, received - accepted))), rows_versioned=versioned, observations_reused=int(coverage.get("observations_reused", 0)), physical_requests=int(coverage.get("physical_requests", 1)), stored_total=_stored_rows_total(db, dataset), metadata={"requested_start": (start - timedelta(days=30)).isoformat(), "requested_end": end.isoformat(), "query_mode": "per_stock_date_range" if hasattr(client, "fetch_stocks_dataset") else "fallback_broad", "coverage": coverage})
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
            _mark_sync(db, dataset, "FAILED", accepted, None, code, str(exc), fetched_at=_now(), expected_latest=_expected_latest_source_date(dataset, end), rows_received=received, rows_accepted=accepted, rows_rejected=max(0, received - accepted), rows_versioned=versioned, physical_requests=int(metrics.get("physical_requests", 1)), stored_total=_stored_rows_total(db, dataset))
            result["datasets"][dataset] = {"status": "FAILED", "error_code": code}
            result["status"] = "PARTIAL"
            if code in GLOBAL_PROVIDER_FAILURE_CODES:
                result["fatal_code"] = code
                break
    fatal_code = result.get("fatal_code")
    if fatal_code:
        result["provider_work_deferred"] = {"reason": "global provider failure; later source and broker requests were not launched", "error_code": fatal_code}
        # Keep the global failure visible, but do not let it veto local
        # per-stock scoring below.  Broker work is explicitly marked deferred
        # because a quota/auth/schema failure must never be bypassed.
        broker_metrics = {
            "requested": len(stock_ids),
            "requested_keys": len(stock_ids) * 20,
            "skipped_checkpoint": 0,
            "physical_requests": 0,
            "rows": 0,
            "rows_received": 0,
            "failed": 0,
            "retryable_pending": 0,
            "success": 0,
            "stocks_completed": 0,
            "stocks_failed": 0,
            "deferred": True,
            "deferred_reason": fatal_code,
        }
        result["datasets"]["TaiwanStockTradingDailyReport"] = {
            **broker_metrics,
            "status": "DEFERRED",
        }
    broker_start = expected_trading_sessions(end, 20)[0]
    pending_broker_rebuilds = _pending_broker_rebuild_stock_ids(db)
    progress("TaiwanStockTradingDailyReport")
    broker_job = _job_start(db, "TaiwanStockTradingDailyReport", broker_start, end, stocks_attempted=len(stock_ids))
    if not fatal_code:
        broker_metrics = {}
    broker_buffer: list[dict[str, Any]] = []
    stored = 0
    broker_versioned = 0

    def broker_sink(rows: list[dict[str, Any]]) -> int:
        nonlocal stored, broker_versioned
        broker_buffer.extend(rows)
        if len(broker_buffer) >= 5000:
            revisions_before = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == "TaiwanStockTradingDailyReport")) or 0
            stored_now = ingest_records(db, "TaiwanStockTradingDailyReport", broker_buffer[:])
            revisions_after = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == "TaiwanStockTradingDailyReport")) or 0
            stored += stored_now
            broker_versioned += max(0, int(revisions_after) - int(revisions_before))
            broker_buffer.clear()
        return stored

    try:
        if not fatal_code:
            refresh_stock_ids = prioritize_stock_ids(db, stock_ids, "TaiwanStockTradingDailyReport")
            broker_metrics = await client.fetch_broker_stocks(refresh_stock_ids, broker_start.isoformat(), end.isoformat(), record_sink=broker_sink, progress_callback=progress)
        if broker_buffer:
            revisions_before = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == "TaiwanStockTradingDailyReport")) or 0
            stored_now = ingest_records(db, "TaiwanStockTradingDailyReport", broker_buffer)
            revisions_after = db.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.dataset == "TaiwanStockTradingDailyReport")) or 0
            stored += stored_now
            broker_versioned += max(0, int(revisions_after) - int(revisions_before))
            broker_buffer.clear()
        checkpoint_complete = broker_metrics.get("skipped_checkpoint", 0) >= broker_metrics.get("requested_keys", len(stock_ids) * 20)
        no_work_reused = checkpoint_complete and broker_metrics.get("physical_requests", 0) == 0 and broker_metrics.get("rows", 0) == 0 and broker_metrics.get("failed", 0) == 0
        if fatal_code:
            # A fatal provider error occurred before this phase.  Keep the
            # broker work explicitly deferred instead of relabelling it as a
            # misleading partial attempt or pretending that the checkpoint
            # was inspected successfully.
            broker_status = "DEFERRED"
            broker_error = fatal_code
        else:
            broker_status = "QUOTA_EXHAUSTED" if broker_metrics.get("fatal_code") == "QUOTA_EXHAUSTED" else ("REUSED" if no_work_reused else ("SUCCESS" if broker_metrics.get("failed", 0) == 0 and broker_metrics.get("retryable_pending", 0) == 0 and (broker_metrics.get("rows", 0) > 0 or not stock_ids) else "PARTIAL"))
            if no_work_reused:
                broker_metrics["reuse_reason"] = "all requested stock-session keys already completed; no new physical requests"
            broker_error = None if broker_status in {"SUCCESS", "REUSED"} else (broker_metrics.get("fatal_code") or "BROKER_PARTIAL")
        _mark_sync(db, "TaiwanStockTradingDailyReport", broker_status, stored, end if stored else None, broker_error, fetched_at=_now(), expected_latest=_expected_latest_source_date("TaiwanStockTradingDailyReport", end), rows_received=broker_metrics.get("rows_received", broker_metrics.get("rows", 0)), rows_accepted=stored, rows_rejected=max(0, broker_metrics.get("rows_received", broker_metrics.get("rows", 0)) - stored), rows_versioned=broker_versioned, observations_reused=int(broker_metrics.get("observations_reused", 0)), physical_requests=int(broker_metrics.get("physical_requests", 0)), stored_total=_stored_rows_total(db, "TaiwanStockTradingDailyReport"), metadata={"query_mode": "per_stock_per_session", "rows_versioned": broker_versioned, **broker_metrics})
        _job_finish(db, broker_job, broker_status, records=stored, retry_count=broker_metrics.get("retries", 0), stocks_completed=broker_metrics.get("stocks_completed", 0), stocks_failed=broker_metrics.get("stocks_failed", 0), error_code=broker_error, checkpoint_state=broker_metrics)
        result["datasets"]["TaiwanStockTradingDailyReport"] = {**broker_metrics, "stored_records": stored, "status": broker_status}
        if broker_status not in {"SUCCESS", "REUSED"}:
            result["status"] = "PARTIAL"
        if broker_metrics.get("fatal_code"):
            result["fatal_code"] = broker_metrics["fatal_code"]
            result["provider_work_deferred"] = {
                "reason": "global provider failure; broker work and further ingestion were deferred",
                "error_code": broker_metrics["fatal_code"],
            }
    except Exception as exc:
        db.rollback()
        code = getattr(exc, "code", "UNEXPECTED")
        _job_finish(db, broker_job, "FAILED", error_code=code, error=str(exc), stocks_failed=len(stock_ids))
        result["datasets"]["TaiwanStockTradingDailyReport"] = {"status": "FAILED", "error_code": code}
        result["status"] = "PARTIAL"
    # The global source gate remains useful observability, but it is advisory
    # only.  A PARTIAL/stale/quota-exhausted source means that *some* stocks
    # are missing data; it does not prove that every stock is unscoreable.
    score_gate = score_source_coverage_gate(db, end)
    score_cutoff = _now()
    result["score_preflight"] = {**score_gate, "scope": "advisory_global_observability", "per_stock_gate": True}
    score_job = _job_start(db, "score", end, end, stocks_attempted=len(stock_ids))
    progress("score")
    ready_stock_count = 0
    not_ready_stock_count = 0
    score_rows_failed = 0
    reason_counts: dict[str, int] = {}
    for stock_id in stock_ids:
        try:
            evaluation = _evaluate_stock_inputs(db, stock_id, end, score_cutoff)
            if evaluation["ready"]:
                ready_stock_count += 1
            else:
                not_ready_stock_count += 1
                for reason in evaluation["missing_reasons"]:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
            score = _persist_stock_evaluation(db, evaluation)
            result["scores"][score.status] = result["scores"].get(score.status, 0) + 1
        except Exception:
            result["status"] = "PARTIAL"
            not_ready_stock_count += 1
            score_rows_failed += 1
            reason_counts["score_evaluation_failed"] = reason_counts.get("score_evaluation_failed", 0) + 1
            # Preserve an explicit fail-closed row for an isolated evaluation
            # error so no stock silently disappears from the run accounting.
            # The original failure remains visible in score_rows_failed.
            fallback_persisted = False
            try:
                failure_coverage = {
                    "InstitutionalDataAvailable": False,
                    "ForeignHoldingDataAvailable": False,
                    "HoldingDistributionAvailable": False,
                    "BrokerDataAvailable": False,
                    "PriceDataAvailable": False,
                    "RequiredFeatureValidation": {},
                    "missing_reasons": ["score evaluation failed before readiness could be proven"],
                    "readiness_reason_codes": ["score_evaluation_failed"],
                    "calendar_version": CALENDAR_VERSION,
                }
                failure_evaluation = {
                    "stock_id": stock_id,
                    "as_of": end,
                    "knowledge_cutoff": score_cutoff,
                    "features": {},
                    "coverage": failure_coverage,
                    "score_result": calculate_score({}, failure_coverage),
                    "input_hashes": [],
                    "snapshot_hash": hashlib.sha256(b"[]").hexdigest(),
                    "latest_source_date": None,
                    "ready": False,
                    "missing_reasons": ["score_evaluation_failed"],
                }
                _persist_stock_evaluation(db, failure_evaluation)
                fallback_persisted = True
                result["scores"]["DATA_INSUFFICIENT"] = result["scores"].get("DATA_INSUFFICIENT", 0) + 1
            except Exception:
                db.rollback()
            if not fallback_persisted:
                result["scores"]["FAILED"] = result["scores"].get("FAILED", 0) + 1
    score_rows_data_insufficient = result["scores"].get("DATA_INSUFFICIENT", 0)
    score_rows_processed = sum(count for status, count in result["scores"].items() if status not in {"DATA_INSUFFICIENT", "FAILED"})
    result["score_metrics"] = {
        "universe_stock_count": len(stock_ids),
        "evaluated_stock_count": len(stock_ids),
        "ready_stock_count": ready_stock_count,
        "not_ready_stock_count": not_ready_stock_count,
        "score_rows_processed": score_rows_processed,
        "score_rows_data_insufficient": score_rows_data_insufficient,
        "score_rows_failed": score_rows_failed,
        "missing_reason_counts": reason_counts,
        "accounting_invariant": ready_stock_count + not_ready_stock_count == len(stock_ids),
    }
    result["score_preflight"]["per_stock_summary"] = result["score_metrics"]
    # Preserve an explicit zero-ready outcome for operational dashboards while
    # allowing a mixed run to complete with numeric and fail-closed rows.
    score_job_status = "SCORE_BLOCKED_BY_SOURCE_COVERAGE" if ready_stock_count == 0 and score_rows_failed == 0 else ("SUCCESS" if score_rows_failed == 0 else "PARTIAL")
    _job_finish(db, score_job, score_job_status, stocks_completed=len(stock_ids) - score_rows_failed, stocks_failed=score_rows_failed, checkpoint_state={"scores": result["scores"], "score_metrics": result["score_metrics"], **score_readiness_checkpoint(db, end, len(stock_ids))})
    result["score_status"] = score_job_status
    rebuilt = _mark_broker_rebuilds_complete(db, pending_broker_rebuilds, end)
    if pending_broker_rebuilds:
        result["broker_source_remediation"] = {
            "pending_at_start": len(pending_broker_rebuilds),
            "rebuilt_from_official_source": len(rebuilt),
            "remaining": len(pending_broker_rebuilds) - len(rebuilt),
        }
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
