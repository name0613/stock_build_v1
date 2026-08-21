from __future__ import annotations

import hashlib
import json
import shutil
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY_IGNORE = shutil.ignore_patterns(
    ".env", "*.db", "*.pyc", "__pycache__", ".pytest_cache", ".ruff_cache",
    "node_modules", ".venv", "dist", "raw", "postgres",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_tree(relative: str, target: Path) -> None:
    source = ROOT / relative
    if not source.exists():
        return
    destination = target / relative
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=COPY_IGNORE)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def create_bundle() -> tuple[Path, str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bundle_dir = ROOT / f"review_bundle_{timestamp}"
    bundle_dir.mkdir()
    source_files = [
        "backend/app", "backend/tests", "backend/Dockerfile", "backend/requirements.txt",
        "backend/requirements.lock",
        "frontend/src", "frontend/e2e", "frontend/Dockerfile", "frontend/package.json",
        "frontend/package-lock.json", "frontend/playwright.config.ts", "frontend/vite.config.ts",
        "frontend/tsconfig.json", "frontend/index.html", "migrations", "nginx", "scripts",
        "README.md", "ARCHITECTURE.md", "DATA_SOURCES.md", "SCORING.md", "DEPLOYMENT.md",
        "OPERATIONS.md", "SECURITY.md", "REVIEW_INSTRUCTIONS.md", "docker-compose.yml", ".env.example",
    ]
    required_build_inputs = [
        "backend/Dockerfile", "backend/requirements.lock", "frontend/Dockerfile",
        "frontend/package-lock.json", "docker-compose.yml",
    ]
    missing = [relative for relative in required_build_inputs if not (ROOT / relative).exists()]
    if missing:
        raise RuntimeError(f"required reviewer build inputs are missing: {', '.join(missing)}")
    for relative in ("ARCHITECTURE.md", "SCORING.md", "OPERATIONS.md"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        if "s-only-v2" in content:
            raise RuntimeError(f"stale score-version reference remains in {relative}")
    for relative in source_files:
        copy_tree(relative, bundle_dir / "source")
    for folder in ["test_results", "screenshots", "sanitized_sample_data"]:
        source = ROOT / folder
        destination = bundle_dir / folder
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True, ignore=COPY_IGNORE)
        else:
            destination.mkdir(exist_ok=True)
    # Keep the submitted run unambiguous. Older runtime attempts are retained
    # only as explicitly labelled history and can never be mistaken for the
    # current deployment evidence.
    historical = ROOT / "deployment_evidence"
    if historical.is_dir():
        shutil.copytree(historical, bundle_dir / "historical_evidence" / "deployment_evidence", dirs_exist_ok=True, ignore=COPY_IGNORE)
    current = ROOT / "current_acceptance"
    if not current.is_dir():
        raise RuntimeError("current_acceptance is missing; create a complete current run before bundling")
    shutil.copytree(current, bundle_dir / "current_acceptance", dirs_exist_ok=True, ignore=COPY_IGNORE)
    manifest = []
    for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(bundle_dir).as_posix()
        if rel == "BUNDLE_MANIFEST.json":
            continue
        manifest.append({"path": rel, "size": path.stat().st_size, "sha256": sha256(path)})
    (bundle_dir / "BUNDLE_MANIFEST.json").write_text(json.dumps({"format": "1", "created_at": datetime.now(timezone.utc).isoformat(), "immutable": True, "files": manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = ROOT / f"{bundle_dir.name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
            zf.write(path, path.relative_to(bundle_dir.parent).as_posix())
    for path in bundle_dir.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IREAD)
    return archive, sha256(archive)


if __name__ == "__main__":
    archive, digest = create_bundle()
    print(json.dumps({"bundle": str(archive), "sha256": digest}, ensure_ascii=False))
