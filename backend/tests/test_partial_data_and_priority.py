from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.ingestion import prioritize_stock_ids
from app.main import app
from app.models import PriceDaily, Stock


def test_stock_list_exposes_partial_rows_and_write_time() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    with SessionLocal() as db:
        db.add(Stock(stock_id="9996", stock_name="部分資料測試", market="上市", industry="測試", is_common_stock=True, source_date=date(2026, 8, 27), fetched_at=now))
        db.add(PriceDaily(stock_id="9996", source_date=date(2026, 8, 27), close=123.4, volume=1000, change=1.2, source_dataset="TaiwanStockPrice", fetched_at=now))
        db.commit()
    try:
        with TestClient(app) as client:
            response = client.get("/api/stocks", params={"search": "9996", "page": 1, "page_size": 10})
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["data_status"] == "PARTIAL"
        assert item["data_latest_source_date"] == "2026-08-27"
        assert item["last_updated_at"].startswith("2026-08-27T12:00:00")
        assert item["coverage"]["PriceDataAvailable"] is True
        assert item["data_sources"]["price"]["available"] is True
        assert item["data_sources"]["price"]["row_count"] == 1
    finally:
        with SessionLocal() as db:
            db.query(PriceDaily).filter(PriceDaily.stock_id == "9996").delete()
            db.query(Stock).filter(Stock.stock_id == "9996").delete()
            db.commit()


def test_refresh_order_starts_with_no_data_then_oldest_write() -> None:
    old_write = datetime(2026, 8, 25, 9, tzinfo=timezone.utc)
    new_write = datetime(2026, 8, 26, 9, tzinfo=timezone.utc)
    with SessionLocal() as db:
        db.add_all([
            Stock(stock_id="9993", stock_name="無資料測試", market="上市", is_common_stock=True),
            Stock(stock_id="9994", stock_name="舊資料測試", market="上市", is_common_stock=True),
            Stock(stock_id="9995", stock_name="新資料測試", market="上市", is_common_stock=True),
            PriceDaily(stock_id="9994", source_date=date(2026, 8, 25), close=1, volume=1, source_dataset="TaiwanStockPrice", fetched_at=old_write),
            PriceDaily(stock_id="9995", source_date=date(2026, 8, 26), close=1, volume=1, source_dataset="TaiwanStockPrice", fetched_at=new_write),
        ])
        db.commit()
        try:
            ordered = prioritize_stock_ids(db, ["9995", "9993", "9994"], "TaiwanStockPrice")
        finally:
            db.query(PriceDaily).filter(PriceDaily.stock_id.in_(["9994", "9995"])).delete(synchronize_session=False)
            db.query(Stock).filter(Stock.stock_id.in_(["9993", "9994", "9995"])).delete(synchronize_session=False)
            db.commit()
    assert ordered == ["9993", "9994", "9995"]
