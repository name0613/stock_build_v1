from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .calendar import expected_trading_sessions, missing_sessions
from .features import build_features
from .finmind import FinMindClient
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
    common_type = security_type in {"股票", "普通股", "Common Stock", ""}
    supported_market = market in {"上市", "上櫃", "興櫃", "TWSE", "TPEx", "ESB"}
    if not stock_id.isdigit() or len(stock_id) != 4 or not supported_market or not common_type or any(term.lower() in f"{name} {security_type} {industry}".lower() for term in excluded_terms):
        return None
    return {"stock_id": stock_id, "stock_name": name or stock_id, "market": market, "industry": industry or None, "security_type": security_type or "股票", "is_common_stock": True, "source_date": _as_date(_v(row, "date", "source_date")), "fetched_at": fetched_at or _now()}


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
    return {"stock_id": stock_id, "source_date": source_date, "securities_trader_id": trader_id, "securities_trader_name": _v(row, "securities_trader_name", "securities_trader", "券商名稱"), "buy_volume": buy, "sell_volume": sell, "net_volume": net_value, "buy_amount": buy * price if buy is not None and price is not None else _num(_v(row, "buy_amount", "買進金額")), "sell_amount": sell * price if sell is not None and price is not None else _num(_v(row, "sell_amount", "賣出金額")), "avg_buy_price": avg_buy if avg_buy is not None else price, "avg_sell_price": avg_sell if avg_sell is not None else price, "source_dataset": dataset, "fetched_at": fetched_at or _now()}


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


def sync_universe(db: Session, client: FinMindClient) -> int:
    records, meta = client.fetch("TaiwanStockInfo")
    count = ingest_records(db, "TaiwanStockInfo", records)
    active_ids = {normalized["stock_id"] for row in records if (normalized := normalize_stock(row)) is not None}
    for existing in db.scalars(select(Stock).where(Stock.is_common_stock.is_(True))).all():
        if existing.stock_id not in active_ids:
            existing.is_common_stock = False
    db.commit()
    info_dates = [_as_date(_v(row, "date", "source_date")) for row in records]
    latest = max((value for value in info_dates if value is not None), default=None)
    _mark_sync(db, "TaiwanStockInfo", "SUCCESS" if count else "PARTIAL", count, latest or _as_date(meta.get("source_date")), fetched_at=_now())
    return count


def sync_stock_dataset(db: Session, client: FinMindClient, dataset: str, stock_id: str, start_date: str, end_date: str) -> int:
    records, meta = client.fetch(dataset, stock_id, start_date, end_date)
    count = ingest_records(db, dataset, records)
    _mark_sync(db, dataset, "SUCCESS" if count else "NO_DATA", count, _as_date(meta.get("source_date")), "NO_DATA" if not count else None, fetched_at=_now())
    return count


