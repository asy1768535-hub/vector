from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "release/v0.9/final-candidate.json"
REPORT_PATH = ROOT / "docs/testing/acceptance/v0.9-final.md"
PRODUCT_CANDIDATE = "3f513a9f750354bcdc7b5e112eeb5f1dae24c913"
PRODUCT_TREE = "e8230129d21e29c58c236b4b2be0fd5ba3c83e1d"
FRONTEND_TREE = "582e2c74298c1a6abf55667f92c0d6205747df8b"
REQUIRED_GATES = {
    "Candidate identity",
    "Automated compatibility",
    "PostgreSQL and migration",
    "HTTP MCP and external sync",
    "Hosted private isolation",
    "Backup restore and Qdrant",
    "Web operator workflows",
    "Privacy security and bounds",
    "Rollback and default off",
    "Cleanup",
}
V09_DEFAULT_OFF_FLAGS = (
    "graph_catalog_enabled",
    "graph_governance_enabled",
    "schema_lifecycle_enabled",
    "public_api_v1_enabled",
    "public_api_operations_enabled",
    "public_api_limits_enabled",
    "external_graph_sync_enabled",
)


def _git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    # The frozen manifest was recorded from the accepted Windows worktree.
    content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return hashlib.sha256(content).hexdigest()


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_final_candidate_identity_and_ordered_dependency_closure() -> None:
    manifest = _manifest()
    assert manifest["product_candidate_commit"] == PRODUCT_CANDIDATE
    assert manifest["product_candidate_tree"] == PRODUCT_TREE
    assert _git("rev-parse", f"{PRODUCT_CANDIDATE}^{{tree}}") == PRODUCT_TREE
    chain = [manifest["baseline_commit"]] + [
        item["commit"] for item in manifest["ordered_dependencies"]
    ]
    for parent, child in zip(chain, chain[1:]):
        result = subprocess.run(
            ("git", "merge-base", "--is-ancestor", parent, child),
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0
    assert _git("merge-base", PRODUCT_CANDIDATE, "HEAD") == PRODUCT_CANDIDATE


def test_manifest_freezes_dependencies_and_unchanged_accepted_frontend() -> None:
    manifest = _manifest()
    assert _sha256(ROOT / "requirements.txt") == manifest["requirements_sha256"]
    assert _sha256(ROOT / "pyproject.toml") == manifest["pyproject_sha256"]
    assert _git("rev-parse", f"{PRODUCT_CANDIDATE}:admin-ui") == FRONTEND_TREE
    assert _git("rev-parse", "e6b98ce:admin-ui") == FRONTEND_TREE


def test_final_candidate_has_one_reversible_0042_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["0042"]
    assert scripts.get_revision("0042").down_revision == "0041"
    assert scripts.get_revision("0041").down_revision == "0040"
    assert _manifest()["alembic_head"] == "0042"


def test_all_runtime_and_organization_rollouts_remain_default_off() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for field in V09_DEFAULT_OFF_FLAGS:
        assert Settings.model_fields[field].default is False
        assert re.search(rf"^{field.upper()}=false$", example, re.MULTILINE)
    migration = (
        ROOT / "alembic/versions/0042_v09_supported_deployment.py"
    ).read_text(encoding="utf-8")
    assert 'server_default=sa.text("false")' in migration
    assert "INSERT INTO organization_capability_rollouts" not in migration
    assert _manifest()["acceptance"]["rollout_enabled"] is False


def test_final_report_is_complete_sanitized_and_unambiguous() -> None:
    report = REPORT_PATH.read_text(encoding="utf-8")
    results = {
        match.group(1).strip(): match.group(2)
        for match in re.finditer(
            r"^\|\s*([^|]+?)\s*\|\s*(PASS|FAIL|NOT_RUN)\s*\|",
            report,
            flags=re.MULTILINE,
        )
    }
    assert REQUIRED_GATES <= results.keys()
    verdicts = re.findall(
        r"^Verdict:\s*\*\*(GO_ELIGIBLE|BLOCKED)\*\*\s*$",
        report,
        flags=re.MULTILINE,
    )
    assert len(verdicts) == 1
    all_passed = all(results[gate] == "PASS" for gate in REQUIRED_GATES)
    assert (verdicts[0] == "GO_ELIGIBLE") is all_passed
    assert _manifest()["acceptance"]["verdict"] == verdicts[0]
    assert re.search(r"\b(?:sk|api)-[A-Za-z0-9_-]{16,}\b", report) is None
    assert re.search(
        r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s:/]+:[^\s@]+@",
        report,
        flags=re.IGNORECASE,
    ) is None
    for required in (
        "zero mandatory PostgreSQL skips",
        "zero residual test databases",
        "zero residual Qdrant collections",
        "no push, PR, merge, deployment, or rollout enablement",
    ):
        assert required in re.sub(r"\s+", " ", report)
