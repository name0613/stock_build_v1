"""Hermetic API test environment with a disposable, deterministic SQLite database."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest


_database_path = Path(tempfile.gettempdir()) / f"tw_accumulation_pytest_{os.getpid()}.db"
_raw_path = Path(tempfile.gettempdir()) / f"tw_accumulation_pytest_raw_{os.getpid()}"
_database_path.unlink(missing_ok=True)
_raw_path.mkdir(parents=True, exist_ok=True)

# These must be set before any app module imports get_settings() or creates the engine.
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{_database_path.as_posix()}"
os.environ["RAW_ROOT"] = str(_raw_path)
os.environ["ALLOW_DEMO_DATA"] = "false"
os.environ.pop("DATABASE_PASSWORD_FILE", None)
sys.path.insert(0, str(Path(__file__).parents[1]))

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    AccumulationFeature,
    AccumulationScore,
    BrokerDaily,
    ForeignShareholdingDaily,
    HoldingDistribution,
    InstitutionalDaily,
    PriceDaily,
    Stock,
)
from app.scoring import BROKER_ROW_CONTRACT_VERSION, FORMULA_HASH, HOLDING_CANONICAL_LEVELS, SCORE_VERSION  # noqa: E402


def _seed_database() -> None:
    init_db()
    now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    latest = date(2026, 8, 20)
    stocks = [
        ("2330", "台積電", "上市", "半導體業"),
        ("2317", "鴻海", "上市", "電子零組件業"),
        ("1101", "台泥", "上市", "水泥工業"),
        ("1102", "亞泥", "上市", "水泥工業"),
        ("1103", "嘉泥", "上市", "水泥工業"),
        ("1104", "環泥", "上市", "水泥工業"),
    ]
    with SessionLocal() as db:
        for index, (stock_id, name, market, industry) in enumerate(stocks):
            db.add(Stock(stock_id=stock_id, stock_name=name, market=market, industry=industry, is_common_stock=True, source_date=latest, fetched_at=now))
            db.add(PriceDaily(stock_id=stock_id, source_date=latest, close=100 + index, volume=1000 + index, change=index / 10, source_dataset="TaiwanStockPrice", fetched_at=now))
            if stock_id in {"2330", "2317", "1101", "1102", "1103", "1104"}:
                score = {"2330": 88.0, "2317": 75.0, "1101": 55.0, "1102": 20.0, "1103": None, "1104": None}[stock_id]
                status = "STRONG_ACCUMULATION" if stock_id == "2330" else ("ACCUMULATION" if stock_id == "2317" else ("DATA_INSUFFICIENT" if stock_id in {"1103", "1104"} else "WATCH"))
                coverage = {"institutional": stock_id not in {"1103", "1104"}, "foreign_holding": stock_id not in {"1103", "1104"}, "holding_distribution": stock_id not in {"1103", "1104"}, "broker": stock_id not in {"1103", "1104"}, "price": True}
                values = {"ForeignNet5D": 100.0 - index, "ForeignNet20D": 450.0 - index, "InvestmentTrustNet5D": 20.0, "InvestmentTrustNet20D": 80.0, "ForeignShareRatioChange20D": 0.03, "LargeHolder400Change4W": 0.02, "TopBrokerNetBuy20D": 120.0, "BrokerPersistenceScore": 0.8}
                db.add(AccumulationFeature(stock_id=stock_id, source_date=latest, values=values, coverage=coverage, latest_source_date=latest.isoformat(), calculated_at=now, knowledge_cutoff=now, input_snapshot_hash=f"{index + 1:064d}"))
                db.add(AccumulationScore(stock_id=stock_id, source_date=latest, score=score, status=status, score_version=SCORE_VERSION, components={"S": score or 0.0}, explanation=[{"label": "S-level evidence", "value": score or 0.0, "detail": "deterministic fixture"}], coverage=coverage, calculated_at=now, knowledge_cutoff=now, input_snapshot_hash=f"{index + 1:064d}", input_source_hashes=[f"{index + 1:064d}"], formula_hash=FORMULA_HASH))

        for offset in (0, 1, 2):
            day = date(2026, 8, 18 + offset)
            db.add(InstitutionalDaily(stock_id="2330", source_date=day, foreign_net=100 + offset, investment_trust_net=20, dealer_net=5, institutional_net=125 + offset, source_dataset="TaiwanStockInstitutionalInvestorsBuySellWide", fetched_at=now))
            db.add(ForeignShareholdingDaily(stock_id="2330", source_date=day, foreign_investment_shares=100000 + offset, foreign_investment_shares_ratio=0.5 + offset / 100, number_of_shares_issued=200000, source_dataset="TaiwanStockShareholding", fetched_at=now))
            for level_index, (level, threshold) in enumerate(HOLDING_CANONICAL_LEVELS):
                db.add(HoldingDistribution(stock_id="2330", source_date=day, holding_shares_level=level, holding_shares_threshold=threshold, people=10 + level_index, percent=1 + offset + level_index / 10, shares=threshold, unit="shares", source_dataset="TaiwanStockHoldingSharesPer", fetched_at=now))
            db.add(BrokerDaily(stock_id="2330", source_date=day, securities_trader_id="A", securities_trader_name="fixture broker", buy_volume=100 + offset, sell_volume=20, net_volume=80 + offset, source_dataset="TaiwanStockTradingDailyReport", provider_row_validated=True, provider_row_contract_version=BROKER_ROW_CONTRACT_VERSION, fetched_at=now))
        db.commit()


@pytest.fixture(scope="session", autouse=True)
def hermetic_database() -> None:
    _seed_database()
    yield
    engine.dispose()
    _database_path.unlink(missing_ok=True)
    for path in _raw_path.glob("*"):
        if path.is_file():
            path.unlink(missing_ok=True)
    _raw_path.rmdir()
