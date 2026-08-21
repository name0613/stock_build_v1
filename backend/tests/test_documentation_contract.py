from pathlib import Path

from app.scoring import BROKER_ROW_CONTRACT_VERSION, SCORE_VERSION


ROOT_CANDIDATES = (
    Path(__file__).resolve().parents[2],
    Path(__file__).resolve().parents[1],
    Path("/app"),
)


def test_public_documentation_matches_current_score_and_broker_contract() -> None:
    root = next(candidate for candidate in ROOT_CANDIDATES if (candidate / "ARCHITECTURE.md").exists())
    architecture = (root / "ARCHITECTURE.md").read_text(encoding="utf-8")
    scoring = (root / "SCORING.md").read_text(encoding="utf-8")
    combined = f"{architecture}\n{scoring}"
    assert SCORE_VERSION in combined
    assert BROKER_ROW_CONTRACT_VERSION in combined
    assert "unknown" in combined.lower()
    assert "never imputed as zero" in combined.lower() or "never zero-filled" in combined.lower()
    assert "s-only-v5" not in combined
    assert "only then may an omitted branch mean zero" not in combined.lower()
