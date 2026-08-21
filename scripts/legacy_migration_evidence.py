"""Execute the legacy-normalized-row SourceRevision backfill on PostgreSQL."""
from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

try:
    from backend.app.db import engine
except ModuleNotFoundError:  # running from the backend image
    from app.db import engine


LEGACY_MIGRATION = Path(__file__).resolve().parents[1] / "migrations/004_seed_legacy_source_revisions.sql"
MIGRATION_DIR = LEGACY_MIGRATION.parent


def run() -> dict[str, object]:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("legacy migration evidence requires PostgreSQL")
    schema = f"legacy_migration_{uuid.uuid4().hex[:12]}"
    fetched_at = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema}"))
            connection.execute(text(f"SET LOCAL search_path TO {schema}, public"))
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
            connection.execute(text("CREATE TABLE stocks (stock_id VARCHAR(16) PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE institutional_daily (id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16), source_date DATE, foreign_net DOUBLE PRECISION, foreign_dealer_self_net DOUBLE PRECISION, investment_trust_net DOUBLE PRECISION, dealer_net DOUBLE PRECISION, dealer_self_net DOUBLE PRECISION, dealer_hedging_net DOUBLE PRECISION, institutional_net DOUBLE PRECISION, source_dataset VARCHAR(100), fetched_at TIMESTAMPTZ)"))
            connection.execute(text("CREATE TABLE foreign_shareholding_daily (id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16), source_date DATE, foreign_investment_shares DOUBLE PRECISION, foreign_investment_shares_ratio DOUBLE PRECISION, number_of_shares_issued DOUBLE PRECISION, recently_declare_date VARCHAR(32), source_dataset VARCHAR(100), fetched_at TIMESTAMPTZ)"))
            connection.execute(text("CREATE TABLE holding_distribution (id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16), source_date DATE, holding_shares_level VARCHAR(128), holding_shares_threshold INTEGER, people DOUBLE PRECISION, percent DOUBLE PRECISION, shares DOUBLE PRECISION, unit VARCHAR(32), source_dataset VARCHAR(100), fetched_at TIMESTAMPTZ)"))
            connection.execute(text("CREATE TABLE broker_daily (id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16), source_date DATE, securities_trader_id VARCHAR(32), securities_trader_name VARCHAR(128), buy_volume DOUBLE PRECISION, sell_volume DOUBLE PRECISION, net_volume DOUBLE PRECISION, buy_amount DOUBLE PRECISION, sell_amount DOUBLE PRECISION, avg_buy_price DOUBLE PRECISION, avg_sell_price DOUBLE PRECISION, source_dataset VARCHAR(100), fetched_at TIMESTAMPTZ)"))
            connection.execute(text("CREATE TABLE price_daily (id BIGSERIAL PRIMARY KEY, stock_id VARCHAR(16), source_date DATE, close DOUBLE PRECISION, volume DOUBLE PRECISION, change DOUBLE PRECISION, source_dataset VARCHAR(100), fetched_at TIMESTAMPTZ)"))
            connection.execute(text("CREATE TABLE source_revisions (id BIGSERIAL PRIMARY KEY, dataset VARCHAR(100) NOT NULL, stock_id VARCHAR(16), source_date DATE, natural_key VARCHAR(255) NOT NULL, payload JSONB NOT NULL, content_hash VARCHAR(64) NOT NULL, fetched_at TIMESTAMPTZ NOT NULL, UNIQUE(dataset, natural_key, content_hash))"))
            connection.execute(text("CREATE TABLE data_sync_status (dataset VARCHAR(100) PRIMARY KEY, status VARCHAR(32) NOT NULL, records INTEGER NOT NULL DEFAULT 0, metadata_json JSON NOT NULL DEFAULT '{}'::json, rows_received_this_attempt INTEGER NOT NULL DEFAULT 0, rows_accepted_this_attempt INTEGER NOT NULL DEFAULT 0, rows_rejected_this_attempt INTEGER NOT NULL DEFAULT 0, rows_versioned_this_attempt INTEGER NOT NULL DEFAULT 0, observations_reused_this_attempt INTEGER NOT NULL DEFAULT 0, stored_rows_total INTEGER NOT NULL DEFAULT 0)"))
            connection.execute(text("INSERT INTO stocks(stock_id) VALUES ('2330')"))
            connection.execute(text("INSERT INTO institutional_daily(stock_id, source_date, source_dataset, fetched_at, institutional_net) VALUES ('2330', '2026-08-20', 'TaiwanStockInstitutionalInvestorsBuySellWide', :fetched_at, 10)"), {"fetched_at": fetched_at})
            connection.execute(text("INSERT INTO foreign_shareholding_daily(stock_id, source_date, source_dataset, fetched_at, foreign_investment_shares_ratio) VALUES ('2330', '2026-08-20', 'TaiwanStockShareholding', :fetched_at, 40)"), {"fetched_at": fetched_at})
            connection.execute(text("INSERT INTO holding_distribution(stock_id, source_date, holding_shares_level, source_dataset, fetched_at, percent) VALUES ('2330', '2026-08-20', '400,001-600,000', 'TaiwanStockHoldingSharesPer', :fetched_at, 10)"), {"fetched_at": fetched_at})
            connection.execute(text("INSERT INTO broker_daily(stock_id, source_date, securities_trader_id, source_dataset, fetched_at, net_volume) VALUES ('2330', '2026-08-20', 'PIT', 'TaiwanStockTradingDailyReport', :fetched_at, 100)"), {"fetched_at": fetched_at})
            connection.execute(text("INSERT INTO price_daily(stock_id, source_date, source_dataset, fetched_at, close) VALUES ('2330', '2026-08-20', 'TaiwanStockPrice', :fetched_at, 100)"), {"fetched_at": fetched_at})
            connection.execute(
                text("INSERT INTO data_sync_status(dataset, status, records, metadata_json, rows_received_this_attempt, rows_accepted_this_attempt, rows_rejected_this_attempt, rows_versioned_this_attempt, observations_reused_this_attempt, stored_rows_total) VALUES ('TaiwanStockHoldingSharesPer', 'SUCCESS', 19995, CAST(:metadata_json AS json), 0, 19995, 0, 19995, 0, 19995)"),
                {"metadata_json": json.dumps({"legacy": True})},
            )
            connection.execute(text(LEGACY_MIGRATION.read_text(encoding="utf-8")))
            initial_count = connection.execute(text("SELECT count(*) FROM source_revisions")).scalar_one()
            preserved = connection.execute(text("SELECT count(*) FROM source_revisions WHERE fetched_at = :fetched_at"), {"fetched_at": fetched_at}).scalar_one()
            connection.execute(text("INSERT INTO source_revisions(dataset, stock_id, source_date, natural_key, payload, content_hash, fetched_at) VALUES ('TaiwanStockInstitutionalInvestorsBuySellWide', '2330', '2026-08-20', :natural_key, :payload, :content_hash, :fetched_at)"), {"natural_key": '{"source_date":"2026-08-20","stock_id":"2330"}', "payload": '{"institutional_net":99}', "content_hash": "b" * 64, "fetched_at": datetime(2026, 8, 20, 13, tzinfo=timezone.utc)})
            corrected_count = connection.execute(text("SELECT count(*) FROM source_revisions WHERE dataset = 'TaiwanStockInstitutionalInvestorsBuySellWide'")).scalar_one()
            connection.execute(text((MIGRATION_DIR / "009_version_sync_attempt_counters.sql").read_text(encoding="utf-8")))
            connection.execute(text((MIGRATION_DIR / "010_broker_observed_row_contract.sql").read_text(encoding="utf-8")))
            sync_probe = connection.execute(text("SELECT metadata_json::text, rows_received_this_attempt, rows_accepted_this_attempt, rows_rejected_this_attempt, rows_versioned_this_attempt, observations_reused_this_attempt, counter_attempt_id, counter_semantics_version, counters_are_current_attempt FROM data_sync_status WHERE dataset = 'TaiwanStockHoldingSharesPer'" )).one()
            metadata_type = connection.execute(text("SELECT data_type FROM information_schema.columns WHERE table_schema = :schema AND table_name = 'data_sync_status' AND column_name = 'metadata_json'"), {"schema": schema}).scalar_one()
            broker_contract = connection.execute(text("SELECT provider_row_validated, provider_row_contract_version FROM broker_daily WHERE stock_id = '2330'" )).one()
        migration_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(MIGRATION_DIR.glob("*.sql"))}
        sync_metadata = json.loads(sync_probe[0])
        counter_reset_pass = list(sync_probe[1:6]) == [0, 0, 0, 0, 0] and sync_probe[6] is None and sync_probe[7] == "legacy-pre-v5-reset-v1" and sync_probe[8] is False
        legacy_snapshot = sync_metadata.get("legacy_pre_v5_counter_snapshot") or {}
        snapshot_pass = legacy_snapshot.get("rows_received") == 0 and legacy_snapshot.get("rows_accepted") == 19995 and legacy_snapshot.get("rows_versioned") == 19995 and legacy_snapshot.get("stored_rows_total") == 19995
        return {"database_dialect": engine.dialect.name, "temporary_schema": schema, "migration": LEGACY_MIGRATION.name, "migration_set": migration_hashes, "migration_count": len(migration_hashes), "legacy_rows_seeded": 5, "source_revisions_after_backfill": initial_count, "fetched_at_preserved_rows": preserved, "correction_revision_count": corrected_count, "backfill_pass": initial_count == 5 and preserved == 5, "correction_preserves_original_pass": corrected_count == 2, "legacy_sync_counter_probe": {"metadata_type": metadata_type, "jsonb_normalized": metadata_type == "jsonb", "legacy_snapshot": legacy_snapshot, "snapshot_preserved_pass": snapshot_pass, "current_attempt_counters_reset_pass": counter_reset_pass}, "legacy_broker_contract_probe": {"provider_row_validated": broker_contract[0], "provider_row_contract_version": broker_contract[1], "legacy_completeness_not_promoted_pass": broker_contract[0] is False and broker_contract[1] is None}, "overall_pass": initial_count == 5 and preserved == 5 and corrected_count == 2 and metadata_type == "jsonb" and snapshot_pass and counter_reset_pass and broker_contract[0] is False and broker_contract[1] is None, "secrets_included": False}
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))


if __name__ == "__main__":
    result = run()
    output = Path("deployment_evidence/LEGACY_MIGRATION_EVIDENCE.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "temporary_schema"}, ensure_ascii=False))
