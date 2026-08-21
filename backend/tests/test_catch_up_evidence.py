from __future__ import annotations

from scripts.catch_up_once_evidence import source_revision


def test_catch_up_evidence_binds_to_runtime_source_revision(monkeypatch) -> None:
    revision = "a" * 40
    monkeypatch.setenv("SOURCE_REVISION", revision)
    assert source_revision() == revision