def _mark_sync(db: Session, dataset: str, status: str, records: int, latest: date | None, error_code: str | None = None, error: str | None = None, *, fetched_at: datetime | None = None, metadata: dict[str, Any] | None = None) -> None:
    item = db.get(DataSyncStatus, dataset)
    if item is None:
        item = DataSyncStatus(dataset=dataset, status=status, records=records)
        db.add(item)
    item.status = status
    item.records = records
    item.usable_records = records
    item.stored_records = records
    item.last_attempt_at = fetched_at or _now()
    item.latest_source_date = latest
    if status == "SUCCESS" and records > 0:
        item.last_successful_sync = fetched_at or _now()
        item.last_fetch_at = fetched_at or _now()
        item.staleness_state = "FRESH"
    elif status in {"NO_DATA", "PARTIAL"}:
        item.staleness_state = status
    item.last_error_code = error_code
    item.last_error = error[:500] if error else None
    if metadata:
        item.metadata_json = {**(item.metadata_json or {}), **metadata}
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
    revisions = db.scalars(select(SourceRevision).where(SourceRevision.dataset == dataset, SourceRevision.stock_id == stock_id, SourceRevision.source_date <= as_of, SourceRevision.fetched_at <= cutoff).order_by(SourceRevision.fetched_at, SourceRevision.id)).all()
    revision_by_key: dict[str, SourceRevision] = {}
    for revision in revisions:
        revision_by_key[revision.natural_key] = revision

    model_rows = db.scalars(
        select(model)
        .where(model.stock_id == stock_id, model.source_date <= as_of, model.fetched_at <= cutoff)
        .order_by(model.source_date.desc(), model.id.desc())
        .limit(limit)
    ).all()
    merged: dict[str, tuple[dict[str, Any], str, date | None, int]] = {}
    for row in model_rows:
        payload = {key: getattr(row, key) for key in row.__table__.columns.keys()}
        merged[_natural_key(dataset, payload)] = (payload, _payload_content_hash(payload), row.source_date, row.id)
    for revision in revision_by_key.values():
        payload = dict(revision.payload)
        merged[revision.natural_key] = (payload, _payload_content_hash(payload), revision.source_date, revision.id)

    selected = sorted(merged.values(), key=lambda item: (item[2] or date.min, item[3]))[-limit:]
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
    foreign_ok, foreign_missing = _daily_coverage(foreign, as_of, 2)
    price_ok, price_missing = _daily_coverage(prices, as_of, 20)
    broker_dates = {_as_date(row.get("source_date") or row.get("date")) for row in brokers}
    broker_dates.discard(None)
    expected_broker = expected_trading_sessions(as_of, 20)
    broker_missing = [day.isoformat() for day in expected_broker if day not in broker_dates]
    holding_coverage = features.get("HoldingDistributionCoverage") or {}
    coverage = {
        "InstitutionalDataAvailable": inst_ok and features.get("InstitutionalNet20D") is not None,
        "ForeignHoldingDataAvailable": foreign_ok,
        "HoldingDistributionAvailable": bool(holdings) and features.get("LargeHolder400Change4W") is not None,
        "BrokerDataAvailable": not broker_missing,
        "PriceDataAvailable": price_ok,
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
        current.config = SCORE_MANIFEST
        current.manifest_hash = FORMULA_HASH
        db.commit()


async def catch_up(db: Session, client: FinMindClient) -> dict[str, Any]:
    """Run the complete scheduled pipeline for the dynamic common-stock universe."""
    end = date.today()
    start = end - timedelta(days=45)
    result: dict[str, Any] = {"status": "SUCCESS", "datasets": {}, "scores": {}}
    required = ["TaiwanStockInstitutionalInvestorsBuySellWide", "TaiwanStockShareholding", "TaiwanStockHoldingSharesPer", "TaiwanStockPrice"]
    info_job = _job_start(db, "TaiwanStockInfo", end, end)
    try:
        info_count = sync_universe(db, client)
        _job_finish(db, info_job, "SUCCESS" if info_count else "PARTIAL", records=info_count)
        result["datasets"]["TaiwanStockInfo"] = {"status": "SUCCESS" if info_count else "PARTIAL", "records": info_count}
    except Exception as exc:
        _job_finish(db, info_job, "FAILED", error_code=getattr(exc, "code", "UNEXPECTED"), error=str(exc))
        result["datasets"]["TaiwanStockInfo"] = {"status": "FAILED", "error_code": getattr(exc, "code", "UNEXPECTED")}
        result["status"] = "PARTIAL"
    for dataset in required:
        job = _job_start(db, dataset, start, end)
        try:
            records, meta = client.fetch(dataset, start_date=start.isoformat(), end_date=end.isoformat())
            count = ingest_records(db, dataset, records)
            status = "SUCCESS" if count else "PARTIAL"
            _mark_sync(db, dataset, status, count, _as_date(meta.get("source_date")), "NO_DATA" if not count else None, fetched_at=_now(), metadata={"requested_start": start.isoformat(), "requested_end": end.isoformat(), "last_usable_records": count})
            _job_finish(db, job, status, records=count, error_code=None if count else "NO_DATA")
            result["datasets"][dataset] = {"status": status, "records": count}
            if status != "SUCCESS":
                result["status"] = "PARTIAL"
        except Exception as exc:
            code = getattr(exc, "code", "UNEXPECTED")
            _job_finish(db, job, "FAILED", error_code=code, error=str(exc))
            _mark_sync(db, dataset, "FAILED", 0, None, code, str(exc), fetched_at=_now())
            result["datasets"][dataset] = {"status": "FAILED", "error_code": code}
            result["status"] = "PARTIAL"
    stock_ids = list(db.scalars(select(Stock.stock_id).where(Stock.is_common_stock.is_(True))).all())
    broker_job = _job_start(db, "TaiwanStockTradingDailyReport", end, end, stocks_attempted=len(stock_ids))
    broker_metrics: dict[str, Any] = {}
    try:
        broker_metrics = await client.fetch_broker_stocks(stock_ids, end.isoformat(), end.isoformat())
        broker_records = broker_metrics.pop("_records", [])
        stored = ingest_records(db, "TaiwanStockTradingDailyReport", broker_records) if broker_records else 0
        checkpoint_complete = broker_metrics.get("skipped_checkpoint", 0) >= len(stock_ids)
        broker_status = "SUCCESS" if broker_metrics.get("failed", 0) == 0 and (broker_metrics.get("rows", 0) > 0 or checkpoint_complete or not stock_ids) else "PARTIAL"
        _mark_sync(db, "TaiwanStockTradingDailyReport", broker_status, stored, end if stored else None, None if broker_status == "SUCCESS" else "BROKER_PARTIAL", fetched_at=_now(), metadata=broker_metrics)
        _job_finish(db, broker_job, broker_status, records=stored, retry_count=broker_metrics.get("retries", 0), stocks_completed=broker_metrics.get("success", 0), stocks_failed=broker_metrics.get("failed", 0), checkpoint_state=broker_metrics)
        result["datasets"]["TaiwanStockTradingDailyReport"] = {**broker_metrics, "stored_records": stored}
        if broker_status != "SUCCESS":
            result["status"] = "PARTIAL"
    except Exception as exc:
        code = getattr(exc, "code", "UNEXPECTED")
        _job_finish(db, broker_job, "FAILED", error_code=code, error=str(exc), stocks_failed=len(stock_ids))
        result["datasets"]["TaiwanStockTradingDailyReport"] = {"status": "FAILED", "error_code": code}
        result["status"] = "PARTIAL"
    existing_scores = db.scalars(select(AccumulationScore).where(AccumulationScore.source_date == end, AccumulationScore.score_version == SCORE_VERSION, AccumulationScore.knowledge_cutoff.is_not(None))).all()
    reuse_ready = len(existing_scores) >= len(stock_ids) and all(len(score.input_source_hashes or []) >= 20 for score in existing_scores)
    if broker_metrics.get("skipped_checkpoint", 0) >= len(stock_ids) and reuse_ready:
        score_job = _job_start(db, "score", end, end, stocks_attempted=len(stock_ids))
        _job_finish(db, score_job, "SUCCESS", stocks_completed=len(stock_ids), checkpoint_state={"reused_existing_scores": True})
        result["scores"] = {status: count for status, count in db.execute(select(AccumulationScore.status, func.count()).where(AccumulationScore.source_date == end, AccumulationScore.score_version == SCORE_VERSION).group_by(AccumulationScore.status)).all()}
        return result
    score_job = _job_start(db, "score", end, end, stocks_attempted=len(stock_ids))
    for stock_id in stock_ids:
        try:
            score = calculate_stock_features_and_score(db, stock_id, end)
            result["scores"][score.status] = result["scores"].get(score.status, 0) + 1
        except Exception:
            result["status"] = "PARTIAL"
            result["scores"]["FAILED"] = result["scores"].get("FAILED", 0) + 1
    _job_finish(db, score_job, "SUCCESS" if result["scores"].get("FAILED", 0) == 0 else "PARTIAL", stocks_completed=len(stock_ids) - result["scores"].get("FAILED", 0), stocks_failed=result["scores"].get("FAILED", 0))
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
