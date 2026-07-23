from __future__ import annotations

import re
import subprocess
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import Settings


ROOT = Path(__file__).resolve().parents[1]
DOCS_INDEX = ROOT / "docs/README.md"
ACCEPTANCE_REPORT = ROOT / "docs/testing/acceptance/v0.9-first-slice.md"

EXPECTED_CHAIN = (
    ("86addef535650a267a19a5814bd75e88bdebff5a", "v0.8 baseline"),
    ("6aa5d39201a9ebd350e4cabae7aab7ed8f30e96e", "M1"),
    ("05659fbe3217221874ae91631be4edf72490b3f8", "M2"),
    ("ce42c6584e6a43b83d97be926b71433d8301320c", "M3"),
    ("0c9aad20040950e3fb648df153fe97bb7f4ebb02", "M4"),
    ("137616e3b819ed35177c8b17d5dc0cb4457d1675", "M5"),
    ("db83b339536526b476bf5e3f294e084afcb6709d", "M6"),
    ("94cbd29234bc3fecb70f113142fdc843059ca860", "M7"),
)
PRODUCTION_CANDIDATE = EXPECTED_CHAIN[-1][0]
V09_FLAGS = (
    "graph_catalog_enabled",
    "graph_governance_enabled",
    "schema_lifecycle_enabled",
    "public_api_v1_enabled",
    "public_api_operations_enabled",
    "public_api_limits_enabled",
    "public_api_limit_private_organizations",
)
EXPECTED_MIGRATIONS = (
    "0038_v09_graph_governance_actions.py",
    "0039_v09_schema_lifecycle_actions.py",
    "0040_v09_public_api_operations.py",
)
REQUIRED_GATES = {
    "Candidate boundary",
    "Focused integration",
    "PostgreSQL",
    "Full regression",
    "Migration and static safety",
    "Browser workflows",
    "Privacy and rollback",
    "Cleanup",
}
PRODUCTION_PATHS = ("app", "alembic", "admin-ui", "eval", "scripts", ".env.example")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _headings(path: Path) -> set[str]:
    return {
        match.group(1).strip().casefold()
        for match in re.finditer(
            r"^#{1,6}\s+(.+?)\s*$",
            path.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    }


def test_m8_candidate_is_the_exact_linear_m1_m7_chain() -> None:
    report = ACCEPTANCE_REPORT.read_text(encoding="utf-8")
    for (parent, _), (child, _) in zip(EXPECTED_CHAIN, EXPECTED_CHAIN[1:]):
        result = _git("merge-base", "--is-ancestor", parent, child)
        assert result.returncode == 0, result.stderr

    assert _git("merge-base", "--is-ancestor", PRODUCTION_CANDIDATE, "HEAD").returncode == 0
    for commit, label in EXPECTED_CHAIN:
        assert commit in report
        assert label in report


def test_m8_adds_no_unreported_product_or_evaluation_change() -> None:
    result = _git("diff", "--name-only", PRODUCTION_CANDIDATE, "--", *PRODUCTION_PATHS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_m8_keeps_v09_flags_default_off_in_runtime_and_example() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for field_name in V09_FLAGS:
        assert Settings.model_fields[field_name].default is False
        environment_name = field_name.upper()
        assert re.search(
            rf"^{re.escape(environment_name)}=false$",
            example,
            flags=re.MULTILINE,
        )


def test_m8_keeps_one_0040_head_and_exact_v09_migrations() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0040"]
    assert scripts.get_revision("0040").down_revision == "0039"
    assert scripts.get_revision("0039").down_revision == "0038"
    assert scripts.get_revision("0038").down_revision == "0037"
    for filename in EXPECTED_MIGRATIONS:
        assert (ROOT / "alembic/versions" / filename).is_file()


def test_m8_report_is_indexed_and_covers_every_gate() -> None:
    assert ACCEPTANCE_REPORT.is_file()
    assert "./testing/acceptance/v0.9-first-slice.md" in DOCS_INDEX.read_text(
        encoding="utf-8"
    )
    assert {
        "accepted boundary",
        "release gate definitions",
        "gate results",
        "focused integration",
        "postgresql integration acceptance",
        "full regression and compatibility",
        "migration and static safety",
        "browser workflow acceptance",
        "privacy and rollback evidence",
        "production code changes",
        "cleanup and final workspace",
        "residual risks",
        "release decision",
    } <= _headings(ACCEPTANCE_REPORT)


def test_m8_decision_matches_gate_results() -> None:
    report = ACCEPTANCE_REPORT.read_text(encoding="utf-8")
    results = {
        match.group(1).strip(): match.group(2)
        for match in re.finditer(
            r"^\|\s*([^|]+?)\s*\|\s*(PASS|FAIL|NOT_RUN)\s*\|",
            report,
            flags=re.MULTILINE,
        )
    }
    assert REQUIRED_GATES <= results.keys()

    decisions = re.findall(r"^Decision:\s*\*\*(GO_READY|NO_GO)\*\*\s*$", report, re.MULTILINE)
    assert len(decisions) == 1
    all_passed = all(results[gate] == "PASS" for gate in REQUIRED_GATES)
    assert (decisions[0] == "GO_READY") is all_passed


def test_m8_report_is_sanitized_and_keeps_rollout_separate() -> None:
    report = ACCEPTANCE_REPORT.read_text(encoding="utf-8")
    normalized_report = re.sub(r"\s+", " ", report)
    assert re.search(r"\b(?:sk|api)-[A-Za-z0-9_-]{16,}\b", report) is None
    assert re.search(
        r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s:/]+:[^\s@]+@",
        report,
        flags=re.IGNORECASE,
    ) is None
    for marker in (
        "zero mandatory PostgreSQL skips",
        "zero M8-created residual databases",
        "feature flags remain default-off",
        "no separate mobile page",
        "no push, PR, merge, deployment, or rollout enablement",
        "tests/test_v07_entity_linking_eval.py",
    ):
        assert marker in normalized_report
