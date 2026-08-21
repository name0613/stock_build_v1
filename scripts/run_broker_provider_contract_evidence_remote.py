"""Capture sanitized live broker-report contract evidence from the deployed worker."""
from __future__ import annotations

import json
import os
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "/volume1/docker/tw-accumulation-evidence"

REMOTE_PROBE = r'''
import json
from pathlib import Path

from app.config import get_settings
from app.finmind import FinMindClient, FinMindError


def revision():
    path = Path("/app/build-metadata.json")
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8")).get("source_revision")
        if value:
            return str(value)
    return "unknown"


def summarize(rows, stock_id, requested_date):
    def positive(value):
        try:
            return float(value or 0) > 0
        except (TypeError, ValueError):
            return False

    def zero(value):
        try:
            return float(value or 0) == 0
        except (TypeError, ValueError):
            return False

    return {
        "row_count": len(rows),
        "unique_stock_ids": sorted({str(row.get("stock_id")) for row in rows}),
        "unique_dates": sorted({str(row.get("date"))[:10] for row in rows}),
        "all_rows_match_requested_stock_and_session": all(
            str(row.get("stock_id")) == stock_id
            and str(row.get("date"))[:10] == requested_date
            for row in rows
        ),
        "required_fields_present": all(
            {"stock_id", "date", "securities_trader_id", "buy", "sell"} <= set(row)
            for row in rows
        ),
        "provider_row_validated_count": sum(
            row.get("provider_row_validated") is True for row in rows
        ),
        "active_buyer_branch_count": sum(positive(row.get("buy")) for row in rows),
        "active_seller_branch_count": sum(positive(row.get("sell")) for row in rows),
        "buyer_zero_count": sum(zero(row.get("buy")) for row in rows),
        "seller_zero_count": sum(zero(row.get("sell")) for row in rows),
        "branch_ids_present": sum(
            bool(str(row.get("securities_trader_id") or "").strip()) for row in rows
        ),
    }


def run_case(client, stock_id, requested_date):
    request = {
        "dataset": "TaiwanStockTradingDailyReport",
        "endpoint": "/api/v4/taiwan_stock_trading_daily_report",
        "query_mode": "per_stock_per_session",
        "data_id": stock_id,
        "date": requested_date,
        "must_need_date": "true",
        "persist_raw": False,
    }
    try:
        rows, meta = client.fetch(
            "TaiwanStockTradingDailyReport",
            data_id=stock_id,
            start_date=requested_date,
            end_date=requested_date,
            persist_raw=False,
        )
        return {
            "request": request,
            "response": {
                "http_status": meta.get("provider_http_status"),
                "application_status": meta.get("provider_application_status"),
                "pagination_complete": meta.get("pagination_complete"),
                "provider_report_complete": meta.get("provider_report_complete"),
                "provider_contract_version": meta.get("provider_contract_version"),
                "provider_row_contract_version": meta.get("provider_row_contract_version"),
                "provider_row_validated": meta.get("provider_row_validated"),
                "provider_contract_reason": meta.get("provider_contract_reason"),
                "empty_is_valid": meta.get("empty_is_valid"),
                "empty_reason": meta.get("empty_reason"),
                "rows": summarize(rows, stock_id, requested_date),
            },
        }
    except FinMindError as exc:
        return {
            "request": request,
            "error": {"code": exc.code, "status_code": exc.status_code},
        }


client = FinMindClient(get_settings())
print(json.dumps({
    "format": "live-broker-provider-contract-evidence-v1",
    "source_revision": revision(),
    "provider": "FinMind",
    "dataset": "TaiwanStockTradingDailyReport",
    "cases": [
        run_case(client, "2330", "2026-08-20"),
        run_case(client, "2311", "2026-07-27"),
    ],
    "semantics": {
        "omitted_branch_as_zero_proven": False,
        "empty_report_semantics_authoritatively_proven": False,
        "production_policy": "unverified_empty_or_incomplete_report_remains_retryable_and_is_excluded_from_scoring",
    },
    "official_contract_context": [
        {
            "url": "https://finmind.github.io/en/tutor/TaiwanMarket/Chip/",
            "claims": ["query by stock_id is supported", "one day is provided per request", "Sponsor access is required"],
        },
        {
            "url": "https://api.finmindtrade.com/docs",
            "claims": ["the trading daily report endpoint is documented by the provider"],
        },
    ],
    "sanitized": True,
    "secrets_included": False,
}, ensure_ascii=False))
'''


def main() -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        os.environ.get("NAS_HOST", "192.168.31.138"),
        username=os.environ["NAS_USER"],
        password=os.environ["NAS_PASSWORD"],
        look_for_keys=False,
        allow_agent=False,
        timeout=15,
    )
    try:
        command = f"cd {PROJECT} && docker compose exec -T worker python -"
        stdin, stdout, stderr = client.exec_command(
            f"sudo -S -p '' sh -c {json.dumps(command)}"
        )
        stdin.write(os.environ["NAS_PASSWORD"] + "\n")
        stdin.write(REMOTE_PROBE)
        stdin.flush()
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8", "replace").strip()
        error = stderr.read().decode("utf-8", "replace")
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"remote broker evidence failed: {error[:500]}")
    finally:
        client.close()
    evidence = json.loads(output)
    target = ROOT / "deployment_evidence/BROKER_PROVIDER_CONTRACT_EVIDENCE.json"
    target.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "path": str(target),
        "source_revision": evidence.get("source_revision"),
        "secrets_included": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
