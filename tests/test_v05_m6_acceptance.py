from __future__ import annotations

import re
from pathlib import Path

from app.schemas.v05_graph_publication import (
    GraphPublicationCommandRead,
    GraphPublicationItemRead,
    GraphPublicationRead,
)


ROOT = Path(__file__).resolve().parents[1]
OPERATOR_SUMMARY = ROOT / "docs/34-v0.5-active-graph-publication.md"
DOCS_INDEX = ROOT / "docs/README.md"
ACCEPTANCE_REPORT = (
    ROOT / "docs/testing/acceptance/v0.5-active-graph-publication.md"
)
MASTER_PLAN = (
    ROOT
    / "docs/superpowers/plans/2026-07-15-v0.5-active-graph-publication.md"
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


def test_operator_summary_is_indexed_and_covers_the_operating_contract():
    assert OPERATOR_SUMMARY.is_file()
    assert "./34-v0.5-active-graph-publication.md" in DOCS_INDEX.read_text(
        encoding="utf-8"
    )
    assert {
        "feature gate",
        "roles and endpoints",
        "publication lifecycle",
        "rollout",
        "health and reconciliation",
        "rollback",
        "scope and privacy",
    } <= _headings(OPERATOR_SUMMARY)


def test_acceptance_report_covers_durable_release_gates():
    assert ACCEPTANCE_REPORT.is_file()
    assert {
        "accepted dirty boundary",
        "release gate definitions",
        "gate results",
        "postgresql release acceptance",
        "compatibility regression",
        "scope and privacy audit",
        "rollout and rollback evidence",
        "release decision",
    } <= _headings(ACCEPTANCE_REPORT)

    report = ACCEPTANCE_REPORT.read_text(encoding="utf-8").casefold()
    for marker in (
        "vector_kb_pg_test_dsn",
        "non-skipped",
        "release acceptance incomplete",
        "0023 (head)",
        "git diff --check",
        "ruff",
    ):
        assert marker in report


def test_master_plan_keeps_postgresql_and_boundary_as_hard_release_gates():
    plan = MASTER_PLAN.read_text(encoding="utf-8").casefold()
    assert "hard release gates:" in plan
    assert "postgresql acceptance tests run non-skipped" in plan
    assert "exact accepted dirty boundary" in plan
    assert "cannot be declared releasable" in plan


def test_publication_response_models_exclude_sensitive_payload_fields():
    response_fields = set().union(
        GraphPublicationRead.model_fields,
        GraphPublicationCommandRead.model_fields,
        GraphPublicationItemRead.model_fields,
    )
    forbidden = {
        "source_text",
        "text_quote",
        "quote_text",
        "evidence_text_snapshot",
        "context",
        "context_text",
        "prompt",
        "raw_response",
        "raw_output",
        "provider_request_id",
        "candidate_payload",
        "fact_snapshot",
        "policy_snapshot",
        "properties",
        "error_message",
        "idempotency_key",
    }
    assert response_fields.isdisjoint(forbidden)


def test_release_docs_are_secret_free_and_record_all_scope_audits():
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (OPERATOR_SUMMARY, ACCEPTANCE_REPORT)
    )
    assert re.search(r"\b(?:sk|api)-[A-Za-z0-9_-]{16,}\b", combined) is None
    assert re.search(
        r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://[^\s:/]+:[^\s@]+@",
        combined,
        flags=re.IGNORECASE,
    ) is None

    report = combined.casefold()
    for scope in ("graphrag", "search", "ui", "qdrant", "chat", "dify", "provider"):
        assert scope in report
    assert report.count("rg -n") >= 2
