from __future__ import annotations

import json
from pathlib import Path

from app.finmind import _broker_report_contract
from app.scoring import BROKER_ROW_CONTRACT_VERSION, HOLDING_CANONICAL_LEVELS, HOLDING_SCHEMA_VERSION, holding_schema_state


def _fixtures_root() -> Path:
    for root in (Path(__file__).resolve().parents[2], Path(__file__).resolve().parents[1]):
        candidate = root / "fixtures"
        if candidate.is_dir():
            return candidate
    raise RuntimeError("contract fixtures are missing from the source or container layout")


FIXTURES = _fixtures_root()


def test_holding_contract_fixture_matches_executable_schema() -> None:
    fixture = json.loads((FIXTURES / "holding_shares_level_v1.json").read_text(encoding="utf-8"))
    assert fixture["schema_version"] == HOLDING_SCHEMA_VERSION
    assert [(item["label"], item["threshold"]) for item in fixture["canonical_levels"]] == list(HOLDING_CANONICAL_LEVELS)
    rows = [{"HoldingSharesLevel": item["label"], "percent": 1, "people": 1, "shares": item["threshold"]} for item in fixture["canonical_levels"]]
    assert holding_schema_state(rows)["available"] is True
    assert fixture["contains_credentials"] is False


def test_broker_contract_fixture_matches_executable_observed_row_policy() -> None:
    fixture = json.loads((FIXTURES / "broker_report_contract_v1.json").read_text(encoding="utf-8"))
    positive = fixture["positive_fixture"]
    request = positive["request"]
    assert fixture["contract_version"] == BROKER_ROW_CONTRACT_VERSION
    assert _broker_report_contract(positive["rows"], request["data_id"], request["date"]) == (True, BROKER_ROW_CONTRACT_VERSION)
    assert fixture["response_contract"]["omitted_branch_policy"] == "unknown_not_zero"
    assert fixture["contains_credentials"] is False
