from datetime import date, datetime, timezone
import importlib.util
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, JobRun, PriceDaily, Stock, StockRefreshIssue


def test_audited_reset_requeues_all_stocks_preserves_data_budget_and_is_idempotent(tmp_path):
    path = Path(__file__).resolve().parents[2] / "scripts" / "reset_refresh_issues.py"
    if not path.exists():
        path = Path("/app/scripts/reset_refresh_issues.py")
    spec = importlib.util.spec_from_file_location("refresh_reset_script", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 3, 4, tzinfo=timezone.utc)
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([Stock(stock_id=sid, stock_name=sid, market="test", is_common_stock=True) for sid in ["9001", "9002"]])
        db.add(StockRefreshIssue(stock_id="9001", no_data_attempts=5, status="EXCLUDED", reason_code="NO_DATA", first_attempt_at=now, last_attempt_at=now))
        db.add(PriceDaily(stock_id="9001", source_date=date(2026, 10, 2), close=100, fetched_at=now, source_dataset="TaiwanStockPrice"))
        job = JobRun(dataset="universe_budget_refresh_score", status="RUNNING", started_at=now, checkpoint_state={"stock_ids": ["9002"], "queue_index": 1, "budget": {"used": 300}, "current_stock_progress": {"datasets": {"old": True}}})
        # Use the production dataset identity, never a test-only queue name.
        job.dataset = script.UNIVERSE_BUDGET_REFRESH_DATASET
        db.add(job)
        db.commit()
        receipt = script.reset(db, "test-reset", tmp_path, now=now)
        assert receipt["previously_excluded_count"] == receipt["cleared_issue_count"] == 1
        assert receipt["requeued_job_ids"] == [job.id]
        assert db.query(StockRefreshIssue).count() == 0
        assert db.query(PriceDaily).one().close == 100
        assert job.checkpoint_state["stock_ids"] == ["9001", "9002"]
        assert job.checkpoint_state["budget"]["used"] == 300
        assert job.checkpoint_state["current_stock_progress"] == {}
        backup = json.loads(Path(receipt["backup_path"]).read_text(encoding="utf-8"))
        assert backup["issues"][0]["no_data_attempts"] == 5
        db.add(StockRefreshIssue(stock_id="9001", no_data_attempts=1, status="RETRY_PENDING", reason_code="NO_DATA", first_attempt_at=now, last_attempt_at=now))
        db.commit()
        assert script.reset(db, "test-reset", tmp_path, now=now)["replayed"]
        assert db.query(StockRefreshIssue).one().no_data_attempts == 1
        assert len(list(tmp_path.glob("*.json"))) == 1
    engine.dispose()
