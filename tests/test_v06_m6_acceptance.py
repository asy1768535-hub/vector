from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from app.main import app
from app.schemas.v06_graph_retrieval import (
    GraphRetrievalAmbiguousCandidate,
    GraphRetrievalCounts,
    GraphRetrievalEntityTypeRead,
    GraphRetrievalErrorResponse,
    GraphRetrievalEvidenceLocator,
    GraphRetrievalNodeRead,
    GraphRetrievalPublicationRead,
    GraphRetrievalQueryRequest,
    GraphRetrievalQueryResponse,
    GraphRetrievalRelationRead,
    GraphRetrievalRelationTypeRead,
    GraphRetrievalSeed,
    GraphRetrievalSeedMatch,
    GraphRetrievalTruncation,
)
from app.services.graph_retrieval_eval import (
    canonical_json_sha256,
    file_sha256,
    load_graph_retrieval_release_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
OPERATOR_GUIDE = ROOT / "docs/35-v0.6-published-graph-retrieval.md"
DOCS_INDEX = ROOT / "docs/README.md"
MASTER_DESIGN = (
    ROOT
    / "docs/superpowers/specs/2026-07-16-v0.6-published-graph-retrieval.md"
)
M6_PLAN = (
    ROOT
    / "docs/superpowers/plans/2026-07-17-v0.6-published-graph-retrieval-m6.md"
)
ACCEPTANCE_REPORT = (
    ROOT / "docs/testing/acceptance/v0.6-published-graph-retrieval.md"
)
POLICY = ROOT / "eval/graph_retrieval/release_policy_v1.json"
RELEASE_EVIDENCE = ROOT / "eval/graph_retrieval/release_evidence_v1.json"

EXPECTED_IMPLEMENTATION_SHA256 = (
    "f1493fd71734cd31b2c69d34be66e4bf9ba4f9398e0c728ae1d2b11f900a762b"
)
EXPECTED_POLICY_CANONICAL_SHA256 = (
    "b6d752bc04abcb29efffbcf7e2920e57661f9493330d5b79e37bf838ad8c3ba0"
)
EXPECTED_POLICY_FILE_SHA256 = (
    "3086d285b9a2c382af47aba9dbbcf7a609bf1007fc4c8f1255fe265eea8c0964"
)
EXPECTED_EVIDENCE_CANONICAL_SHA256 = (
    "7d1894a2512914b15e87f3ec6bd226ef2b96f178f9275461c93de033f257b340"
)
EXPECTED_EVIDENCE_FILE_SHA256 = (
    "cfb4a525f8772f1d8cde295af232bc933ff5c3531eda4ea70b8e0221ef0d35a1"
)
EXPECTED_RESPONSE_SET_SHA256 = (
    "487cf9526ed612dcdcf0985d725f6e65f3643a3c5c92ba222d312598181a226e"
)
FROZEN_IMPLEMENTATION_COMMIT = "2084624882cab5b1eb5572efcd5b0823d8ce3045"


def _headings(path: Path) -> set[str]:
    return {
        match.group(1).strip().casefold()
        for match in re.finditer(
            r"^#{1,6}\s+(.+?)\s*$",
            path.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    }


def _canonical_and_file_sha256(path: Path) -> tuple[str, str]:
    payload = path.read_bytes()
    return canonical_json_sha256(json.loads(payload)), file_sha256(payload)


def test_m6_operator_guide_and_release_docs_are_indexed():
    assert OPERATOR_GUIDE.is_file()
    assert M6_PLAN.is_file()
    assert ACCEPTANCE_REPORT.is_file()
    index = DOCS_INDEX.read_text(encoding="utf-8")
    for relative_path in (
        "./35-v0.6-published-graph-retrieval.md",
        "./superpowers/plans/2026-07-17-v0.6-published-graph-retrieval-m6.md",
        "./testing/acceptance/v0.6-published-graph-retrieval.md",
    ):
        assert relative_path in index

    assert {
        "status and boundary",
        "required configuration",
        "evaluation gates",
        "staged rollout",
        "observability and alerts",
        "client retry contract",
        "incident response",
        "disable and rollback",
    } <= _headings(OPERATOR_GUIDE)


def test_m6_acceptance_report_covers_every_hard_release_gate():
    assert {
        "accepted boundary",
        "frozen m5 evidence",
        "release gate definitions",
        "gate results",
        "postgresql release acceptance",
        "compatibility regression",
        "api, privacy and scope audit",
        "rollout and rollback evidence",
        "production code changes",
        "final workspace boundary",
        "residual risks",
        "release decision",
    } <= _headings(ACCEPTANCE_REPORT)

    report = ACCEPTANCE_REPORT.read_text(encoding="utf-8").casefold()
    for marker in (
        "vector_kb_pg_test_dsn",
        "non-skipped",
        "release acceptance incomplete",
        "0023 (head)",
        "verify-release",
        "git diff --check",
        "ruff",
        "8 passed",
        "0 failed",
    ):
        assert marker in report

    decisions = re.findall(r"decision:\s*\*\*(passed|blocked)\*\*", report)
    assert len(decisions) == 1


def test_m6_release_bundle_and_historical_implementation_are_frozen():
    loaded = load_graph_retrieval_release_evidence(
        repository_root=ROOT,
        evidence_path=RELEASE_EVIDENCE,
    )
    assert _canonical_and_file_sha256(POLICY) == (
        EXPECTED_POLICY_CANONICAL_SHA256,
        EXPECTED_POLICY_FILE_SHA256,
    )
    assert _canonical_and_file_sha256(RELEASE_EVIDENCE) == (
        EXPECTED_EVIDENCE_CANONICAL_SHA256,
        EXPECTED_EVIDENCE_FILE_SHA256,
    )
    assert loaded.policy.policy.implementation_tree_sha256 == (
        EXPECTED_IMPLEMENTATION_SHA256
    )
    assert loaded.calibration.code_commit == FROZEN_IMPLEMENTATION_COMMIT
    assert (
        subprocess.run(
            ("git", "merge-base", "--is-ancestor", FROZEN_IMPLEMENTATION_COMMIT, "HEAD"),
            cwd=ROOT,
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )
    assert loaded.policy.policy.properties_exposed is False
    assert loaded.policy.policy.thresholds == loaded.calibration.candidate_thresholds


def test_m6_release_bundle_has_three_independent_identical_passes():
    loaded = load_graph_retrieval_release_evidence(
        repository_root=ROOT,
        evidence_path=RELEASE_EVIDENCE,
    )
    runs = loaded.post_freeze_runs
    assert len(runs) == 3
    assert len({row.run_id for row in runs}) == 3
    assert len({row.database_id for row in runs}) == 3
    assert all(row.status == "passed" for row in runs)
    assert all(row.case_counts == {"failed": 0, "passed": 48} for row in runs)
    assert all(row.database_cleanup_succeeded for row in runs)
    assert {
        loaded.calibration.canonical_response_set_sha256,
        *(row.canonical_response_set_sha256 for row in runs),
    } == {EXPECTED_RESPONSE_SET_SHA256}


def test_m6_keeps_one_read_only_route_and_zero_property_surface():
    v06_paths = [path for path in app.openapi()["paths"] if "/v06/" in path]
    assert v06_paths == ["/libraries/{slug}/v06/graph/query"]
    path_item = app.openapi()["paths"][v06_paths[0]]
    assert {key for key in path_item if key in {"get", "post", "put", "patch", "delete"}} == {
        "post"
    }

    models = (
        GraphRetrievalAmbiguousCandidate,
        GraphRetrievalCounts,
        GraphRetrievalEntityTypeRead,
        GraphRetrievalErrorResponse,
        GraphRetrievalEvidenceLocator,
        GraphRetrievalNodeRead,
        GraphRetrievalPublicationRead,
        GraphRetrievalQueryRequest,
        GraphRetrievalQueryResponse,
        GraphRetrievalRelationRead,
        GraphRetrievalRelationTypeRead,
        GraphRetrievalSeed,
        GraphRetrievalSeedMatch,
        GraphRetrievalTruncation,
    )
    forbidden = {
        "properties",
        "include_properties",
        "source_text",
        "quote_text",
        "evidence_text_snapshot",
        "context",
        "context_text",
        "prompt",
        "raw_response",
        "raw_output",
        "candidate_payload",
        "error_message",
        "api_key",
        "secret",
    }
    for model in models:
        assert forbidden.isdisjoint(model.model_fields)
        assert model.model_config.get("extra") == "forbid"


def test_m6_release_documents_are_secret_free_and_record_scope_audits():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (OPERATOR_GUIDE, M6_PLAN, ACCEPTANCE_REPORT, MASTER_DESIGN)
    )
    assert re.search(r"\b(?:sk|api)-[A-Za-z0-9_-]{16,}\b", combined) is None
    assert re.search(
        r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s:/]+:[^\s@]+@",
        combined,
        flags=re.IGNORECASE,
    ) is None

    report = ACCEPTANCE_REPORT.read_text(encoding="utf-8").casefold()
    for scope in (
        "graphrag",
        "properties",
        "migration `0024`",
        "qdrant",
        "chat",
        "dify",
        "provider",
        "production/eval drift",
    ):
        assert scope in report
    assert report.count("rg -n") >= 2
