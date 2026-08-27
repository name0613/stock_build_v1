from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import subprocess
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


def docker_copy_inputs(dockerfile: Path) -> list[str]:
    """Return source paths referenced by COPY instructions in a Dockerfile."""
    inputs: list[str] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*COPY\s+(?:--[^ ]+\s+)*(.+?)\s+[^ ]+\s*$", line)
        if not match:
            continue
        sources = match.group(1).split()
        inputs.extend(source for source in sources if not source.startswith("--"))
    return inputs


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"evidence JSON is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"evidence JSON root must be an object: {path}")
    return value


def _assert_same(errors: list[str], label: str, values: dict[str, object]) -> None:
    present = {name: value for name, value in values.items() if value is not None}
    if not present:
        errors.append(f"missing cross-reference: {label}")
        return
    if len(set(map(str, present.values()))) != 1:
        errors.append(f"cross-reference mismatch: {label} ({present})")


def validate_acceptance_evidence(root: Path) -> dict[str, object]:
    """Reject internally stale final evidence before creating an immutable ZIP."""
    current = root / "current_acceptance"
    manifest_path = current / "ACCEPTANCE_RUN_MANIFEST.json"
    manifest = _load_json(manifest_path)
    errors: list[str] = []

    required_names = [
        "RUN_METADATA.json",
        "LATEST_REMEDIATION_RUN.json",
        "REMEDIATION_MATRIX.json",
        "frontend-build-results.json",
        "frontend-e2e-junit.xml",
        "frontend-e2e-results.json",
        "BROWSER_ACCEPTANCE_EVIDENCE.json",
        "NAS_RUNTIME_METADATA_EVIDENCE.json",
    ]
    payloads = {name: _load_json(current / name) for name in required_names if name.endswith(".json")}
    missing = [name for name in required_names if not (current / name).is_file()]
    if missing:
        errors.append(f"missing required final evidence: {', '.join(missing)}")

    run_id = manifest.get("acceptance_run_id")
    source_revision = manifest.get("source_revision")
    deployed_revision = manifest.get("deployed_source_revision")
    current_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if source_revision != current_head:
        errors.append(f"acceptance evidence source revision {source_revision!r} does not match current HEAD {current_head!r}")
    _assert_same(errors, "acceptance_run_id", {name: payload.get("acceptance_run_id") for name, payload in payloads.items() if name != "NAS_RUNTIME_METADATA_EVIDENCE.json"})
    _assert_same(errors, "source_revision", {name: payload.get("source_revision") for name, payload in payloads.items() if name != "NAS_RUNTIME_METADATA_EVIDENCE.json"})
    _assert_same(errors, "deployed_source_revision", {name: payload.get("deployed_source_revision") for name, payload in payloads.items() if name in {"RUN_METADATA.json", "LATEST_REMEDIATION_RUN.json", "REMEDIATION_MATRIX.json"}} | {"acceptance_manifest": deployed_revision})
    if manifest.get("source_revision_match") is not True or source_revision != deployed_revision:
        errors.append("acceptance manifest does not prove source/deployed revision equality")

    artifact_entries = manifest.get("artifacts")
    if not isinstance(artifact_entries, list) or manifest.get("artifact_count") != len(artifact_entries):
        errors.append("acceptance artifact_count does not match artifacts")
    artifact_map: dict[str, dict] = {}
    for item in artifact_entries if isinstance(artifact_entries, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            errors.append("acceptance manifest contains malformed artifact entry")
            continue
        path = current / item["path"]
        artifact_map[item["path"]] = item
        if not path.is_file():
            errors.append(f"acceptance artifact is missing: {item['path']}")
            continue
        actual_size = path.stat().st_size
        actual_hash = sha256(path)
        if item.get("size") != actual_size or item.get("sha256") != actual_hash:
            errors.append(f"acceptance artifact hash/size mismatch: {item['path']}")

    junit_path = current / "frontend-e2e-junit.xml"
    if junit_path.is_file():
        junit_hash = sha256(junit_path)
        build_hash = payloads.get("frontend-build-results.json", {}).get("e2e_junit_sha256")
        manifest_hash = artifact_map.get("frontend-e2e-junit.xml", {}).get("sha256")
        _assert_same(errors, "frontend E2E JUnit SHA-256", {"frontend-build-results.json": build_hash, "current_acceptance/frontend-e2e-junit.xml": junit_hash, "ACCEPTANCE_RUN_MANIFEST.json": manifest_hash})
        duplicate_junit = root / "test_results/frontend-e2e-junit.xml"
        if duplicate_junit.is_file():
            _assert_same(errors, "duplicated frontend E2E JUnit SHA-256", {"current_acceptance": junit_hash, "test_results": sha256(duplicate_junit)})

    images = manifest.get("images") if isinstance(manifest.get("images"), dict) else {}
    runtime = payloads.get("RUN_METADATA.json", {})
    runtime_images = runtime.get("images") if isinstance(runtime.get("images"), dict) else {}
    latest_images = payloads.get("LATEST_REMEDIATION_RUN.json", {}).get("deployed_image_digests")
    latest_images = latest_images if isinstance(latest_images, dict) else {}
    browser = payloads.get("BROWSER_ACCEPTANCE_EVIDENCE.json", {})
    for image_name in ("api", "worker", "frontend"):
        _assert_same(errors, f"{image_name} image digest", {"acceptance": images.get(image_name), "run_metadata": runtime_images.get(image_name), "latest_remediation": latest_images.get(image_name)})
    _assert_same(errors, "frontend image digest in browser evidence", {"acceptance": images.get("frontend"), "browser": browser.get("frontend_image_digest")})

    locks = manifest.get("dependency_locks") if isinstance(manifest.get("dependency_locks"), dict) else {}
    runtime_locks = runtime.get("dependency_locks") if isinstance(runtime.get("dependency_locks"), dict) else {}
    _assert_same(errors, "backend dependency lock SHA-256", {"acceptance": locks.get("backend_sha256"), "run_metadata": runtime_locks.get("backend_sha256"), "source": sha256(root / "backend/requirements.lock") if (root / "backend/requirements.lock").is_file() else None})
    _assert_same(errors, "frontend dependency lock SHA-256", {"acceptance": locks.get("frontend_sha256"), "run_metadata": runtime_locks.get("frontend_sha256"), "frontend-build-results": payloads.get("frontend-build-results.json", {}).get("frontend_lock_sha256"), "source": sha256(root / "frontend/package-lock.json") if (root / "frontend/package-lock.json").is_file() else None})

    score_contract = runtime.get("score_contract") if isinstance(runtime.get("score_contract"), dict) else {}
    _assert_same(errors, "score formula hash", {"acceptance": manifest.get("formula_hash"), "run_metadata": score_contract.get("formula_hash"), "browser": browser.get("formula_hash")})
    _assert_same(errors, "calendar hash", {"acceptance": manifest.get("calendar_hash"), "run_metadata": score_contract.get("calendar_hash")})
    e2e_results = payloads.get("frontend-e2e-results.json", {})
    e2e_metadata = ((e2e_results.get("config") or {}).get("metadata") or {}) if isinstance(e2e_results.get("config"), dict) else {}
    _assert_same(errors, "frontend E2E acceptance_run_id", {"manifest": run_id, "results": e2e_metadata.get("acceptance_run_id")})
    _assert_same(errors, "frontend E2E source revision", {"manifest": source_revision, "results": e2e_metadata.get("source_revision")})
    _assert_same(errors, "frontend E2E formula hash", {"manifest": manifest.get("formula_hash"), "results": e2e_metadata.get("formula_hash")})

    if errors:
        raise RuntimeError("evidence integrity validation failed:\n- " + "\n- ".join(errors))
    return {
        "acceptance_run_id": run_id,
        "source_revision": source_revision,
        "frontend_e2e_junit_sha256": sha256(junit_path),
        "artifact_count": len(artifact_entries),
        "cross_references_valid": True,
    }


def create_bundle() -> tuple[Path, str]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bundle_dir = ROOT / f"review_bundle_{timestamp}"
    bundle_dir.mkdir()
    source_files = [
        "backend/app", "backend/tests", "backend/Dockerfile", "backend/requirements.txt",
        "backend/requirements.lock",
        "frontend/src", "frontend/e2e", "frontend/Dockerfile", "frontend/package.json",
        "frontend/package-lock.json", "frontend/playwright.config.ts", "frontend/vite.config.ts",
        "frontend/tsconfig.json", "frontend/index.html", "fixtures", "migrations", "nginx", "scripts",
        "README.md", "ARCHITECTURE.md", "DATA_SOURCES.md", "SCORING.md", "DEPLOYMENT.md",
        "OPERATIONS.md", "SECURITY.md", "REVIEW_INSTRUCTIONS.md", "docker-compose.yml", ".env.example",
    ]
    required_build_inputs = [
        "backend/Dockerfile", "backend/requirements.lock", "frontend/Dockerfile",
        "frontend/package-lock.json", "docker-compose.yml", "fixtures",
    ]
    missing = [relative for relative in required_build_inputs if not (ROOT / relative).exists()]
    if missing:
        raise RuntimeError(f"required reviewer build inputs are missing: {', '.join(missing)}")
    docker_inputs = docker_copy_inputs(ROOT / "backend/Dockerfile")
    missing_docker_inputs = [relative for relative in docker_inputs if not (ROOT / relative).exists()]
    if missing_docker_inputs:
        raise RuntimeError(f"backend Dockerfile COPY inputs are missing: {', '.join(missing_docker_inputs)}")
    fixture_test = ROOT / "backend/tests/test_contract_fixtures.py"
    if not (ROOT / "fixtures").is_dir() or not fixture_test.exists():
        raise RuntimeError("reviewer bundle must include fixtures/ and its contract-fixture test")
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
    validate_acceptance_evidence(ROOT)
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
