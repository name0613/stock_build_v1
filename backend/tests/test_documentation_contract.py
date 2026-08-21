from pathlib import Path

from app.scoring import BROKER_ROW_CONTRACT_VERSION, SCORE_VERSION


ROOT = Path(__file__).resolve().parents[2]


def test_public_documentation_matches_current_score_and_broker_contract() -> None:
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    scoring = (ROOT / "SCORING.md").read_text(encoding="utf-8")
    combined = f"{architecture}\n{scoring}"
    assert SCORE_VERSION in combined
    assert BROKER_ROW_CONTRACT_VERSION in combined
    assert "unknown" in combined.lower()
    assert "never imputed as zero" in combined.lower() or "never zero-filled" in combined.lower()
    assert "s-only-v5" not in combined
    assert "only then may an omitted branch mean zero" not in combined.lower()
