from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.finmind import FORBIDDEN_DATASETS, FinMindClient, FinMindError
from app.ingestion import ingest_records, normalize_stock
from app.models import Base, InstitutionalDaily, Stock


def test_forbidden_datasets_are_rejected() -> None:
    client = FinMindClient(Settings(raw_root=Path("data/raw-test")))
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockMarginPurchaseShortSale")
    assert exc.value.code == "FORBIDDEN_DATASET"
    assert "token" not in str(exc.value).lower()


def test_allowlist_excludes_unknown_dataset() -> None:
    client = FinMindClient(Settings(raw_root=Path("data/raw-test")))
    with pytest.raises(FinMindError) as exc:
        client.fetch("TaiwanStockPriceTick")
    assert exc.value.code == "FORBIDDEN_DATASET"


def test_capability_dataset_list_is_s_only() -> None:
    assert "TaiwanStockTradingDailyReportSecIdAgg" not in FORBIDDEN_DATASETS
    assert "TaiwanStockGovernmentBankBuySell" in FORBIDDEN_DATASETS


def test_universe_excludes_etf_category_from_industry() -> None:
    assert normalize_stock({"stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "industry_category": "ETF"}) is None
    assert normalize_stock({"stock_id": "2330", "stock_name": "台積電", "type": "twse", "industry_category": "半導體業"})["is_common_stock"] is True


def test_stock_datasets_ignore_non_common_rows_before_foreign_key_insert() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Stock(stock_id="2330", stock_name="台積電", market="上市", is_common_stock=True))
        db.commit()
        count = ingest_records(
            db,
            "TaiwanStockInstitutionalInvestorsBuySellWide",
            [
                {"stock_id": "2330", "date": "2026-08-20", "Foreign_Investor_buy": 10, "Foreign_Investor_sell": 2},
                {"stock_id": "00400A", "date": "2026-08-20", "Foreign_Investor_buy": 10, "Foreign_Investor_sell": 2},
            ],
        )
        assert count == 1
        assert db.scalar(select(InstitutionalDaily.stock_id)) == "2330"
