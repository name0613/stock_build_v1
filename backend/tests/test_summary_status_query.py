from datetime import date, datetime, timedelta, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.main import _canonical_statuses
from app.models import AccumulationScore, Stock
from app.scoring import SCORE_VERSION


def test_summary_uses_latest_numeric_common_stock_status_without_loading_provenance():
    engine = create_engine("sqlite://")
    Stock.__table__.create(engine)
    AccumulationScore.__table__.create(engine)
    day = date(2026, 9, 11)
    now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add_all([
            Stock(stock_id="A", stock_name="A", market="listed", is_common_stock=True),
            Stock(stock_id="B", stock_name="B", market="listed", is_common_stock=True),
            Stock(stock_id="ETF", stock_name="ETF", market="listed", is_common_stock=False),
        ])
        def add(stock_id, source_date, status, offset, *, score=80, version=SCORE_VERSION, bound=True):
            db.add(AccumulationScore(
                stock_id=stock_id, source_date=source_date, status=status, score=score,
                score_version=version, calculated_at=now + timedelta(seconds=offset // 2),
                knowledge_cutoff=now + timedelta(seconds=offset) if bound else None,
                input_source_hashes=["a" * 64] * 1000,
            ))
        add("A", day - timedelta(days=1), "WATCH", 0)
        add("A", day, "ACCUMULATION", 2)
        add("A", day, "STRONG_ACCUMULATION", 3)  # Same timestamp; newest id wins.
        add("A", day + timedelta(days=1), "DATA_INSUFFICIENT", 4, score=None)
        add("A", day + timedelta(days=1), "WATCH", 5, version="old-version")
        add("A", day + timedelta(days=1), "WATCH", 6, bound=False)
        add("ETF", day, "STRONG_ACCUMULATION", 7)
        db.commit()
        db.expunge_all()
        statements = []
        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        assert _canonical_statuses(db) == (2, {"A": "STRONG_ACCUMULATION", "B": "DATA_INSUFFICIENT"})
        assert _canonical_statuses(db, day - timedelta(days=1)) == (2, {"A": "WATCH", "B": "DATA_INSUFFICIENT"})
        assert not db.identity_map  # Summary must not hydrate historical ORM rows.
        assert all("input_source_hashes" not in sql and "explanation" not in sql for sql in statements)
    engine.dispose()
