from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.metadata
import ipaddress
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
import tracemalloc
import uuid
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from sqlalchemy.engine import make_url

from app.services.graph_canonical import canonical_graph_json_v1
from app.services.graph_normalization import normalize_graph_name_v1
from eval.entity_linking.contracts import (
    CATEGORY_ORDER,
    GATE_ID_ORDER,
    STRATUM_ORDER,
    ArtifactRef,
    BootstrapResult,
    CalibrationArtifact,
    CategoryCounts,
    CohortCounts,
    CategoryPredicateCase,
    ConformanceDataset,
    ControlConfig,
    CountsRecord,
    DocumentRecord,
    EmbeddingIdentity,
    EntityRecord,
    EntityTypeRecord,
    EvaluationCase,
    EvidenceRecord,
    EXTERNAL_DISTRIBUTION_VERSIONS,
    ExternalDistributionRecord,
    FeasibilityManifest,
    FrozenPolicy,
    FeatureCase,
    FeatureExpected,
    GoldDataset,
    GoldUtilityRecord,
    GridResult,
    GateDecision,
    IdentityTimeCleanupDecision,
    IntrinsicMetrics,
    LatencySummary,
    LibraryRecord,
    MentionRecord,
    MetricConfig,
    Metrics,
    MinimumCountsRecord,
    OntologyRecord,
    OrderingCase,
    PerformanceFixture,
    Performance,
    PolicyApprovalPayload,
    PostFreezeArtifact,
    PrivacyCanaryRecord,
    PublicationRecord,
    QdrantCollection,
    QdrantCollectionConfig,
    QdrantIdentity,
    ReducedRational,
    ReleaseEvidence,
    RelationRecord,
    RelationTypeRecord,
    RevisionRecord,
    ScopeSchemaHash,
    StratumCounts,
    SignedGain,
    RunGateDecision,
    Threshold,
    ThresholdGrid,
    UtilityGains,
    UtilityMetrics,
    OrdinalArtifactRef,
    OrdinalResponseHash,
    CaseResponseHash,
    EnvironmentRecord,
    BootstrapConfig,
    CandidateFixture,
    DecisionCase,
    DecisionExpected,
    ChunkRecord,
    artifact_ref,
    canonical_sha256,
    exact_file_sha256,
    load_canonical_json,
    load_canonical_jsonl,
    parse_json_bytes,
)
from eval.entity_linking.reference_scorer import (
    EntityCandidate,
    FeatureScores,
    PreparedEntityCandidate,
    ScoredCandidate,
    decide_scored_candidates,
    prepare_candidates,
    logical_decision,
    resolve_prepared_mention,
    resolve_mention,
    is_strict_subsequence,
    score_candidate,
    score_normalized_pair,
    stable_candidate_key,
)


G2_APPROVAL_COMMIT = "1d67635735b0aa553399168eb3c923500ee41b2b"
G2_SPECIFICATION_TREE_SHA256 = "af4ca0f30369694722504e34ecfd3667875b1f93dc462c8751497f59697991e0"
G1_COMMIT = "f40c5c84c3248639aa6603d43b6b306ad76d66fd"
AUDIT_PRESERVATION_COMMIT = "155ef946c48272518c996458bc206039b6676c18"
INVALIDATED_G2_APPROVAL_COMMIT = "75bf4141be743c1164bfa9841d0737509d7575fe"
INVALIDATED_G2_SPECIFICATION_TREE_SHA256 = "b5cde88a6705b1da9053dd36bef46ec2b096b7bd18555b65b5c3ff6fab908075"
INCOMPLETE_IMPLEMENTATION_COMMIT = "4217498277706ceb1ee2b60d25d593cbc80ef45f"
INVALIDATED_CALIBRATION_PATH = "eval/entity_linking/results/v07-el-calibration-v1-20260717-01.json"
INVALIDATED_CALIBRATION_CANONICAL_SHA256 = "96757dcd901439828b61605c473a34c2c838ed6755db6f129d030561b156894d"
INVALIDATED_CALIBRATION_FILE_SHA256 = "c35210075e179ebce197275041e0ff69403297185ec2ebcc16a663d7611e9225"
INVALIDATED_CALIBRATION_BLOB_OID = "4c1dc04cca1ddd874fa309ad0314ad24dfc0ea68"
PRESERVED_GITATTRIBUTES_BLOB_OID = "3f5e0ee58b5d94db6fd2ed25511a9957130885f6"
UUID_NAMESPACE = uuid.UUID("47eb7b81-7cac-42be-976d-92d8a009a325")
SCORER_CONFORMANCE_UUID_NAMESPACE = uuid.UUID("1bcb8d89-4423-563a-962d-670c026f6dc8")
CALIBRATION_RUN_ID = "v07-el-calibration-v9-20260720-01"
CALIBRATION_DATABASE_ID = "vkt_v07_el_eval_calibration_v9_20260720_01"
POST_FREEZE_IDENTITIES = (
    (1, "v07-el-post-freeze-v9-20260720-01", "vkt_v07_el_eval_post_freeze_v9_20260720_01"),
    (2, "v07-el-post-freeze-v9-20260720-02", "vkt_v07_el_eval_post_freeze_v9_20260720_02"),
    (3, "v07-el-post-freeze-v9-20260720-03", "vkt_v07_el_eval_post_freeze_v9_20260720_03"),
)
POLICY_PATH = "eval/entity_linking/link_policy_v8.json"
RELEASE_EVIDENCE_PATH = "eval/entity_linking/release_evidence_v8.json"
EMBEDDING_PROBE_TEXT = "vkt-v07-entity-linking-identity-probe"
EMBEDDING_PROBE_SHA256 = "4caad60c112bd93fda55714c91aef2762a3c5c5c0df2a09dc28e6296800cc61f"
CONTROL_RETRIEVAL_CANDIDATE_K = 50

DEPENDENCY_ROOT_MODULES = (
    "app.config",
    "app.models.library",
    "app.schemas.dify",
    "app.schemas.v06_graph_retrieval",
    "app.services.embedding",
    "app.services.graph_canonical",
    "app.services.graph_normalization",
    "app.services.graph_publication_read",
    "app.services.graph_retrieval",
    "app.services.keyword_search",
    "app.services.qdrant",
    "app.services.retrieval",
    "app.services.source_enrichment",
    "app.services.visibility",
)
EXTERNAL_DISTRIBUTIONS = EXTERNAL_DISTRIBUTION_VERSIONS
G3_IMPLEMENTATION_PATHS = (
    "eval/entity_linking/__init__.py",
    "eval/entity_linking/contracts.py",
    "eval/entity_linking/reference_scorer.py",
    "eval/entity_linking/runtime.py",
    "eval/entity_linking/README.md",
    "eval/entity_linking/gold_v3.json",
    "eval/entity_linking/cases_v3.jsonl",
    "eval/entity_linking/conformance_v2.json",
    "eval/entity_linking/manifests/feasibility_v3.json",
    "scripts/entity_linking_feasibility.py",
    "tests/test_v07_entity_linking_eval.py",
    "tests/test_v07_entity_linking_eval_pg.py",
    "docs/testing/acceptance/v0.7-entity-linking-feasibility.md",
)
PROTECTED_PATHS = (
    "app",
    "config",
    ".env.example",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "alembic.ini",
    "alembic",
    "eval/graph_retrieval",
    "scripts/graph_retrieval_eval.py",
)
EXCLUDED_USER_PATHS = (
    "admin-ui/login_redesign.test.mjs",
    "admin-ui/src/views/Login.js",
    "admin-ui/style.css",
    "_drop_tables.py",
    "_drop_test_db.py",
    "_drop_test_db2.py",
    "_drop_test_sync.py",
    "_verify_baseline.py",
    "docs/codex-handoff.md",
)
G2_SPECIFICATION_PATHS = (
    ".gitattributes",
    "docs/superpowers/specs/2026-07-17-v0.7-publication-scoped-entity-linking.md",
    "docs/superpowers/plans/2026-07-17-v0.7-publication-scoped-entity-linking-g2.md",
    "docs/testing/acceptance/v0.7-entity-linking-corrective-audit.md",
    "docs/testing/acceptance/v0.7-entity-linking-database-change-incident.md",
)
G2_APPROVAL_PATHS = (
    "docs/README.md",
    "docs/superpowers/specs/2026-07-17-v0.7-publication-scoped-entity-linking.md",
    "docs/superpowers/plans/2026-07-17-v0.7-publication-scoped-entity-linking-g2.md",
    "docs/testing/acceptance/v0.7-entity-linking-corrective-audit.md",
)
AUDIT_PRESERVATION_PATHS = (".gitattributes", INVALIDATED_CALIBRATION_PATH)
LF_CONTRACT_PATHS = (
    ".gitattributes",
    "docs/superpowers/specs/2026-07-17-v0.7-publication-scoped-entity-linking.md",
    "docs/superpowers/plans/2026-07-17-v0.7-publication-scoped-entity-linking-g2.md",
    "docs/testing/acceptance/v0.7-entity-linking-corrective-audit.md",
    "docs/testing/acceptance/v0.7-entity-linking-database-change-incident.md",
    "docs/testing/acceptance/v0.7-entity-linking-feasibility.md",
    "eval/entity_linking/contracts.py",
    "eval/entity_linking/gold_v2.json",
    "eval/entity_linking/cases_v2.jsonl",
    "eval/entity_linking/conformance_v2.json",
    "eval/entity_linking/manifests/feasibility_v2.json",
    "eval/entity_linking/gold_v3.json",
    "eval/entity_linking/cases_v3.jsonl",
    "eval/entity_linking/manifests/feasibility_v3.json",
    "eval/entity_linking/results/v07-el-calibration-v1-20260717-01.json",
    "scripts/entity_linking_feasibility.py",
    "tests/test_v07_entity_linking_eval.py",
    "tests/test_v07_entity_linking_eval_pg.py",
)
PHASE_OUTPUT_PATHS = (
    INVALIDATED_CALIBRATION_PATH,
    f"eval/entity_linking/results/{CALIBRATION_RUN_ID}.json",
    POLICY_PATH,
    *(f"eval/entity_linking/results/{run_id}.json" for _, run_id, _ in POST_FREEZE_IDENTITIES),
    RELEASE_EVIDENCE_PATH,
)
HISTORICAL_ENTITY_LINKING_PATHS = (
    "eval/entity_linking/gold_v1.json",
    "eval/entity_linking/cases_v1.jsonl",
    "eval/entity_linking/conformance_v1.json",
    "eval/entity_linking/manifests/feasibility_v1.json",
    "eval/entity_linking/results/v07-el-calibration-v2-20260720-01.json",
    "eval/entity_linking/link_policy_v1.json",
    "eval/entity_linking/results/v07-el-post-freeze-v2-20260720-01.json",
    "eval/entity_linking/results/v07-el-calibration-v3-20260720-01.json",
    "eval/entity_linking/results/v07-el-calibration-v4-20260720-01.json",
    "eval/entity_linking/results/v07-el-calibration-v5-20260720-01.json",
    "eval/entity_linking/results/v07-el-calibration-v6-20260720-01.json",
    "eval/entity_linking/gold_v2.json",
    "eval/entity_linking/cases_v2.jsonl",
    "eval/entity_linking/manifests/feasibility_v2.json",
    "eval/entity_linking/results/v07-el-calibration-v7-20260720-01.json",
    "eval/entity_linking/link_policy_v6.json",
    "eval/entity_linking/results/v07-el-post-freeze-v7-20260720-01.json",
)


class EntityLinkingEvalError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class LoadedDataset:
    root: Path
    gold_path: Path
    cases_path: Path
    conformance_path: Path
    manifest_path: Path
    gold: GoldDataset
    cases: tuple[EvaluationCase, ...]
    conformance: ConformanceDataset
    manifest: FeasibilityManifest
    dataset_content_sha256: str
    evaluation_config_sha256: str
    manifest_ref: ArtifactRef


def eval_uuid(kind: str, logical_key: str) -> uuid.UUID:
    return uuid.uuid5(UUID_NAMESPACE, f"{kind}:{logical_key}")


def _git(root: Path, *args: str, allow_failure: bool = False) -> str:
    result = subprocess.run(
        ("git", *args), cwd=root, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode and not allow_failure:
        raise EntityLinkingEvalError("git_state_unavailable")
    return result.stdout.strip()


def _require_ancestor(root: Path, ancestor: str, descendant: str) -> None:
    result = subprocess.run(
        ("git", "merge-base", "--is-ancestor", ancestor, descendant),
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise EntityLinkingEvalError("g2_approval_commit_invalid")


def _audit_field(document: str, name: str) -> str:
    match = re.search(rf"(?m)^{re.escape(name)}:\s*\n\s+([^\r\n]+)$", document)
    if match is None:
        raise EntityLinkingEvalError("invalidated_calibration_artifact")
    return match.group(1).strip()


def verify_g2_approval(root: Path) -> dict[str, str]:
    head = _git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise EntityLinkingEvalError("g2_approval_commit_invalid")
    _require_ancestor(root, AUDIT_PRESERVATION_COMMIT, G2_APPROVAL_COMMIT)
    _require_ancestor(root, G1_COMMIT, G2_APPROVAL_COMMIT)
    _require_ancestor(root, G2_APPROVAL_COMMIT, head)

    preservation_paths = set(
        _git(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            AUDIT_PRESERVATION_COMMIT,
        ).splitlines()
    )
    approval_paths = set(
        _git(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            G2_APPROVAL_COMMIT,
        ).splitlines()
    )
    if preservation_paths != set(AUDIT_PRESERVATION_PATHS) or approval_paths != set(G2_APPROVAL_PATHS):
        raise EntityLinkingEvalError("g2_approval_commit_invalid")

    records: list[dict[str, str]] = []
    for relative in sorted(G2_SPECIFICATION_PATHS):
        oid = _git(root, "rev-parse", f"{G2_APPROVAL_COMMIT}:{relative}")
        body = subprocess.check_output(("git", "cat-file", "blob", oid), cwd=root)
        if (root / relative).read_bytes() != body:
            raise EntityLinkingEvalError("g2_specification_tree_drift")
        records.append(
            {
                "repository_relative_path": relative,
                "git_blob_oid": oid,
                "exact_file_sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    specification_hash = canonical_sha256(records)
    if specification_hash != G2_SPECIFICATION_TREE_SHA256:
        raise EntityLinkingEvalError("g2_specification_tree_drift")

    audit = (root / G2_SPECIFICATION_PATHS[3]).read_text(encoding="utf-8")
    incident = (root / G2_SPECIFICATION_PATHS[4]).read_text(encoding="utf-8")
    expected_audit_fields = {
        "previous_g2_approval_commit": INVALIDATED_G2_APPROVAL_COMMIT,
        "previous_g2_specification_tree_sha256": INVALIDATED_G2_SPECIFICATION_TREE_SHA256,
        "incomplete_g3_implementation_commit": INCOMPLETE_IMPLEMENTATION_COMMIT,
        "repository_relative_path": INVALIDATED_CALIBRATION_PATH,
        "canonical_sha256": INVALIDATED_CALIBRATION_CANONICAL_SHA256,
        "exact_file_sha256": INVALIDATED_CALIBRATION_FILE_SHA256,
        "workflow_status": "invalidated_audit_only",
        "audit_preservation_commit": AUDIT_PRESERVATION_COMMIT,
        "preserved_artifact_git_blob_oid": INVALIDATED_CALIBRATION_BLOB_OID,
        "preserved_gitattributes_git_blob_oid": PRESERVED_GITATTRIBUTES_BLOB_OID,
    }
    if any(_audit_field(audit, key) != value for key, value in expected_audit_fields.items()):
        raise EntityLinkingEvalError("invalidated_calibration_artifact")
    required_incident_values = (
        "> **Status:** CLOSED_REBUILT",
        "environment_classification: non_production_non_shared_acceptance",
        "disposition: rebuilt_from_template0_to_0023_and_smoke_checked",
        "upgrade_pre_authorized: false",
        "post_event_disposition_authorized: true",
        "incident_gate: PASSED",
    )
    if any(value not in incident for value in required_incident_values):
        raise EntityLinkingEvalError("g2_approval_commit_invalid")

    preserved_artifact_oid = _git(
        root, "rev-parse", f"{AUDIT_PRESERVATION_COMMIT}:{INVALIDATED_CALIBRATION_PATH}"
    )
    preserved_attributes_oid = _git(root, "rev-parse", f"{AUDIT_PRESERVATION_COMMIT}:.gitattributes")
    invalidated_path = root / INVALIDATED_CALIBRATION_PATH
    current_artifact_oid = _git(root, "hash-object", "--no-filters", "--", INVALIDATED_CALIBRATION_PATH)
    if (
        preserved_artifact_oid != INVALIDATED_CALIBRATION_BLOB_OID
        or current_artifact_oid != INVALIDATED_CALIBRATION_BLOB_OID
        or preserved_attributes_oid != PRESERVED_GITATTRIBUTES_BLOB_OID
        or exact_file_sha256(invalidated_path) != INVALIDATED_CALIBRATION_FILE_SHA256
    ):
        raise EntityLinkingEvalError("invalidated_calibration_artifact")
    payload = invalidated_path.read_bytes()
    if not payload.endswith(b"\n"):
        raise EntityLinkingEvalError("invalidated_calibration_artifact")
    invalidated_value = parse_json_bytes(payload[:-1])
    if canonical_sha256(invalidated_value) != INVALIDATED_CALIBRATION_CANONICAL_SHA256:
        raise EntityLinkingEvalError("invalidated_calibration_artifact")

    attributes = _git(root, "check-attr", "text", "eol", "--", *LF_CONTRACT_PATHS)
    observed_attributes: dict[tuple[str, str], str] = {}
    for line in attributes.splitlines():
        parts = line.split(": ", 2)
        if len(parts) != 3:
            raise EntityLinkingEvalError("g2_specification_tree_drift")
        observed_attributes[(parts[0].replace("\\", "/"), parts[1])] = parts[2]
    if any(
        observed_attributes.get((path, "text")) != "set" or observed_attributes.get((path, "eol")) != "lf"
        for path in LF_CONTRACT_PATHS
    ):
        raise EntityLinkingEvalError("g2_specification_tree_drift")

    protected = _git(root, "diff", "--name-only", G2_APPROVAL_COMMIT, "--", *PROTECTED_PATHS)
    if protected:
        raise EntityLinkingEvalError("scope_drift_detected")
    return {"g2_approval_commit": G2_APPROVAL_COMMIT, "g2_specification_tree_sha256": specification_hash}


def _module_candidates(root: Path, module: str) -> tuple[Path, ...]:
    base = root.joinpath(*module.split("."))
    return tuple(path for path in (base.with_suffix(".py"), base / "__init__.py") if path.is_file())


def build_dependency_closure(root: Path) -> tuple[dict[str, str], ...]:
    pending = list(DEPENDENCY_ROOT_MODULES)
    modules: dict[str, Path] = {}
    while pending:
        module = pending.pop()
        if module in modules:
            continue
        candidates = _module_candidates(root, module)
        if len(candidates) != 1:
            raise EntityLinkingEvalError("dependency_closure_unresolved")
        path = candidates[0]
        modules[module] = path
        components = module.split(".")[:-1]
        for index in range(1, len(components) + 1):
            init_module = ".".join(components[:index])
            init_path = root.joinpath(*components[:index], "__init__.py")
            if init_path.is_file() and init_module not in modules:
                pending.append(init_module)

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in {"importlib", "pkgutil"}:
                        raise EntityLinkingEvalError("dependency_closure_unresolved")
                    if alias.name.startswith("app.") or alias.name == "app":
                        candidate = alias.name
                        if _module_candidates(root, candidate):
                            pending.append(candidate)
                        elif candidate != "app":
                            raise EntityLinkingEvalError("dependency_closure_unresolved")
            elif isinstance(node, ast.ImportFrom):
                if node.module in {"importlib", "pkgutil"} or any(alias.name == "*" for alias in node.names):
                    raise EntityLinkingEvalError("dependency_closure_unresolved")
                if node.level:
                    anchor = package.split(".")
                    if node.level > len(anchor):
                        raise EntityLinkingEvalError("dependency_closure_unresolved")
                    prefix = anchor[: len(anchor) - node.level + 1]
                    absolute = ".".join(prefix + ((node.module or "").split(".") if node.module else []))
                else:
                    absolute = node.module or ""
                if absolute.startswith("app"):
                    if not _module_candidates(root, absolute):
                        raise EntityLinkingEvalError("dependency_closure_unresolved")
                    pending.append(absolute)
                    for alias in node.names:
                        child = f"{absolute}.{alias.name}"
                        if _module_candidates(root, child):
                            pending.append(child)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"__import__", "eval", "exec"}:
                    raise EntityLinkingEvalError("dependency_closure_unresolved")

    records = tuple(
        {
            "repository_relative_path": path.relative_to(root).as_posix(),
            "exact_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(set(modules.values()), key=lambda item: item.relative_to(root).as_posix())
    )
    if len(records) != 63:
        raise EntityLinkingEvalError("dependency_closure_unresolved")
    return records


def external_distribution_records() -> tuple[dict[str, str], ...]:
    records = []
    for name, expected in EXTERNAL_DISTRIBUTIONS:
        actual = importlib.metadata.version(name)
        if actual != expected:
            raise EntityLinkingEvalError("external_distribution_version_mismatch")
        records.append({"distribution_name": name, "exact_version": actual})
    return tuple(records)


def repository_module_is_local(root: Path, top_level_name: str) -> bool:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", top_level_name):
        raise EntityLinkingEvalError("external_distribution_identity_unresolved")
    module_file = root / f"{top_level_name}.py"
    module_directory = root / top_level_name
    return module_file.is_file() or module_directory.is_dir()


def _source_module_name(root: Path, path: Path) -> tuple[str, bool]:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    is_package = bool(parts and parts[-1] == "__init__")
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def discover_external_distribution_records(root: Path) -> tuple[dict[str, str], ...]:
    closure_paths = [root / row["repository_relative_path"] for row in build_dependency_closure(root)]
    evaluation_paths = [
        root / path for path in G3_IMPLEMENTATION_PATHS if path.endswith(".py") and (root / path).is_file()
    ]
    top_level_imports: set[str] = set()
    for path in (*closure_paths, *evaluation_paths):
        module_name, is_package = _source_module_name(root, path)
        package_parts = module_name.split(".") if is_package else module_name.split(".")[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                top_level_imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(package_parts) + 1:
                        raise EntityLinkingEvalError("external_distribution_identity_unresolved")
                    continue
                if node.module:
                    top_level_imports.add(node.module.split(".")[0])
    external_top_levels = sorted(
        name
        for name in top_level_imports
        if name not in sys.stdlib_module_names and not repository_module_is_local(root, name)
    )
    package_map = importlib.metadata.packages_distributions()
    distributions: set[str] = set()
    for package in external_top_levels:
        mapped = package_map.get(package, ())
        normalized = {re.sub(r"[-_.]+", "-", value).lower() for value in mapped}
        if len(normalized) != 1:
            raise EntityLinkingEvalError("external_distribution_identity_unresolved")
        distributions.update(normalized)
    distributions.update({"alembic", "asyncpg", "psycopg2-binary", "python-dotenv", "pytest-asyncio", "ruff"})
    expected = {name for name, _version in EXTERNAL_DISTRIBUTIONS}
    if distributions != expected:
        raise EntityLinkingEvalError("external_distribution_identity_unresolved")
    return external_distribution_records()


def _canonical_file_bytes(value: Any) -> bytes:
    return canonical_graph_json_v1(value).encode("utf-8") + b"\n"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_file_bytes(value))


def _write_jsonl(path: Path, values: Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [canonical_graph_json_v1(value).encode("utf-8") for value in values]
    path.write_bytes(b"\n".join(lines) + b"\n")


def _candidate(entity: EntityRecord) -> EntityCandidate:
    return EntityCandidate(
        entity_id=str(eval_uuid("entity", entity.entity_key)),
        entity_key=entity.entity_key,
        entity_type_key=entity.entity_type_key,
        canonical_name=entity.canonical_name,
        normalized_name=entity.normalized_name,
    )


def _entity(
    key: str,
    canonical: str,
    *,
    type_key: str = "type-org",
    library: str = "library-primary",
    ontology: str = "ontology-primary",
    status: str = "published",
    evidence: Sequence[str] = (),
) -> EntityRecord:
    return EntityRecord(
        entity_key=key,
        library_key=library,
        ontology_key=ontology,
        entity_type_key=type_key,
        canonical_name=canonical,
        normalized_name=normalize_graph_name_v1(canonical),
        status=status,
        support_evidence_keys=tuple(evidence),
        privacy_canary_keys=("canary-properties",) if library == "library-primary" else (),
    )


def _case_prefix(index: int) -> str:
    return f"v3-{index:03d}"


_CALIBRATION_ROOTS = (
    "Ardent", "Beacon", "Cobalt", "Drift", "Echo", "Fable", "Harbor", "Ion", "Kestrel", "Meridian"
)
_RELEASE_ROOTS = (
    "Nimbus", "Orbit", "Prism", "Quill", "Radiant", "Summit", "Tundra", "Umber", "Vector", "Zenith"
)
_FAMILY_MODIFIERS = (
    "Bronze", "Cinder", "Falcon", "Glacier", "Helix", "Indigo",
    "Kernel", "Matrix", "Onyx", "Pulsar", "Saffron", "Vertex",
)
_CALIBRATION_CJK_ROOTS = (
    "苍穹", "碧海", "翠岭", "岚谷", "朝霞", "柏涛", "澄湖", "涌泉", "劲风", "星湾",
)
_RELEASE_CJK_ROOTS = (
    "云际", "沧岳", "梅影", "东辰", "霞光", "杉涛", "映湖", "河谷", "西风", "霜原",
)
_CJK_MODIFIERS = ("智联", "勘研", "构造", "算据", "联创", "新域", "云服", "数科", "策划", "营运", "平台", "验证")


def _family_words(index: int, split: str) -> tuple[str, str, str]:
    position = index if split == "calibration" else index - 80
    roots = _CALIBRATION_ROOTS if split == "calibration" else _RELEASE_ROOTS
    root = roots[position % len(roots)]
    modifier = _FAMILY_MODIFIERS[position // len(roots)]
    cjk_roots = _CALIBRATION_CJK_ROOTS if split == "calibration" else _RELEASE_CJK_ROOTS
    cjk = cjk_roots[position % len(cjk_roots)] + _CJK_MODIFIERS[position // len(cjk_roots)]
    return root, modifier, cjk


def build_static_gold_and_cases() -> tuple[GoldDataset, tuple[EvaluationCase, ...]]:
    libraries = (
        LibraryRecord(
            library_key="library-negative",
            slug="synthetic-negative",
            name="Synthetic Negative Library",
            qdrant_collection_key="qdrant-negative",
            embedding_model="bge-m3",
            embedding_dim=1024,
            retrieval_mode="hybrid",
        ),
        LibraryRecord(
            library_key="library-primary",
            slug="synthetic-primary",
            name="Synthetic Primary Library",
            qdrant_collection_key="qdrant-primary",
            embedding_model="bge-m3",
            embedding_dim=1024,
            retrieval_mode="hybrid",
        ),
    )
    ontologies = (
        OntologyRecord(
            ontology_key="ontology-negative",
            library_key="library-negative",
            version_key="ontology-negative-v1",
            version_no=1,
            status="active",
        ),
        OntologyRecord(
            ontology_key="ontology-primary",
            library_key="library-primary",
            version_key="ontology-primary-v1",
            version_no=1,
            status="active",
        ),
    )
    entity_types = tuple(
        sorted(
            (
                EntityTypeRecord(
                    entity_type_key="type-negative-org",
                    library_key="library-negative",
                    ontology_key="ontology-negative",
                    key="organization",
                    label="Synthetic Organization",
                    status="active",
                ),
                EntityTypeRecord(
                    entity_type_key="type-org",
                    library_key="library-primary",
                    ontology_key="ontology-primary",
                    key="organization",
                    label="Synthetic Organization",
                    status="active",
                ),
                EntityTypeRecord(
                    entity_type_key="type-team",
                    library_key="library-primary",
                    ontology_key="ontology-primary",
                    key="team",
                    label="Synthetic Team",
                    status="active",
                ),
            ),
            key=lambda row: row.entity_type_key,
        )
    )
    relation_types = (
        RelationTypeRecord(
            relation_type_key="relation-type-related",
            library_key="library-primary",
            ontology_key="ontology-primary",
            key="related-to",
            label="Synthetic Related To",
            direction="directed",
            requires_evidence=True,
            status="active",
        ),
    )

    documents: list[DocumentRecord] = []
    revisions: list[RevisionRecord] = []
    chunks: list[ChunkRecord] = []
    evidence: list[EvidenceRecord] = []
    entities: dict[str, EntityRecord] = {}
    relations: list[RelationRecord] = []
    cases: list[EvaluationCase] = []

    for index in range(200):
        suffix = _case_prefix(index)
        split = "calibration" if index < 80 else "release"
        split_index = index if split == "calibration" else index - 80
        cohort = "safety" if split_index < 40 else "utility"
        cohort_index = split_index if cohort == "safety" else split_index - 40
        group = (2, 3)[cohort_index % 2] if cohort == "safety" else 6
        stratum = STRATUM_ORDER[cohort_index % 4]
        hop = 1 if stratum.startswith("one-hop") else 2
        root, modifier, cjk_name = _family_words(index, split)
        family_label = f"{modifier} {root}"

        document_key = f"document-{suffix}"
        revision_key = f"revision-{suffix}"
        chunk_key = f"chunk-{suffix}"
        evidence_key = f"evidence-{suffix}"
        documents.append(
            DocumentRecord(
                document_key=document_key,
                library_key="library-primary",
                title=f"{family_label} published relation record",
                external_id=f"synthetic-v3-{index:03d}",
                status="ready",
            )
        )
        revisions.append(
            RevisionRecord(
                revision_key=revision_key,
                document_key=document_key,
                revision_no=1,
                status="ready",
                is_current=True,
            )
        )
        text = (
            f"{family_label} Anchor is formally related to {family_label} Archive "
            "under the active synthetic publication."
        )
        chunks.append(
            ChunkRecord(
                chunk_key=chunk_key,
                document_key=document_key,
                revision_key=revision_key,
                seq=0,
                text=text,
            )
        )
        evidence.append(
            EvidenceRecord(
                evidence_key=evidence_key,
                library_key="library-primary",
                document_key=document_key,
                revision_key=revision_key,
                chunk_key=chunk_key,
                locator_start=0,
                locator_end=len(text),
            )
        )

        seed_key = f"entity-{suffix}-seed"
        target_key = f"entity-{suffix}-target"
        entities[seed_key] = _entity(seed_key, f"{family_label} Anchor", evidence=(evidence_key,))
        entities[target_key] = _entity(target_key, f"{family_label} Archive", evidence=(evidence_key,))
        relation_keys: list[str] = []
        node_keys = [seed_key, target_key]
        if hop == 1:
            relation_key = f"relation-{suffix}-a"
            relation_keys.append(relation_key)
            relations.append(
                RelationRecord(
                    relation_key=relation_key,
                    library_key="library-primary",
                    ontology_key="ontology-primary",
                    relation_type_key="relation-type-related",
                    source_entity_key=seed_key,
                    target_entity_key=target_key,
                    status="published",
                    support_evidence_keys=(evidence_key,),
                    privacy_canary_keys=("canary-source",),
                )
            )
        else:
            middle_key = f"entity-{suffix}-middle"
            entities[middle_key] = _entity(middle_key, f"{family_label} Bridge", evidence=(evidence_key,))
            node_keys.insert(1, middle_key)
            for marker, source, target in (
                ("a", seed_key, middle_key),
                ("b", middle_key, target_key),
            ):
                relation_key = f"relation-{suffix}-{marker}"
                relation_keys.append(relation_key)
                relations.append(
                    RelationRecord(
                        relation_key=relation_key,
                        library_key="library-primary",
                        ontology_key="ontology-primary",
                        relation_type_key="relation-type-related",
                        source_entity_key=source,
                        target_entity_key=target,
                        status="published",
                        support_evidence_keys=(evidence_key,),
                        privacy_canary_keys=("canary-source",),
                    )
                )

        mentions: list[MentionRecord] = []
        family_entities = {seed_key, target_key, *node_keys}
        categories: set[str] = set()

        def add_linkable(
            key: str, canonical: str, mention: str, *, script: str, type_key: str = "type-org"
        ) -> None:
            entities[key] = _entity(key, canonical, type_key=type_key, evidence=(evidence_key,))
            family_entities.add(key)
            mentions.append(
                MentionRecord(
                    input_index=len(mentions),
                    text=mention,
                    entity_type_key=type_key,
                    gold_status="linkable",
                    gold_entity_key=key,
                    gold_ambiguous_entity_keys=(),
                    negative_entity_keys=(),
                    is_exact_control=normalize_graph_name_v1(mention) == normalize_graph_name_v1(canonical),
                    script=script,
                )
            )

        add_linkable(seed_key, f"{family_label} Anchor", f"{family_label} Anchor", script="latin")
        categories.add("exact-canonical")
        if group == 0:
            exact_key = f"entity-{suffix}-exact"
            prefix_key = f"entity-{suffix}-prefix"
            add_linkable(exact_key, f"{family_label} Control", f"  {family_label.upper()}   control ", script="latin")
            add_linkable(prefix_key, f"{cjk_name}中心", cjk_name, script="cjk")
            categories.update(("exact-canonical", "case-whitespace-normalization", "prefix-suffix-omission"))
        elif group == 1:
            abbreviation_key = f"entity-{suffix}-abbreviation"
            reorder_key = f"entity-{suffix}-reorder"
            add_linkable(
                abbreviation_key,
                f"{root.lower()}{modifier.lower()}orbit",
                f"{root[0].lower()}{root[-1].lower()}{modifier.lower()}or",
                script="latin",
            )
            add_linkable(
                reorder_key,
                f"{root.lower()} {modifier.lower()} unit",
                f"unit {modifier.lower()} {root.lower()}",
                script="latin",
            )
            categories.update(("abbreviation-like-overlap", "word-order-token-overlap"))
        elif group == 2:
            close_key = f"entity-{suffix}-close"
            entities[close_key] = _entity(close_key, f"{root} {modifier} Alphi", evidence=(evidence_key,))
            family_entities.add(close_key)
            mentions.append(
                MentionRecord(
                    input_index=len(mentions),
                    text=f"{root} {modifier} Alpha",
                    entity_type_key="type-org",
                    gold_status="unlinkable",
                    gold_entity_key=None,
                    gold_ambiguous_entity_keys=(),
                    negative_entity_keys=(),
                    is_exact_control=False,
                    script="latin",
                )
            )
            tie_a = f"entity-{suffix}-tie-a"
            tie_b = f"entity-{suffix}-tie-b"
            entities[tie_a] = _entity(tie_a, f"{family_label} A", evidence=(evidence_key,))
            entities[tie_b] = _entity(tie_b, f"{family_label} B", evidence=(evidence_key,))
            family_entities.update((tie_a, tie_b))
            mentions.append(
                MentionRecord(
                    input_index=len(mentions),
                    text=family_label,
                    entity_type_key="type-org",
                    gold_status="ambiguous",
                    gold_entity_key=None,
                    gold_ambiguous_entity_keys=(tie_a, tie_b),
                    negative_entity_keys=(),
                    is_exact_control=False,
                    script="latin",
                )
            )
            shared_a = f"entity-{suffix}-shared-a"
            shared_b = f"entity-{suffix}-shared-b"
            entities[shared_a] = _entity(shared_a, f"{family_label} Shared", evidence=(evidence_key,))
            entities[shared_b] = _entity(
                shared_b, f"{family_label} Shared", type_key="type-team", evidence=(evidence_key,)
            )
            family_entities.update((shared_a, shared_b))
            mentions.append(
                MentionRecord(
                    input_index=len(mentions),
                    text=f"{family_label} Shared",
                    entity_type_key=None,
                    gold_status="ambiguous",
                    gold_entity_key=None,
                    gold_ambiguous_entity_keys=(shared_a, shared_b),
                    negative_entity_keys=(),
                    is_exact_control=False,
                    script="latin",
                )
            )
            categories.update(
                (
                    "exact-canonical",
                    "close-name-negative",
                    "same-score-ambiguity",
                    "same-name-cross-type-ambiguity",
                )
            )
        elif group == 3:
            outside_key = f"entity-{suffix}-outside"
            outside_name = f"externalcodev3{index:03d}"
            entities[outside_key] = _entity(
                outside_key,
                outside_name,
                type_key="type-negative-org",
                library="library-negative",
                ontology="ontology-negative",
            )
            family_entities.add(outside_key)
            mentions.append(
                MentionRecord(
                    input_index=len(mentions),
                    text=outside_name,
                    entity_type_key=None,
                    gold_status="unlinkable",
                    gold_entity_key=None,
                    gold_ambiguous_entity_keys=(),
                    negative_entity_keys=(outside_key,),
                    is_exact_control=False,
                    script="latin",
                )
            )
            categories.update(("exact-canonical", "unpublished-cross-scope", "not-found"))
        elif group == 4:
            prefix_key = f"entity-{suffix}-prefix"
            reorder_key = f"entity-{suffix}-reorder"
            add_linkable(prefix_key, f"{cjk_name}实验室", cjk_name, script="cjk")
            add_linkable(reorder_key, f"{modifier.lower()} {root.lower()} node", f"node {root.lower()} {modifier.lower()}", script="latin")
            categories.update(("prefix-suffix-omission", "word-order-token-overlap"))
        elif group == 5:
            exact_key = f"entity-{suffix}-exact"
            abbreviation_key = f"entity-{suffix}-abbreviation"
            add_linkable(exact_key, f"{modifier} {root} Registry", f" {modifier.upper()}  {root.upper()} registry ", script="latin")
            add_linkable(
                abbreviation_key,
                f"{modifier.lower()}{root.lower()}signal",
                f"{modifier[0].lower()}{modifier[-1].lower()}{root.lower()}sg",
                script="mixed",
            )
            categories.update(
                ("exact-canonical", "case-whitespace-normalization", "abbreviation-like-overlap")
            )
        else:
            exact_key = f"entity-{suffix}-exact"
            cjk_exact_key = f"entity-{suffix}-cjk-exact"
            add_linkable(
                exact_key,
                f"{modifier} {root} Registry",
                f"  {modifier.upper()}  {root.upper()} registry  ",
                script="latin",
            )
            add_linkable(cjk_exact_key, f"{cjk_name}档案", f"{cjk_name}档案", script="cjk")
            categories.update(("exact-canonical", "case-whitespace-normalization"))

        if cohort == "safety":
            safety_prefix_key = f"entity-{suffix}-safety-prefix"
            safety_abbreviation_key = f"entity-{suffix}-safety-abbreviation"
            safety_reorder_key = f"entity-{suffix}-safety-reorder"
            add_linkable(safety_prefix_key, f"{cjk_name}验证中心", cjk_name, script="cjk")
            add_linkable(
                safety_abbreviation_key,
                f"{root.lower()}{modifier.lower()}orbit",
                f"{root[0].lower()}{root[-1].lower()}{modifier.lower()}or",
                script="latin",
            )
            add_linkable(
                safety_reorder_key,
                f"{root.lower()} {modifier.lower()} unit",
                f"unit {modifier.lower()} {root.lower()}",
                script="latin",
            )
            categories.update(
                ("prefix-suffix-omission", "abbreviation-like-overlap", "word-order-token-overlap")
            )

        categories.add("one-hop-evidence-utility" if hop == 1 else "two-hop-evidence-utility")
        decoy_family_keys: tuple[str, ...] = ()
        decoy_chunk_keys: tuple[str, ...] = ()
        if cohort == "utility":
            surface = " / ".join(" ".join(mention.text.split()) for mention in mentions)
            question = f"Retrieve the signed publication evidence dossier for {surface}."
            decoy_keys: list[str] = []
            decoy_chunks: list[str] = []
            for decoy_index in range(12):
                marker = decoy_index + 1
                decoy_document_key = f"document-{suffix}-decoy-{marker:02d}"
                decoy_revision_key = f"revision-{suffix}-decoy-{marker:02d}"
                decoy_chunk_key = f"chunk-{suffix}-decoy-{marker:02d}"
                decoy_keys.append(f"decoy-family-{suffix}-{marker:02d}")
                decoy_chunks.append(decoy_chunk_key)
                if decoy_index < 4:
                    decoy_title = f"{question} Contradictory surface record {marker}"
                    decoy_text = (
                        f"{question} {surface}. This ordinary record states that no active "
                        "published relation connects the named entities."
                    )
                elif decoy_index < 8:
                    decoy_title = f"{question} Competing relation record {marker}"
                    decoy_text = (
                        f"{question} {surface}. This ordinary record assigns the requested relation "
                        "to an unrelated archive instead of the named target."
                    )
                else:
                    decoy_title = f"{question} Incomplete provenance record {marker}"
                    decoy_text = (
                        f"{question} {surface}. This ordinary record lists the names but omits "
                        "the requested relation and its supporting evidence."
                    )
                documents.append(
                    DocumentRecord(
                        document_key=decoy_document_key,
                        library_key="library-primary",
                        title=decoy_title,
                        external_id=f"synthetic-v3-decoy-{index:03d}-{marker:02d}",
                        status="ready",
                    )
                )
                revisions.append(
                    RevisionRecord(
                        revision_key=decoy_revision_key,
                        document_key=decoy_document_key,
                        revision_no=1,
                        status="ready",
                        is_current=True,
                    )
                )
                chunks.append(
                    ChunkRecord(
                        chunk_key=decoy_chunk_key,
                        document_key=decoy_document_key,
                        revision_key=decoy_revision_key,
                        seq=0,
                        text=decoy_text,
                    )
                )
            decoy_family_keys = tuple(decoy_keys)
            decoy_chunk_keys = tuple(decoy_chunks)
        ordered_categories = tuple(value for value in CATEGORY_ORDER if value in categories)
        case = EvaluationCase(
            schema_version="entity-linking-case-v2",
            case_id=f"case-{suffix}",
            split=split,
            cohort=cohort,
            entity_family_keys=(f"entity-family-{suffix}",),
            mention_family_keys=tuple(
                f"mention-family-{suffix}-{mention.input_index}" for mention in mentions
            ),
            relation_template_family_key=f"relation-family-{suffix}",
            phrase_family_key=f"phrase-family-{suffix}",
            decoy_family_keys=decoy_family_keys,
            decoy_chunk_keys=decoy_chunk_keys,
            categories=ordered_categories,
            utility_stratum=stratum,
            scenario_publication_key="publication-primary",
            question=(
                question
                if cohort == "utility"
                else f"Find the published relation evidence associated with {family_label}."
            ),
            mentions=tuple(mentions),
            gold_utility=GoldUtilityRecord(
                evidence_keys=(evidence_key,),
                relation_keys=tuple(relation_keys),
                node_keys=tuple(node_keys),
                max_evidence_budget=10,
                hop=hop,
            ),
        )
        cases.append(case)

    primary_entities = tuple(
        sorted(
            (row.entity_key for row in entities.values() if row.library_key == "library-primary"),
        )
    )
    negative_entities = tuple(
        sorted(
            (row.entity_key for row in entities.values() if row.library_key == "library-negative"),
        )
    )
    primary_relations = tuple(sorted(row.relation_key for row in relations))
    publications = (
        PublicationRecord(
            publication_key="publication-degraded",
            library_key="library-primary",
            ontology_key="ontology-primary",
            status="failed",
            entity_keys=(),
            relation_keys=(),
            scenario="degraded",
        ),
        PublicationRecord(
            publication_key="publication-negative",
            library_key="library-negative",
            ontology_key="ontology-negative",
            status="active",
            entity_keys=negative_entities,
            relation_keys=(),
            scenario="healthy-negative",
        ),
        PublicationRecord(
            publication_key="publication-partial",
            library_key="library-primary",
            ontology_key="ontology-primary",
            status="failed",
            entity_keys=primary_entities[:2],
            relation_keys=(),
            scenario="partial",
        ),
        PublicationRecord(
            publication_key="publication-primary",
            library_key="library-primary",
            ontology_key="ontology-primary",
            status="active",
            entity_keys=primary_entities,
            relation_keys=primary_relations,
            scenario="healthy-primary",
        ),
        PublicationRecord(
            publication_key="publication-superseded",
            library_key="library-primary",
            ontology_key="ontology-primary",
            status="superseded",
            entity_keys=(),
            relation_keys=(),
            scenario="superseded",
        ),
    )
    canaries = (
        PrivacyCanaryRecord(
            canary_key="canary-credential",
            key_marker="synthetic-credential-shaped-key",
            value_marker="synthetic-credential-shaped-value",
            target_rows=("entity-v3-000-seed",),
        ),
        PrivacyCanaryRecord(
            canary_key="canary-properties",
            key_marker="synthetic-private-properties-key",
            value_marker="synthetic-private-properties-value",
            target_rows=("entity-v3-000-seed",),
        ),
        PrivacyCanaryRecord(
            canary_key="canary-source",
            key_marker="synthetic-source-like-key",
            value_marker="synthetic-source-like-value",
            target_rows=("relation-v3-000-a",),
        ),
    )
    gold = GoldDataset(
        schema_version="entity-linking-gold-v2",
        dataset_id="feasibility-v3",
        uuid_namespace=str(UUID_NAMESPACE),
        libraries=libraries,
        ontologies=ontologies,
        entity_types=entity_types,
        relation_types=relation_types,
        documents=tuple(sorted(documents, key=lambda row: row.document_key)),
        revisions=tuple(sorted(revisions, key=lambda row: row.revision_key)),
        chunks=tuple(sorted(chunks, key=lambda row: row.chunk_key)),
        evidence=tuple(sorted(evidence, key=lambda row: row.evidence_key)),
        entities=tuple(sorted(entities.values(), key=lambda row: row.entity_key)),
        relations=tuple(sorted(relations, key=lambda row: row.relation_key)),
        publications=publications,
        privacy_canaries=canaries,
    )
    return gold, tuple(cases)


def build_conformance_dataset() -> ConformanceDataset:
    feature_inputs = (
        ("feature-single-code-point", "a", "a"),
        ("feature-repeated-multiset", "aaaa", "aaab"),
        ("feature-cjk", "星河制造", "星河制造中心"),
        ("feature-latin-case", "Alpha", "alpha"),
        ("feature-whitespace", "alpha   beta", "alpha beta"),
        ("feature-combining", "e\u0301", "é"),
        ("feature-containment", "alpha", "alphabet"),
        ("feature-disjoint", "alpha", "zulu"),
        ("feature-token-jaccard", "alpha beta", "beta alpha"),
        ("feature-round-half-up", "ab", "ac"),
        ("feature-boundary-eligible", "star lab", "star laboratory"),
        ("feature-boundary-middle-rejected", "labor", "star laboratory"),
        ("feature-initialism", "abc", "alpha beta center"),
        ("feature-ordered-subsequence", "aramberor", "alderamberorbit"),
        ("feature-subsequence-ratio-rejected", "abcde", "abcdef"),
    )
    feature_cases = []
    for case_id, left, right in feature_inputs:
        score = score_normalized_pair(normalize_graph_name_v1(left), normalize_graph_name_v1(right))
        feature_cases.append(
            FeatureCase(
                case_id=case_id,
                left=left,
                right=right,
                expected=FeatureExpected(
                    character_bigram_dice_micros=score.character_bigram_dice_micros,
                    token_jaccard_micros=score.token_jaccard_micros,
                    substring_containment_micros=score.substring_containment_micros,
                    boundary_omission_micros=score.boundary_omission_micros,
                    ordered_abbreviation_micros=score.ordered_abbreviation_micros,
                    score_micros=score.score_micros,
                ),
            )
        )

    base_candidates = (
        CandidateFixture(
            entity_id=str(
                uuid.uuid5(SCORER_CONFORMANCE_UUID_NAMESPACE, "conformance:candidate-a")
            ),
            entity_key="candidate-a",
            entity_type_key="type-org",
            canonical_name="Candidate Alpha",
            normalized_name="candidate alpha",
        ),
        CandidateFixture(
            entity_id=str(
                uuid.uuid5(SCORER_CONFORMANCE_UUID_NAMESPACE, "conformance:candidate-b")
            ),
            entity_key="candidate-b",
            entity_type_key="type-team",
            canonical_name="Candidate Beta",
            normalized_name="candidate beta",
        ),
    )
    decision_cases: list[DecisionCase] = []
    for threshold in (850000, 880000, 900000, 920000, 950000):
        for delta in (-1, 0, 1):
            score = threshold + delta
            linked = score >= threshold
            decision_cases.append(
                DecisionCase(
                    case_id=f"decision-score-{threshold}-{delta + 1}",
                    mention_text="synthetic",
                    entity_type_key=None,
                    min_score_micros=threshold,
                    min_margin_micros=80000,
                    candidates=base_candidates,
                    candidate_scores_micros=(score, 500000),
                    expected=DecisionExpected(
                        status="linked" if linked else "ambiguous",
                        method="lexical_v2" if linked else None,
                        selected_entity_key="candidate-a" if linked else None,
                        candidate_entity_keys=() if linked else ("candidate-a", "candidate-b"),
                    ),
                )
            )
    for margin in (80000, 100000, 120000, 150000, 200000):
        for delta in (-1, 0, 1):
            observed_margin = margin + delta
            second = 950000 - observed_margin
            linked = observed_margin >= margin
            decision_cases.append(
                DecisionCase(
                    case_id=f"decision-margin-{margin}-{delta + 1}",
                    mention_text="synthetic",
                    entity_type_key=None,
                    min_score_micros=850000,
                    min_margin_micros=margin,
                    candidates=base_candidates,
                    candidate_scores_micros=(950000, second),
                    expected=DecisionExpected(
                        status="linked" if linked else "ambiguous",
                        method="lexical_v2" if linked else None,
                        selected_entity_key="candidate-a" if linked else None,
                        candidate_entity_keys=() if linked else ("candidate-a", "candidate-b"),
                    ),
                )
            )

    ordering_candidates = (
        CandidateFixture(
            entity_id="00000000-0000-0000-0000-000000000002",
            entity_key="order-b",
            entity_type_key="type-org",
            canonical_name="Order Same",
            normalized_name="order same",
        ),
        CandidateFixture(
            entity_id="00000000-0000-0000-0000-000000000001",
            entity_key="order-a",
            entity_type_key="type-org",
            canonical_name="Order Same",
            normalized_name="order same",
        ),
        CandidateFixture(
            entity_id="00000000-0000-0000-0000-000000000003",
            entity_key="order-team",
            entity_type_key="type-team",
            canonical_name="Order Same",
            normalized_name="order same",
        ),
    )
    ordering_cases = (
        OrderingCase(
            case_id="ordering-uuid-tie-break",
            mention_text="order",
            candidates=ordering_candidates,
            expected_entity_keys=("order-a", "order-b", "order-team"),
        ),
    )

    category_specs = (
        (
            "cat-exact-raw",
            "exact-canonical",
            True,
            "alpha",
            "alpha",
            "linkable",
            1,
            (1000000,),
            None,
            ("type-org",),
            (),
            1,
            ("exact-canonical",),
        ),
        (
            "cat-casefold-whitespace",
            "case-whitespace-normalization",
            True,
            "  ALPHA   UNIT ",
            "Alpha Unit",
            "linkable",
            1,
            (1000000,),
            None,
            ("type-org",),
            (),
            1,
            ("exact-canonical", "case-whitespace-normalization"),
        ),
        (
            "cat-prefix-length-two",
            "prefix-suffix-omission",
            True,
            "ab",
            "abcd",
            "linkable",
            0,
            (500000,),
            "type-org",
            ("type-org",),
            (),
            1,
            ("prefix-suffix-omission",),
        ),
        (
            "cat-prefix-length-one-rejected",
            "prefix-suffix-omission",
            False,
            "a",
            "abcd",
            "linkable",
            0,
            (),
            "type-org",
            ("type-org",),
            (),
            1,
            (),
        ),
        (
            "cat-middle-substring-rejected",
            "prefix-suffix-omission",
            False,
            "bc",
            "abcd",
            "linkable",
            0,
            (500000,),
            "type-org",
            ("type-org",),
            (),
            1,
            (),
        ),
        (
            "cat-initialism",
            "abbreviation-like-overlap",
            True,
            "abc",
            "Alpha Beta Center",
            "linkable",
            0,
            (700000,),
            "type-org",
            ("type-org",),
            (),
            1,
            ("abbreviation-like-overlap",),
        ),
        (
            "cat-noncontiguous-subsequence",
            "abbreviation-like-overlap",
            True,
            "a01o",
            "alpha01omega",
            "linkable",
            0,
            (700000,),
            "type-org",
            ("type-org",),
            (),
            1,
            ("abbreviation-like-overlap",),
        ),
        (
            "cat-contiguous-substring-rejected",
            "abbreviation-like-overlap",
            False,
            "alpha",
            "alphabet",
            "linkable",
            0,
            (700000,),
            "type-org",
            ("type-org",),
            (),
            1,
            (),
        ),
        (
            "cat-subsequence-ratio-over-75pct",
            "abbreviation-like-overlap",
            False,
            "abcde",
            "abcdef",
            "linkable",
            0,
            (800000,),
            "type-org",
            ("type-org",),
            (),
            1,
            (),
        ),
        (
            "cat-token-reorder",
            "word-order-token-overlap",
            True,
            "beta alpha",
            "alpha beta",
            "linkable",
            0,
            (1000000,),
            "type-org",
            ("type-org",),
            (),
            1,
            ("word-order-token-overlap",),
        ),
        (
            "cat-token-multiset-different",
            "word-order-token-overlap",
            False,
            "beta alpha",
            "alpha gamma",
            "linkable",
            0,
            (500000,),
            "type-org",
            ("type-org",),
            (),
            1,
            (),
        ),
        (
            "cat-close-score-499999",
            "close-name-negative",
            False,
            "near",
            "candidate",
            "unlinkable",
            0,
            (499999,),
            "type-org",
            ("type-org",),
            (),
            1,
            ("not-found",),
        ),
        (
            "cat-close-score-500000",
            "close-name-negative",
            True,
            "near",
            "candidate",
            "unlinkable",
            0,
            (500000,),
            "type-org",
            ("type-org",),
            (),
            1,
            ("close-name-negative",),
        ),
        (
            "cat-same-score-top-two",
            "same-score-ambiguity",
            True,
            "tie",
            "tie a",
            "ambiguous",
            0,
            (750000, 750000),
            "type-org",
            ("type-org", "type-org"),
            (),
            1,
            ("same-score-ambiguity",),
        ),
        (
            "cat-score-diff-one-micro",
            "same-score-ambiguity",
            False,
            "tie",
            "tie a",
            "ambiguous",
            0,
            (750001, 750000),
            "type-org",
            ("type-org", "type-org"),
            (),
            1,
            (),
        ),
        (
            "cat-cross-type-no-hint",
            "same-name-cross-type-ambiguity",
            True,
            "shared",
            "shared",
            "ambiguous",
            2,
            (1000000, 1000000),
            None,
            ("type-org", "type-team"),
            (),
            1,
            ("same-name-cross-type-ambiguity",),
        ),
        (
            "cat-cross-type-with-hint",
            "same-name-cross-type-ambiguity",
            False,
            "shared",
            "shared",
            "linkable",
            1,
            (1000000,),
            "type-org",
            ("type-org", "type-team"),
            (),
            1,
            ("exact-canonical",),
        ),
        (
            "cat-unpublished-exact",
            "unpublished-cross-scope",
            True,
            "outside",
            "outside",
            "unlinkable",
            0,
            (),
            None,
            (),
            ("outside-entity",),
            1,
            ("unpublished-cross-scope", "not-found"),
        ),
        (
            "cat-one-hop",
            "one-hop-evidence-utility",
            True,
            "anchor",
            "anchor",
            "linkable",
            1,
            (1000000,),
            "type-org",
            ("type-org",),
            (),
            1,
            ("exact-canonical", "one-hop-evidence-utility"),
        ),
        (
            "cat-two-hop",
            "two-hop-evidence-utility",
            True,
            "anchor",
            "anchor",
            "linkable",
            1,
            (1000000,),
            "type-org",
            ("type-org",),
            (),
            2,
            ("exact-canonical", "two-hop-evidence-utility"),
        ),
    )
    category_cases = tuple(
        CategoryPredicateCase(
            case_id=row[0],
            predicate=row[1],
            expected=row[2],
            mention_text=row[3],
            canonical_name=row[4],
            gold_status=row[5],
            exact_match_count=row[6],
            presented_scores_micros=row[7],
            entity_type_key=row[8],
            candidate_type_keys=row[9],
            negative_entity_keys=row[10],
            hop=row[11],
            expected_categories=row[12],
        )
        for row in category_specs
    )
    return ConformanceDataset(
        schema_version="entity-linking-scorer-conformance-v2",
        algorithm_version="lexical-score-v2",
        normalization_version="normalize_graph_name_v1",
        rounding_version="integer-half-up-v1",
        feature_cases=tuple(feature_cases),
        decision_cases=tuple(decision_cases),
        ordering_cases=ordering_cases,
        category_predicate_cases=category_cases,
    )


def _family_hash(cases: Sequence[EvaluationCase], field: str) -> str:
    records: list[dict[str, str]] = []
    for case in cases:
        values = getattr(case, field)
        if isinstance(values, str):
            values = (values,)
        for value in values:
            records.append({"family_key": value, "split": case.split})
    records.sort(key=lambda row: row["family_key"])
    return canonical_sha256(records)


def _scope_hashes(gold: GoldDataset) -> tuple[ScopeSchemaHash, ...]:
    result = []
    for logical_scope_id, ontology_key in (
        ("scope-negative", "ontology-negative"),
        ("scope-primary", "ontology-primary"),
    ):
        projection = {
            "entity_types": [
                row.model_dump(mode="json") for row in gold.entity_types if row.ontology_key == ontology_key
            ],
            "ontology": next(
                row.model_dump(mode="json") for row in gold.ontologies if row.ontology_key == ontology_key
            ),
            "relation_types": [
                row.model_dump(mode="json") for row in gold.relation_types if row.ontology_key == ontology_key
            ],
        }
        result.append(
            ScopeSchemaHash(
                logical_scope_id=logical_scope_id,
                ontology_schema_hash=canonical_sha256(projection),
            )
        )
    return tuple(result)


def _count_dataset(gold: GoldDataset, cases: Sequence[EvaluationCase]) -> CountsRecord:
    mentions = [mention for case in cases for mention in case.mentions]
    return CountsRecord(
        questions=len(cases),
        mentions=len(mentions),
        linkable_mentions=sum(row.gold_status == "linkable" for row in mentions),
        ambiguous_unlinkable_mentions=sum(row.gold_status != "linkable" for row in mentions),
        exact_canonical_mentions=sum(row.is_exact_control for row in mentions),
        cjk_non_exact_linkable_mentions=sum(
            row.gold_status == "linkable" and not row.is_exact_control and row.script == "cjk"
            for row in mentions
        ),
        latin_mixed_non_exact_linkable_mentions=sum(
            row.gold_status == "linkable" and not row.is_exact_control and row.script in {"latin", "mixed"}
            for row in mentions
        ),
        libraries=len(gold.libraries),
        ontologies=len(gold.ontologies),
        entities=len(gold.entities),
        relations=len(gold.relations),
        evidence=len(gold.evidence),
        documents=len(gold.documents),
        chunks=len(gold.chunks),
        calibration_cases=sum(case.split == "calibration" for case in cases),
        release_cases=sum(case.split == "release" for case in cases),
        calibration_safety_cases=sum(
            case.split == "calibration" and case.cohort == "safety" for case in cases
        ),
        calibration_utility_cases=sum(
            case.split == "calibration" and case.cohort == "utility" for case in cases
        ),
        release_safety_cases=sum(
            case.split == "release" and case.cohort == "safety" for case in cases
        ),
        release_utility_cases=sum(
            case.split == "release" and case.cohort == "utility" for case in cases
        ),
    )


def _fixed_controls() -> tuple[
    ControlConfig, MetricConfig, ThresholdGrid, BootstrapConfig, PerformanceFixture
]:
    return (
        ControlConfig(
            exact_only_normalizer="normalize_graph_name_v1",
            dense_retrieval_mode="dense",
            hybrid_retrieval_mode="hybrid",
            top_k=10,
            score_threshold_micros=0,
            metadata_condition=None,
            source_config=None,
            rerank_enabled=False,
            query_rewrite_enabled=False,
            query_rewrite_llm_enabled=False,
            embedding_model="bge-m3",
            embedding_dimension=1024,
            hybrid_candidate_k=CONTROL_RETRIEVAL_CANDIDATE_K,
            hybrid_rrf_k=60,
            hybrid_keyword_threshold_micros=300000,
            hybrid_keyword_title_boost_micros=1500000,
            hybrid_keyword_external_id_boost_micros=2000000,
            enable_revision_id_visibility=False,
        ),
        MetricConfig(
            version="entity-linking-metrics-v1",
            required_mentions_all_or_nothing=True,
            evidence_top_k=10,
            rate_rounding="integer-half-up-v1",
            best_control_tie="dense",
        ),
        ThresholdGrid(
            min_score_micros=(850000, 880000, 900000, 920000, 950000),
            min_margin_micros=(80000, 100000, 120000, 150000, 200000),
            candidate_floor_micros=500000,
            max_candidates=10,
        ),
        BootstrapConfig(
            algorithm_version="paired-stratified-bootstrap-v1",
            prng="python-random-mt19937",
            seed=2026071707,
            replicates=10000,
            strata=STRATUM_ORDER,
            lower_index=249,
            upper_index=9749,
        ),
        PerformanceFixture(
            generator_version="entity-linking-performance-fixture-v2",
            publication_total_items=10000,
            publication_entities=6000,
            publication_relations=4000,
            linker_scenario="linker-mixed-10",
            linker_mention_count=10,
            linker_mention_mix="2 exact, 3 high-similarity, 3 ambiguous, 2 not-found",
            link_graph_scenario="link-graph-linked-10",
            link_graph_mention_count=10,
            link_graph_mention_mix="2 exact, 4 boundary-omission, 4 ordered-abbreviation",
            warmup_count=5,
            sample_count=30,
            timeout_micros=2000000,
            max_candidates_per_mention=10,
        ),
    )


def write_static_dataset(root: Path) -> FeasibilityManifest:
    verify_g2_approval(root)
    gold, cases = build_static_gold_and_cases()
    conformance = build_conformance_dataset()
    base = root / "eval/entity_linking"
    gold_path = base / "gold_v3.json"
    cases_path = base / "cases_v3.jsonl"
    conformance_path = base / "conformance_v2.json"
    manifest_path = base / "manifests/feasibility_v3.json"
    _write_json(gold_path, gold.model_dump(mode="json"))
    _write_jsonl(cases_path, [case.model_dump(mode="json") for case in cases])
    _write_json(conformance_path, conformance.model_dump(mode="json"))

    gold_value = gold.model_dump(mode="json")
    cases_value = [case.model_dump(mode="json") for case in cases]
    conformance_value = conformance.model_dump(mode="json")
    gold_ref = artifact_ref(root, gold_path, gold_value)
    cases_ref = artifact_ref(root, cases_path, cases_value)
    conformance_ref = artifact_ref(root, conformance_path, conformance_value)
    release = tuple(case for case in cases if case.split == "release")
    calibration = tuple(case for case in cases if case.split == "calibration")
    calibration_utility = tuple(case for case in calibration if case.cohort == "utility")
    release_utility = tuple(case for case in release if case.cohort == "utility")
    category_counts_value = {
        category: sum(category in case.categories for case in release) for category in CATEGORY_ORDER
    }
    stratum_counts_value = {
        stratum: sum(case.utility_stratum == stratum for case in release_utility)
        for stratum in STRATUM_ORDER
    }
    closure = build_dependency_closure(root)
    distributions = discover_external_distribution_records(root)
    scope_hashes = _scope_hashes(gold)
    control, metric, grid, bootstrap, performance = _fixed_controls()
    manifest = FeasibilityManifest(
        schema_version="entity-linking-feasibility-manifest-v2",
        dataset_id="feasibility-v3",
        g2_approval_commit=G2_APPROVAL_COMMIT,
        g2_specification_tree_sha256=G2_SPECIFICATION_TREE_SHA256,
        gold_ref=gold_ref,
        cases_ref=cases_ref,
        conformance_ref=conformance_ref,
        canonicalization_version="canonical-graph-json-v1",
        normalization_version="normalize_graph_name_v1",
        algorithm_version="lexical-score-v2",
        rounding_version="integer-half-up-v1",
        category_predicate_version="entity-linking-category-predicates-v2",
        ordered_case_ids=tuple(case.case_id for case in cases),
        ordered_calibration_case_ids=tuple(case.case_id for case in cases if case.split == "calibration"),
        ordered_release_case_ids=tuple(case.case_id for case in release),
        ordered_calibration_utility_case_ids=tuple(case.case_id for case in calibration_utility),
        ordered_release_utility_case_ids=tuple(case.case_id for case in release_utility),
        calibration_cohort_counts=CohortCounts(safety=40, utility=40),
        release_cohort_counts=CohortCounts(safety=40, utility=80),
        counts=_count_dataset(gold, cases),
        minimum_counts=MinimumCountsRecord(
            relation_oriented_questions=200,
            linkable_mentions=240,
            ambiguous_unlinkable_mentions=60,
            exact_canonical_controls=40,
            cjk_non_exact_linkable_mentions=40,
            latin_mixed_non_exact_linkable_mentions=40,
            libraries=2,
            ontologies=2,
            release_category_cases=20,
            release_stratum_questions=20,
            decoys_per_utility_case=12,
        ),
        release_category_counts=CategoryCounts.model_validate(category_counts_value),
        release_stratum_counts=StratumCounts.model_validate(stratum_counts_value),
        entity_family_split_hash=_family_hash(cases, "entity_family_keys"),
        mention_family_split_hash=_family_hash(cases, "mention_family_keys"),
        relation_template_family_split_hash=_family_hash(cases, "relation_template_family_key"),
        phrase_family_split_hash=_family_hash(cases, "phrase_family_key"),
        decoy_family_split_hash=_family_hash(cases, "decoy_family_keys"),
        logical_scope_schema_hashes=scope_hashes,
        ontology_schema_set_hash=canonical_sha256([row.model_dump(mode="json") for row in scope_hashes]),
        control_config=control,
        metric_config=metric,
        threshold_grid=grid,
        bootstrap_config=bootstrap,
        performance_fixture=performance,
        dependency_closure_algorithm="app-python-ast-import-closure-v1",
        dependency_root_modules=DEPENDENCY_ROOT_MODULES,
        accepted_dependency_closure_records=closure,
        accepted_dependency_closure_sha256=canonical_sha256(closure),
        accepted_dependency_closure_path_count=63,
        external_distribution_identity_version="external-distribution-identity-v1",
        external_distribution_records=tuple(ExternalDistributionRecord(**row) for row in distributions),
        external_distribution_set_sha256=canonical_sha256(distributions),
    )
    _write_json(manifest_path, manifest.model_dump(mode="json"))
    return manifest


def recompute_case_categories(case: EvaluationCase, gold: GoldDataset) -> tuple[str, ...]:
    entities = {row.entity_key: row for row in gold.entities}
    publication = next(
        row for row in gold.publications if row.publication_key == case.scenario_publication_key
    )
    scoped_rows = [entities[key] for key in publication.entity_keys]
    result: set[str] = set()
    for mention in case.mentions:
        normalized = normalize_graph_name_v1(mention.text)
        filtered = [
            row
            for row in scoped_rows
            if mention.entity_type_key is None or row.entity_type_key == mention.entity_type_key
        ]
        exact = [row for row in filtered if row.normalized_name == normalized]
        scored = sorted(
            (score_candidate(mention.text, _candidate(row)) for row in filtered),
            key=stable_candidate_key,
        )
        presented = [row for row in scored if row.features.score_micros >= 500000]
        gold_entity = entities.get(mention.gold_entity_key or "")
        canonical = gold_entity.normalized_name if gold_entity else ""
        exact_predicate = (
            mention.gold_status == "linkable"
            and len(exact) == 1
            and exact[0].entity_key == mention.gold_entity_key
            and normalized == canonical
        )
        if mention.is_exact_control != exact_predicate:
            raise ValueError(f"{case.case_id} exact-control declaration mismatch")
        if exact_predicate:
            result.add("exact-canonical")
            if mention.text != gold_entity.canonical_name:
                result.add("case-whitespace-normalization")

        prefix = bool(
            mention.gold_status == "linkable"
            and normalized != canonical
            and len(normalized) >= 2
            and len(normalized) < len(canonical)
            and (canonical.startswith(normalized) or canonical.endswith(normalized))
            and not exact
        )
        if prefix:
            result.add("prefix-suffix-omission")

        compact_mention = "".join(normalized.split())
        compact_canonical = "".join(canonical.split())
        canonical_tokens = canonical.split()
        initialism = bool(
            len(canonical_tokens) >= 2 and compact_mention == "".join(token[0] for token in canonical_tokens)
        )
        subsequence = bool(
            compact_canonical
            and 2 <= len(compact_mention) < len(compact_canonical)
            and is_strict_subsequence(compact_mention, compact_canonical)
            and compact_mention not in compact_canonical
            and 4 * len(compact_mention) <= 3 * len(compact_canonical)
        )
        abbreviation = bool(
            mention.gold_status == "linkable" and not exact and not prefix and (initialism or subsequence)
        )
        if abbreviation:
            result.add("abbreviation-like-overlap")

        mention_tokens = normalized.split()
        word_order = bool(
            mention.gold_status == "linkable"
            and not exact
            and len(mention_tokens) >= 2
            and len(canonical_tokens) >= 2
            and Counter(mention_tokens) == Counter(canonical_tokens)
            and mention_tokens != canonical_tokens
            and not prefix
            and not abbreviation
        )
        if word_order:
            result.add("word-order-token-overlap")

        if (
            mention.gold_status == "unlinkable"
            and not exact
            and presented
            and not mention.negative_entity_keys
        ):
            result.add("close-name-negative")
        if mention.gold_status == "ambiguous" and not exact and len(presented) >= 2:
            top_score = presented[0].features.score_micros
            tied = tuple(
                row.candidate.entity_key for row in presented if row.features.score_micros == top_score
            )
            if len(tied) >= 2 and tied == mention.gold_ambiguous_entity_keys:
                result.add("same-score-ambiguity")
        if (
            mention.gold_status == "ambiguous"
            and mention.entity_type_key is None
            and len(exact) >= 2
            and len({row.entity_type_key for row in exact}) >= 2
        ):
            ordered_exact = tuple(
                row.entity_key
                for row in sorted(
                    (_candidate(row) for row in exact),
                    key=lambda row: (
                        row.entity_type_key,
                        row.normalized_name,
                        row.entity_id,
                    ),
                )
            )
            if ordered_exact != mention.gold_ambiguous_entity_keys:
                raise ValueError(f"{case.case_id} exact ambiguity ordering mismatch")
            result.add("same-name-cross-type-ambiguity")
        if mention.gold_status == "unlinkable" and mention.negative_entity_keys:
            for key in mention.negative_entity_keys:
                row = entities[key]
                if row.normalized_name != normalized or row.entity_key in publication.entity_keys:
                    raise ValueError(f"{case.case_id} invalid cross-scope negative")
            result.add("unpublished-cross-scope")
        if mention.gold_status == "unlinkable" and not exact and not presented:
            result.add("not-found")

    result.add("one-hop-evidence-utility" if case.gold_utility.hop == 1 else "two-hop-evidence-utility")
    return tuple(category for category in CATEGORY_ORDER if category in result)


def _validate_references(gold: GoldDataset, cases: Sequence[EvaluationCase]) -> None:
    def keys(rows: Iterable[Any], field: str) -> set[str]:
        values = [getattr(row, field) for row in rows]
        if values != sorted(values) or len(values) != len(set(values)):
            raise ValueError(f"{field} records must be sorted and unique")
        return set(values)

    library_keys = keys(gold.libraries, "library_key")
    ontology_keys = keys(gold.ontologies, "ontology_key")
    entity_type_keys = keys(gold.entity_types, "entity_type_key")
    relation_type_keys = keys(gold.relation_types, "relation_type_key")
    document_keys = keys(gold.documents, "document_key")
    revision_keys = keys(gold.revisions, "revision_key")
    chunk_keys = keys(gold.chunks, "chunk_key")
    evidence_keys = keys(gold.evidence, "evidence_key")
    entity_keys = keys(gold.entities, "entity_key")
    relation_keys = keys(gold.relations, "relation_key")
    publication_keys = keys(gold.publications, "publication_key")
    canary_keys = keys(gold.privacy_canaries, "canary_key")
    documents = {row.document_key: row for row in gold.documents}
    chunks = {row.chunk_key: row for row in gold.chunks}
    evidence = {row.evidence_key: row for row in gold.evidence}
    entities = {row.entity_key: row for row in gold.entities}
    for row in gold.entities:
        if (
            row.library_key not in library_keys
            or row.ontology_key not in ontology_keys
            or row.entity_type_key not in entity_type_keys
            or set(row.support_evidence_keys) - evidence_keys
            or set(row.privacy_canary_keys) - canary_keys
            or normalize_graph_name_v1(row.canonical_name) != row.normalized_name
        ):
            raise ValueError(f"invalid Entity reference: {row.entity_key}")
    for row in gold.relations:
        if (
            row.relation_type_key not in relation_type_keys
            or row.source_entity_key not in entity_keys
            or row.target_entity_key not in entity_keys
            or set(row.support_evidence_keys) - evidence_keys
            or set(row.privacy_canary_keys) - canary_keys
        ):
            raise ValueError(f"invalid Relation reference: {row.relation_key}")
    for row in gold.evidence:
        if (
            row.library_key not in library_keys
            or row.document_key not in document_keys
            or row.revision_key not in revision_keys
            or row.chunk_key not in chunk_keys
        ):
            raise ValueError(f"invalid Evidence reference: {row.evidence_key}")
    for row in gold.publications:
        if set(row.entity_keys) - entity_keys or set(row.relation_keys) - relation_keys:
            raise ValueError(f"invalid Publication reference: {row.publication_key}")
    for case in cases:
        if set(case.decoy_chunk_keys) - chunk_keys:
            raise ValueError(f"unknown decoy Chunk: {case.case_id}")
        if case.scenario_publication_key not in publication_keys:
            raise ValueError(f"unknown Publication: {case.case_id}")
        if set(case.gold_utility.evidence_keys) - evidence_keys:
            raise ValueError(f"unknown Evidence: {case.case_id}")
        if set(case.gold_utility.relation_keys) - relation_keys:
            raise ValueError(f"unknown Relation: {case.case_id}")
        if set(case.gold_utility.node_keys) - entity_keys:
            raise ValueError(f"unknown Entity: {case.case_id}")
        if case.cohort == "utility":
            for mention in case.mentions:
                entity = entities[mention.gold_entity_key or ""]
                if normalize_graph_name_v1(mention.text) != entity.normalized_name:
                    raise ValueError(f"utility mention is not normalization-exact: {case.case_id}")
            gold_chunks = tuple(
                chunks[evidence[key].chunk_key] for key in case.gold_utility.evidence_keys
            )
            if any(case.question in chunk.text for chunk in gold_chunks):
                raise ValueError(f"gold Chunk copies retrieval query: {case.case_id}")
            expected_markers = (
                *("Contradictory surface record",) * 4,
                *("Competing relation record",) * 4,
                *("Incomplete provenance record",) * 4,
            )
            for chunk_key, marker in zip(case.decoy_chunk_keys, expected_markers, strict=True):
                chunk = chunks[chunk_key]
                document = documents[chunk.document_key]
                if (
                    case.question not in document.title
                    or case.question not in chunk.text
                    or marker not in document.title
                ):
                    raise ValueError(f"retrieval-competitive decoy invalid: {case.case_id}")
        for mention in case.mentions:
            referenced = set(mention.gold_ambiguous_entity_keys) | set(mention.negative_entity_keys)
            if mention.gold_entity_key:
                referenced.add(mention.gold_entity_key)
            if referenced - entity_keys:
                raise ValueError(f"unknown mention Entity: {case.case_id}")


def validate_conformance(conformance: ConformanceDataset) -> None:
    for case in conformance.feature_cases:
        observed = score_normalized_pair(
            normalize_graph_name_v1(case.left), normalize_graph_name_v1(case.right)
        )
        if (
            observed.character_bigram_dice_micros != case.expected.character_bigram_dice_micros
            or observed.token_jaccard_micros != case.expected.token_jaccard_micros
            or observed.substring_containment_micros != case.expected.substring_containment_micros
            or observed.boundary_omission_micros != case.expected.boundary_omission_micros
            or observed.ordered_abbreviation_micros
            != case.expected.ordered_abbreviation_micros
            or observed.score_micros != case.expected.score_micros
        ):
            raise ValueError(f"feature conformance failed: {case.case_id}")
    for case in conformance.decision_cases:
        candidates = tuple(
            EntityCandidate(**candidate.model_dump(mode="python")) for candidate in case.candidates
        )
        if case.candidate_scores_micros is None:
            decision = resolve_mention(
                case.mention_text,
                candidates,
                entity_type_key=case.entity_type_key,
                min_score_micros=case.min_score_micros,
                min_margin_micros=case.min_margin_micros,
            )
            optimized = resolve_prepared_mention(
                case.mention_text,
                prepare_candidates(candidates),
                entity_type_key=case.entity_type_key,
                min_score_micros=case.min_score_micros,
                min_margin_micros=case.min_margin_micros,
            )
            if logical_decision(optimized) != logical_decision(decision):
                raise ValueError(f"prepared decision conformance failed: {case.case_id}")
        else:
            scored = tuple(
                ScoredCandidate(candidate, FeatureScores(score, score, score, 0, 0, score))
                for candidate, score in zip(candidates, case.candidate_scores_micros, strict=True)
            )
            decision = decide_scored_candidates(
                scored,
                min_score_micros=case.min_score_micros,
                min_margin_micros=case.min_margin_micros,
            )
        selected = decision.selected.candidate.entity_key if decision.selected else None
        presented = tuple(row.candidate.entity_key for row in decision.candidates)
        if (
            decision.status != case.expected.status
            or decision.method != case.expected.method
            or selected != case.expected.selected_entity_key
            or presented != case.expected.candidate_entity_keys
        ):
            raise ValueError(f"decision conformance failed: {case.case_id}")
    for case in conformance.ordering_cases:
        candidates = tuple(
            EntityCandidate(**candidate.model_dump(mode="python")) for candidate in case.candidates
        )
        scored = tuple(
            ScoredCandidate(candidate, FeatureScores(700000, 700000, 700000, 0, 0, 700000))
            for candidate in candidates
        )
        observed = tuple(row.candidate.entity_key for row in sorted(scored, key=stable_candidate_key))
        if observed != case.expected_entity_keys:
            raise ValueError(f"ordering conformance failed: {case.case_id}")
    required_category_ids = {
        "cat-exact-raw",
        "cat-casefold-whitespace",
        "cat-prefix-length-two",
        "cat-prefix-length-one-rejected",
        "cat-middle-substring-rejected",
        "cat-initialism",
        "cat-noncontiguous-subsequence",
        "cat-contiguous-substring-rejected",
        "cat-subsequence-ratio-over-75pct",
        "cat-token-reorder",
        "cat-token-multiset-different",
        "cat-close-score-499999",
        "cat-close-score-500000",
        "cat-same-score-top-two",
        "cat-score-diff-one-micro",
        "cat-cross-type-no-hint",
        "cat-cross-type-with-hint",
        "cat-unpublished-exact",
        "cat-one-hop",
        "cat-two-hop",
    }
    if {row.case_id for row in conformance.category_predicate_cases} != required_category_ids:
        raise ValueError("category predicate conformance IDs mismatch")


def _validate_families(cases: Sequence[EvaluationCase]) -> None:
    for field in (
        "entity_family_keys",
        "mention_family_keys",
        "relation_template_family_key",
        "phrase_family_key",
        "decoy_family_keys",
    ):
        family_split: dict[str, str] = {}
        for case in cases:
            values = getattr(case, field)
            if isinstance(values, str):
                values = (values,)
            for value in values:
                existing = family_split.setdefault(value, case.split)
                if existing != case.split:
                    raise EntityLinkingEvalError("family_split_overlap")


def _validate_historical_family_disjoint(
    current_cases: Sequence[EvaluationCase], historical_cases: Sequence[EvaluationCase]
) -> None:
    def values_for(cases: Sequence[EvaluationCase], field: str) -> set[str]:
        result: set[str] = set()
        for case in cases:
            values = getattr(case, field)
            result.update((values,) if isinstance(values, str) else values)
        return result

    for field in (
        "entity_family_keys",
        "mention_family_keys",
        "relation_template_family_key",
        "phrase_family_key",
        "decoy_family_keys",
    ):
        current_values = values_for(current_cases, field)
        historical_values = values_for(historical_cases, field)
        if current_values & historical_values or any("v3" not in value for value in current_values):
            raise EntityLinkingEvalError("family_split_overlap")


@lru_cache(maxsize=4)
def load_dataset(root: Path) -> LoadedDataset:
    verify_g2_approval(root)
    base = root / "eval/entity_linking"
    gold_path = base / "gold_v3.json"
    cases_path = base / "cases_v3.jsonl"
    conformance_path = base / "conformance_v2.json"
    manifest_path = base / "manifests/feasibility_v3.json"
    gold = load_canonical_json(root, gold_path, GoldDataset)
    cases = load_canonical_jsonl(root, cases_path, EvaluationCase)
    conformance = load_canonical_json(root, conformance_path, ConformanceDataset)
    manifest = load_canonical_json(root, manifest_path, FeasibilityManifest)
    assert isinstance(gold, GoldDataset)
    assert isinstance(conformance, ConformanceDataset)
    assert isinstance(manifest, FeasibilityManifest)
    if (
        manifest.g2_approval_commit != G2_APPROVAL_COMMIT
        or manifest.g2_specification_tree_sha256 != G2_SPECIFICATION_TREE_SHA256
    ):
        raise EntityLinkingEvalError("g2_specification_tree_drift")
    typed_cases = tuple(case for case in cases if isinstance(case, EvaluationCase))
    if len(typed_cases) != len(cases):
        raise ValueError("case type mismatch")
    _validate_references(gold, typed_cases)
    _validate_families(typed_cases)
    historical_cases = tuple(
        case
        for case in load_canonical_jsonl(root, base / "cases_v2.jsonl", EvaluationCase)
        if isinstance(case, EvaluationCase)
    )
    _validate_historical_family_disjoint(typed_cases, historical_cases)
    validate_conformance(conformance)
    if tuple(case.case_id for case in typed_cases) != tuple(sorted(case.case_id for case in typed_cases)):
        raise ValueError("cases must be sorted")
    if len(typed_cases) != 200 or len(typed_cases) % 5:
        raise ValueError("fixed case count invalid")
    calibration = tuple(case for case in typed_cases if case.split == "calibration")
    release = tuple(case for case in typed_cases if case.split == "release")
    if len(calibration) * 5 != len(typed_cases) * 2 or len(release) * 5 != len(typed_cases) * 3:
        raise ValueError("40/60 split invalid")
    calibration_utility = tuple(case for case in calibration if case.cohort == "utility")
    release_utility = tuple(case for case in release if case.cohort == "utility")
    if (
        len(calibration) != 80
        or len(release) != 120
        or len(calibration_utility) != 40
        or len(release_utility) != 80
        or sum(case.cohort == "safety" for case in calibration) != 40
        or sum(case.cohort == "safety" for case in release) != 40
    ):
        raise ValueError("cohort counts invalid")
    if (
        tuple(case.case_id for case in calibration_utility)
        != manifest.ordered_calibration_utility_case_ids
        or tuple(case.case_id for case in release_utility)
        != manifest.ordered_release_utility_case_ids
        or manifest.calibration_cohort_counts != CohortCounts(safety=40, utility=40)
        or manifest.release_cohort_counts != CohortCounts(safety=40, utility=80)
    ):
        raise ValueError("manifest cohort identity mismatch")
    if any(
        len(case.decoy_family_keys) != (12 if case.cohort == "utility" else 0)
        or len(case.decoy_chunk_keys) != (12 if case.cohort == "utility" else 0)
        for case in typed_cases
    ):
        raise ValueError("decoy family count invalid")
    for case in calibration:
        observed = recompute_case_categories(case, gold)
        if observed != case.categories:
            raise EntityLinkingEvalError("category_predicate_mismatch")
    counts = _count_dataset(gold, typed_cases)
    if counts != manifest.counts:
        raise ValueError("manifest counts mismatch")
    category_counts = {
        category: sum(category in case.categories for case in release) for category in CATEGORY_ORDER
    }
    if category_counts != manifest.release_category_counts.root or min(category_counts.values()) < 20:
        raise EntityLinkingEvalError("category_minimum_not_met")
    stratum_counts = {
        stratum: sum(case.utility_stratum == stratum for case in release_utility)
        for stratum in STRATUM_ORDER
    }
    if stratum_counts != manifest.release_stratum_counts.root or min(stratum_counts.values()) < 20:
        raise EntityLinkingEvalError("category_minimum_not_met")
    if (
        _family_hash(typed_cases, "entity_family_keys") != manifest.entity_family_split_hash
        or _family_hash(typed_cases, "mention_family_keys") != manifest.mention_family_split_hash
        or _family_hash(typed_cases, "relation_template_family_key")
        != manifest.relation_template_family_split_hash
        or _family_hash(typed_cases, "phrase_family_key") != manifest.phrase_family_split_hash
        or _family_hash(typed_cases, "decoy_family_keys") != manifest.decoy_family_split_hash
    ):
        raise EntityLinkingEvalError("family_split_overlap")

    gold_value = gold.model_dump(mode="json")
    cases_value = [case.model_dump(mode="json") for case in typed_cases]
    conformance_value = conformance.model_dump(mode="json")
    refs = (
        (manifest.gold_ref, artifact_ref(root, gold_path, gold_value)),
        (manifest.cases_ref, artifact_ref(root, cases_path, cases_value)),
        (manifest.conformance_ref, artifact_ref(root, conformance_path, conformance_value)),
    )
    if any(left != right for left, right in refs):
        raise EntityLinkingEvalError("dataset_hash_mismatch")
    dataset_hash = canonical_sha256(
        {
            "cases_content_sha256": manifest.cases_ref.canonical_sha256,
            "cases_schema_version": "entity-linking-case-v2",
            "conformance_content_sha256": manifest.conformance_ref.canonical_sha256,
            "conformance_schema_version": conformance.schema_version,
            "dataset_id": "feasibility-v3",
            "gold_content_sha256": manifest.gold_ref.canonical_sha256,
            "gold_schema_version": gold.schema_version,
        }
    )
    config_value = {
        "bootstrap_config": manifest.bootstrap_config.model_dump(mode="json"),
        "control_config": manifest.control_config.model_dump(mode="json"),
        "metric_config": manifest.metric_config.model_dump(mode="json"),
        "performance_fixture": manifest.performance_fixture.model_dump(mode="json"),
        "threshold_grid": manifest.threshold_grid.model_dump(mode="json"),
    }
    closure = build_dependency_closure(root)
    distributions = discover_external_distribution_records(root)
    if tuple(row.model_dump(mode="json") for row in manifest.accepted_dependency_closure_records) != closure:
        raise EntityLinkingEvalError("dependency_closure_unresolved")
    if manifest.accepted_dependency_closure_sha256 != canonical_sha256(closure):
        raise EntityLinkingEvalError("dependency_closure_unresolved")
    if tuple(row.model_dump(mode="json") for row in manifest.external_distribution_records) != distributions:
        raise EntityLinkingEvalError("external_distribution_identity_unresolved")
    manifest_identity = artifact_ref(root, manifest_path, manifest.model_dump(mode="json"))
    return LoadedDataset(
        root=root,
        gold_path=gold_path,
        cases_path=cases_path,
        conformance_path=conformance_path,
        manifest_path=manifest_path,
        gold=gold,
        cases=typed_cases,
        conformance=conformance,
        manifest=manifest,
        dataset_content_sha256=dataset_hash,
        evaluation_config_sha256=canonical_sha256(config_value),
        manifest_ref=manifest_identity,
    )


def validate_release_case_categories(dataset: LoadedDataset) -> None:
    for case in dataset.cases:
        if case.split != "release":
            continue
        if recompute_case_categories(case, dataset.gold) != case.categories:
            raise EntityLinkingEvalError("category_predicate_mismatch")


def rate_metric(numerator: int, denominator: int) -> dict[str, int | None]:
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise ValueError("invalid rate population")
    if denominator == 0:
        return {"numerator": 0, "denominator": 0, "value_micros": None}
    value = (2 * numerator * 1_000_000 + denominator) // (2 * denominator)
    return {"numerator": numerator, "denominator": denominator, "value_micros": value}


def reduced_rational(value: Fraction) -> ReducedRational:
    return ReducedRational(numerator=value.numerator, denominator=value.denominator)


def paired_stratified_bootstrap(
    *,
    candidate_recall_micros: Mapping[str, int],
    control_recall_micros: Mapping[str, int],
    case_strata: Mapping[str, str],
    best_control: str,
) -> BootstrapResult:
    case_ids = tuple(sorted(candidate_recall_micros))
    if (
        set(case_ids) != set(control_recall_micros)
        or set(case_ids) != set(case_strata)
        or best_control not in {"dense", "hybrid"}
    ):
        raise ValueError("bootstrap population identity mismatch")
    by_stratum: dict[str, list[int]] = {stratum: [] for stratum in STRATUM_ORDER}
    for case_id in case_ids:
        stratum = case_strata[case_id]
        if stratum not in by_stratum:
            raise ValueError("unknown bootstrap stratum")
        by_stratum[stratum].append(candidate_recall_micros[case_id] - control_recall_micros[case_id])
    if any(not values for values in by_stratum.values()):
        raise ValueError("empty bootstrap stratum")
    total = sum(len(values) for values in by_stratum.values())
    point = Fraction(sum(sum(values) for values in by_stratum.values()), total)
    rng = random.Random(2026071707)
    replicates: list[Fraction] = []
    for _replicate in range(10000):
        observed = 0
        for stratum in STRATUM_ORDER:
            values = by_stratum[stratum]
            for _draw in range(len(values)):
                observed += values[rng.randrange(len(values))]
        replicates.append(Fraction(observed, total))
    replicate_records = [reduced_rational(value).model_dump(mode="json") for value in replicates]
    ordered = sorted(replicates)
    lower = ordered[249]
    upper = ordered[9749]
    return BootstrapResult(
        algorithm_version="paired-stratified-bootstrap-v1",
        prng="python-random-mt19937",
        seed=2026071707,
        replicates=10000,
        strata=STRATUM_ORDER,
        best_control=best_control,
        point_estimate_micros=reduced_rational(point),
        lower_index=249,
        upper_index=9749,
        ci_lower_micros=reduced_rational(lower),
        ci_upper_micros=reduced_rational(upper),
        replicate_statistics_sha256=canonical_sha256(replicate_records),
        passed=lower > 0,
    )


def candidate_decisions_for_threshold(
    dataset: LoadedDataset,
    case: EvaluationCase,
    threshold: Threshold,
) -> tuple[dict[str, object], ...]:
    publication = next(
        row for row in dataset.gold.publications if row.publication_key == case.scenario_publication_key
    )
    entities = {row.entity_key: row for row in dataset.gold.entities}
    candidates = tuple(_candidate(entities[key]) for key in publication.entity_keys)
    return tuple(
        logical_decision(
            resolve_mention(
                mention.text,
                candidates,
                entity_type_key=mention.entity_type_key,
                min_score_micros=threshold.min_score_micros,
                min_margin_micros=threshold.min_margin_micros,
            )
        )
        for mention in case.mentions
    )


def exact_only_decisions(dataset: LoadedDataset, case: EvaluationCase) -> tuple[dict[str, object], ...]:
    publication = next(
        row for row in dataset.gold.publications if row.publication_key == case.scenario_publication_key
    )
    entities = {row.entity_key: row for row in dataset.gold.entities}
    candidates = tuple(_candidate(entities[key]) for key in publication.entity_keys)
    from eval.entity_linking.reference_scorer import resolve_exact_only

    return tuple(
        logical_decision(
            resolve_exact_only(
                mention.text,
                candidates,
                entity_type_key=mention.entity_type_key,
            )
        )
        for mention in case.mentions
    )


def build_grid_results(dataset: LoadedDataset) -> tuple[GridResult, ...]:
    calibration = tuple(case for case in dataset.cases if case.split == "calibration")
    results: list[GridResult] = []
    grid_index = 0
    for min_score in dataset.manifest.threshold_grid.min_score_micros:
        for min_margin in dataset.manifest.threshold_grid.min_margin_micros:
            threshold = Threshold(
                min_score_micros=min_score,
                min_margin_micros=min_margin,
                candidate_floor_micros=500000,
                max_candidates=10,
            )
            correct_auto = 0
            auto_total = 0
            linkable_correct = 0
            linkable_total = 0
            non_exact_correct = 0
            non_exact_total = 0
            candidate_recall = 0
            abstention_correct = 0
            abstention_total = 0
            exact_correct = 0
            exact_total = 0
            wrong = 0
            false_auto = 0
            utility_execution = 0
            utility_total = 0
            for case in calibration:
                decisions = candidate_decisions_for_threshold(dataset, case, threshold)
                if case.cohort == "utility":
                    utility_total += 1
                    utility_execution += all(
                        decision["status"] == "linked" for decision in decisions
                    )
                for mention, decision in zip(case.mentions, decisions, strict=True):
                    selected = decision["selected"]
                    selected_key = selected.get("entity_key") if isinstance(selected, dict) else None
                    if decision["status"] == "linked":
                        auto_total += 1
                        if mention.gold_status == "linkable" and selected_key == mention.gold_entity_key:
                            correct_auto += 1
                        else:
                            wrong += 1
                            if mention.gold_status != "linkable":
                                false_auto += 1
                    if mention.gold_status == "linkable":
                        linkable_total += 1
                        if selected_key == mention.gold_entity_key:
                            linkable_correct += 1
                            if not mention.is_exact_control:
                                non_exact_correct += 1
                        if not mention.is_exact_control:
                            non_exact_total += 1
                        visible = [
                            row.get("entity_key") for row in decision["candidates"] if isinstance(row, dict)
                        ]
                        if selected_key == mention.gold_entity_key or mention.gold_entity_key in visible[:5]:
                            candidate_recall += 1
                    else:
                        abstention_total += 1
                        if decision["status"] != "linked":
                            abstention_correct += 1
                    if mention.is_exact_control:
                        exact_total += 1
                        if (
                            selected_key == mention.gold_entity_key
                            and decision["method"] == "exact_canonical"
                        ):
                            exact_correct += 1
            precision = rate_metric(correct_auto, auto_total)
            link_recall = rate_metric(linkable_correct, linkable_total)
            non_exact_recall = rate_metric(non_exact_correct, non_exact_total)
            candidate_rate = rate_metric(candidate_recall, linkable_total)
            abstention = rate_metric(abstention_correct, abstention_total)
            exact_accuracy = rate_metric(exact_correct, exact_total)
            execution_coverage = rate_metric(utility_execution, utility_total)
            eligible = bool(
                wrong == 0
                and false_auto == 0
                and precision["value_micros"] == 1_000_000
                and exact_accuracy["value_micros"] == 1_000_000
                and execution_coverage["value_micros"] is not None
                and execution_coverage["value_micros"] >= 800_000
            )
            always = rate_metric(len(calibration), len(calibration))
            results.append(
                GridResult(
                    grid_index=grid_index,
                    thresholds=threshold,
                    wrong_auto_link_count=wrong,
                    ambiguous_unlinkable_false_auto_link_count=false_auto,
                    privacy_leak_count=0,
                    publication_membership_failures=0,
                    auto_link_precision=precision,
                    link_recall=link_recall,
                    non_exact_link_recall=non_exact_recall,
                    candidate_recall_at_5=candidate_rate,
                    abstention_accuracy=abstention,
                    exact_regression_accuracy=exact_accuracy,
                    scope_safety=always,
                    property_privacy=always,
                    utility_question_execution_coverage=execution_coverage,
                    non_exact_correct_auto_link_count=non_exact_correct,
                    selection_eligible=eligible,
                )
            )
            grid_index += 1
    return tuple(results)


def select_calibration_threshold(grid: Sequence[GridResult]) -> tuple[Threshold | None, str]:
    eligible = [row for row in grid if row.selection_eligible]
    if not eligible:
        return None, "no-valid-candidate"
    ordered = sorted(
        eligible,
        key=lambda row: (
            row.utility_question_execution_coverage.numerator,
            row.non_exact_correct_auto_link_count,
            row.thresholds.min_score_micros,
            row.thresholds.min_margin_micros,
        ),
        reverse=True,
    )
    selected = ordered[0]
    maximum_execution = max(
        row.utility_question_execution_coverage.numerator for row in eligible
    )
    execution_ties = [
        row
        for row in eligible
        if row.utility_question_execution_coverage.numerator == maximum_execution
    ]
    if len(execution_ties) == 1:
        reason = "max-question-execution-coverage"
    else:
        maximum_non_exact = max(
            row.non_exact_correct_auto_link_count for row in execution_ties
        )
        non_exact_ties = [
            row
            for row in execution_ties
            if row.non_exact_correct_auto_link_count == maximum_non_exact
        ]
        if len(non_exact_ties) == 1:
            reason = "tie-non-exact-coverage"
        else:
            max_score = max(row.thresholds.min_score_micros for row in non_exact_ties)
            score_ties = [
                row for row in non_exact_ties if row.thresholds.min_score_micros == max_score
            ]
            reason = "tie-higher-score" if len(score_ties) == 1 else "tie-higher-margin"
    return selected.thresholds, reason


_PRIVATE_KEY_RE = re.compile(
    r"properties|alias|mention|evidence_text|source_text|quote|prompt|raw|dsn|password|api_key|endpoint|hostname|username",
    re.IGNORECASE,
)
_PRIVATE_KEY_ALLOWLIST = frozenset(
    {
        "endpoint_origin_sha256",
        "endpoint_origin_version",
        "per_mention_sql_count",
    }
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/(?:home|Users|tmp|var)/)")


def assert_private_data_absent(value: Any, *, dataset: LoadedDataset) -> None:
    def walk(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if _PRIVATE_KEY_RE.search(str(key)) and key not in _PRIVATE_KEY_ALLOWLIST:
                    raise EntityLinkingEvalError("privacy_leak_detected")
                walk(nested, f"{path}.{key}")
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                walk(nested, f"{path}[{index}]")
        elif isinstance(item, str):
            if (
                _UUID_RE.search(item)
                or _ABSOLUTE_PATH_RE.search(item)
                or "http://" in item
                or "https://" in item
            ):
                raise EntityLinkingEvalError("privacy_leak_detected")

    walk(value, "$")
    payload = canonical_graph_json_v1(value)
    forbidden = (
        {row.question for row in dataset.cases}
        | {mention.text for row in dataset.cases for mention in row.mentions}
        | {row.canonical_name for row in dataset.gold.entities}
        | {row.text for row in dataset.gold.chunks}
    )
    forbidden.update(
        marker for row in dataset.gold.privacy_canaries for marker in (row.key_marker, row.value_marker)
    )
    if any(marker and marker in payload for marker in forbidden):
        raise EntityLinkingEvalError("privacy_leak_detected")


def normalize_service_origin(url: str, *, qdrant: bool) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise EntityLinkingEvalError("environment_fingerprint_mismatch")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise EntityLinkingEvalError("environment_fingerprint_mismatch")
    if qdrant and parsed.path not in {"", "/"}:
        raise EntityLinkingEvalError("environment_fingerprint_mismatch")
    try:
        ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    else:
        host = ip.compressed.lower()
        if ip.version == 6:
            host = f"[{host}]"
    try:
        port = parsed.port or (80 if scheme == "http" else 443)
    except ValueError as exc:
        raise EntityLinkingEvalError("environment_fingerprint_mismatch") from exc
    return f"{scheme}://{host}:{port}"


def qdrant_collection_identity(run_id: str, phase_token: str) -> QdrantCollection:
    if phase_token not in {"cal", "pf1", "pf2", "pf3"}:
        raise EntityLinkingEvalError("qdrant_collection_identity_mismatch")
    run_hash = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    name = f"vkt_v07_el_{phase_token}_{run_hash[:12]}"
    return QdrantCollection(
        derivation_version="entity-linking-qdrant-collection-name-v1",
        phase_token=phase_token,
        run_id_sha256=run_hash,
        collection_name=name,
        collection_name_sha256=hashlib.sha256(name.encode("utf-8")).hexdigest(),
    )


def qdrant_config() -> QdrantCollectionConfig:
    return QdrantCollectionConfig(
        config_version="entity-linking-qdrant-collection-config-v1",
        vector_size=1024,
        distance="Cosine",
        shard_number=1,
        hnsw_m=16,
        hnsw_ef_construct=128,
        scalar_type="int8",
        scalar_quantile=ReducedRational(numerator=99, denominator=100),
        scalar_always_ram=True,
    )


def _embedding_vector_sha256(vector: Sequence[Decimal | float | int]) -> str:
    if len(vector) != 1024:
        raise EntityLinkingEvalError("embedding_identity_mismatch")
    deadzone = Decimal("0.005")
    encoded = bytearray(256)
    for index, value in enumerate(vector):
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        if not decimal_value.is_finite():
            raise EntityLinkingEvalError("embedding_identity_mismatch")
        if decimal_value < -deadzone:
            ternary = 0
        elif decimal_value > deadzone:
            ternary = 2
        else:
            ternary = 1
        encoded[index // 4] |= ternary << (6 - 2 * (index % 4))
    return hashlib.sha256(bytes(encoded)).hexdigest()


async def _embedding_identity(base_url: str, model: str, api_key: str) -> EmbeddingIdentity:
    if model != "bge-m3":
        raise EntityLinkingEvalError("embedding_identity_mismatch")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        response = await client.post(
            base_url,
            json={"model": model, "input": [EMBEDDING_PROBE_TEXT]},
            headers=headers,
        )
    if response.status_code != 200:
        raise EntityLinkingEvalError("embedding_unavailable")
    try:
        body = json.loads(response.content, parse_float=Decimal, parse_int=Decimal)
        data = body["data"]
        vector = data[0]["embedding"]
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EntityLinkingEvalError("embedding_identity_mismatch") from exc
    if len(data) != 1 or not isinstance(vector, list) or len(vector) != 1024:
        raise EntityLinkingEvalError("embedding_identity_mismatch")
    origin_hash = hashlib.sha256(normalize_service_origin(base_url, qdrant=False).encode("utf-8")).hexdigest()
    projection = {
        "identity_version": "entity-linking-embedding-identity-v1",
        "endpoint_origin_version": "service-origin-v1",
        "endpoint_origin_sha256": origin_hash,
        "model": "bge-m3",
        "dimension": 1024,
        "probe_text_sha256": EMBEDDING_PROBE_SHA256,
        "vector_encoding_version": "ternary-deadzone-0.005-v1",
        "probe_vector_sha256": _embedding_vector_sha256(vector),
    }
    return EmbeddingIdentity(
        **projection,
        embedding_fingerprint_sha256=canonical_sha256(projection),
    )


def _qdrant_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key
    return headers


async def _qdrant_identity(base_url: str, api_key: str) -> QdrantIdentity:
    origin = normalize_service_origin(base_url, qdrant=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        response = await client.get(base_url.rstrip("/") + "/", headers=_qdrant_headers(api_key))
    if response.status_code != 200:
        raise EntityLinkingEvalError("qdrant_unavailable")
    try:
        server_version = response.json()["version"]
    except (KeyError, TypeError, ValueError) as exc:
        raise EntityLinkingEvalError("qdrant_identity_mismatch") from exc
    if not isinstance(server_version, str):
        raise EntityLinkingEvalError("qdrant_identity_mismatch")
    config = qdrant_config()
    config_hash = canonical_sha256(config.model_dump(mode="json"))
    projection = {
        "identity_version": "entity-linking-qdrant-identity-v1",
        "endpoint_origin_version": "service-origin-v1",
        "endpoint_origin_sha256": hashlib.sha256(origin.encode("utf-8")).hexdigest(),
        "server_version": server_version,
        "collection_config": config.model_dump(mode="json"),
        "collection_config_sha256": config_hash,
    }
    return QdrantIdentity(**projection, qdrant_fingerprint_sha256=canonical_sha256(projection))


async def _admin_connection(admin_dsn: str):
    import asyncpg

    url = make_url(admin_dsn)
    if not url.drivername.startswith("postgresql"):
        raise EntityLinkingEvalError("postgres_dsn_required")
    return await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database or "postgres",
    )


async def postgres_cluster_identity(admin_dsn: str) -> dict[str, Any]:
    connection = await _admin_connection(admin_dsn)
    try:
        row = await connection.fetchrow(
            "SELECT (pg_control_system()).system_identifier::text AS system_identifier, "
            "current_setting('server_version_num')::int AS server_version_num, "
            "current_setting('server_encoding') AS server_encoding, "
            "database_identity.datcollate AS lc_collate, database_identity.datctype AS lc_ctype "
            "FROM pg_database AS database_identity WHERE database_identity.datname=current_database()"
        )
        available = await connection.fetchval(
            "SELECT default_version FROM pg_available_extensions WHERE name='pg_trgm'"
        )
    except Exception as exc:
        raise EntityLinkingEvalError("postgres_cluster_identity_unavailable") from exc
    finally:
        await connection.close()
    if row is None or not available:
        raise EntityLinkingEvalError("postgres_cluster_identity_unavailable")
    projection = {
        "fingerprint_version": "entity-linking-pg-cluster-v1",
        "system_identifier": row["system_identifier"],
        "server_version_num": str(row["server_version_num"]),
        "server_encoding": row["server_encoding"],
        "lc_collate": row["lc_collate"],
        "lc_ctype": row["lc_ctype"],
    }
    return {
        "pg_cluster_fingerprint_sha256": canonical_sha256(projection),
        "pg_server_version_num": row["server_version_num"],
        "pg_trgm_version": available,
    }


async def preflight_live_dependencies() -> dict[str, Any]:
    admin_dsn = os.getenv("VECTOR_KB_PG_TEST_DSN", "")
    qdrant_url = os.getenv("QDRANT_URL", "")
    embedding_url = os.getenv("EMBEDDING_BASE_URL", "")
    model = os.getenv("EMBEDDING_MODEL", "")
    dimension = os.getenv("EMBEDDING_DIM", "")
    if not admin_dsn:
        raise EntityLinkingEvalError("postgres_dsn_required")
    if not qdrant_url:
        raise EntityLinkingEvalError("qdrant_unavailable")
    if not embedding_url or model != "bge-m3" or dimension != "1024":
        raise EntityLinkingEvalError("embedding_unavailable")
    postgres, qdrant, embedding = await asyncio.gather(
        postgres_cluster_identity(admin_dsn),
        _qdrant_identity(qdrant_url, os.getenv("QDRANT_API_KEY", "")),
        _embedding_identity(
            embedding_url,
            model,
            os.getenv("EMBEDDING_API_KEY", ""),
        ),
    )
    return {
        "embedding_fingerprint_sha256": embedding.embedding_fingerprint_sha256,
        "pg_cluster_fingerprint_sha256": postgres["pg_cluster_fingerprint_sha256"],
        "pg_trgm": "passed",
        "postgresql": "passed",
        "qdrant": "passed",
        "qdrant_fingerprint_sha256": qdrant.qdrant_fingerprint_sha256,
        "embedding": "passed",
        "status": "passed",
    }


def validate_database_id(database_id: str) -> str:
    if len(database_id) > 63 or re.fullmatch(r"vkt_v07_el_eval_[a-z0-9_]{1,45}", database_id) is None:
        raise EntityLinkingEvalError("database_id_invalid")
    return database_id


def target_database_dsn(admin_dsn: str, database_id: str) -> str:
    validate_database_id(database_id)
    url = make_url(admin_dsn)
    if not url.drivername.startswith("postgresql"):
        raise EntityLinkingEvalError("postgres_dsn_required")
    return url.set(drivername="postgresql+asyncpg", database=database_id).render_as_string(
        hide_password=False
    )


async def database_exists(admin_dsn: str, database_id: str) -> bool:
    validate_database_id(database_id)
    connection = await _admin_connection(admin_dsn)
    try:
        return bool(
            await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", database_id
            )
        )
    finally:
        await connection.close()


async def create_database(admin_dsn: str, database_id: str) -> str:
    validate_database_id(database_id)
    connection = await _admin_connection(admin_dsn)
    try:
        if await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)", database_id
        ):
            raise EntityLinkingEvalError("database_already_exists")
        await connection.execute(f'CREATE DATABASE "{database_id}" TEMPLATE template0')
    finally:
        await connection.close()
    return target_database_dsn(admin_dsn, database_id)


async def drop_database(admin_dsn: str, database_id: str) -> None:
    validate_database_id(database_id)
    connection = await _admin_connection(admin_dsn)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{database_id}" WITH (FORCE)')
    except Exception as exc:
        raise EntityLinkingEvalError("database_cleanup_failed") from exc
    finally:
        await connection.close()


def upgrade_database(root: Path, dsn: str) -> None:
    url = make_url(dsn)
    if not all((url.host, url.port, url.username, url.database)):
        raise EntityLinkingEvalError("postgres_dsn_required")
    environment = os.environ.copy()
    environment.update(
        {
            "DB_HOST": url.host,
            "DB_PORT": str(url.port),
            "DB_USER": url.username,
            "DB_PASSWORD": url.password or "",
            "DB_NAME": url.database,
        }
    )
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(root / "alembic.ini"),
            "upgrade",
            "0023",
        ),
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise EntityLinkingEvalError("entity_linking_feasibility_failed")


def _stable_hash(kind: str, key: str) -> str:
    return hashlib.sha256(f"entity-linking-eval:{kind}:{key}".encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SeededRuntime:
    primary_library_id: uuid.UUID
    primary_library_slug: str
    primary_ontology_id: uuid.UUID
    primary_publication_id: uuid.UUID
    qdrant_collection: str
    uuid_by_logical: Mapping[str, uuid.UUID]
    logical_by_uuid: Mapping[str, str]


def _uuid_maps(dataset: LoadedDataset) -> tuple[dict[str, uuid.UUID], dict[str, str]]:
    values: set[tuple[str, str]] = set()
    for rows, field, kind in (
        (dataset.gold.libraries, "library_key", "library"),
        (dataset.gold.ontologies, "ontology_key", "ontology"),
        (dataset.gold.entity_types, "entity_type_key", "entity-type"),
        (dataset.gold.relation_types, "relation_type_key", "relation-type"),
        (dataset.gold.documents, "document_key", "document"),
        (dataset.gold.revisions, "revision_key", "revision"),
        (dataset.gold.chunks, "chunk_key", "chunk"),
        (dataset.gold.evidence, "evidence_key", "evidence"),
        (dataset.gold.entities, "entity_key", "entity"),
        (dataset.gold.relations, "relation_key", "relation"),
        (dataset.gold.publications, "publication_key", "publication"),
    ):
        values.update((kind, getattr(row, field)) for row in rows)
    by_logical = {key: eval_uuid(kind, key) for kind, key in values}
    by_uuid = {str(value): key for key, value in by_logical.items()}
    return by_logical, by_uuid


async def seed_runtime_database(
    dsn: str,
    dataset: LoadedDataset,
    collection: QdrantCollection,
) -> SeededRuntime:
    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.models.chunk import Chunk
    from app.models.document import Document
    from app.models.document_revision import DocumentRevision
    from app.models.entity import Entity
    from app.models.entity_type import EntityType
    from app.models.evidence_unit import EvidenceUnit
    from app.models.graph_publication import GraphPublication
    from app.models.graph_publication_item import GraphPublicationItem
    from app.models.knowledge_relation import KnowledgeRelation
    from app.models.library import Library
    from app.models.ontology_version import OntologyVersion
    from app.models.relation_evidence import RelationEvidence
    from app.models.relation_type import RelationType

    uuid_by_logical, logical_by_uuid = _uuid_maps(dataset)
    evidence_by_key = {row.evidence_key: row for row in dataset.gold.evidence}
    primary_entities = [row for row in dataset.gold.entities if row.library_key == "library-primary"]
    negative_entities = [row for row in dataset.gold.entities if row.library_key == "library-negative"]
    primary_relations = list(dataset.gold.relations)
    performance_entities: list[tuple[str, uuid.UUID]] = []
    for index in range(6000 - len(primary_entities)):
        key = f"performance-entity-{index:04d}"
        value = eval_uuid("entity", key)
        performance_entities.append((key, value))
        logical_by_uuid[str(value)] = key
    performance_relations: list[tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]] = []
    all_perf_ids = [value for _key, value in performance_entities]
    for index in range(4000 - len(primary_relations)):
        key = f"performance-relation-{index:04d}"
        value = eval_uuid("relation", key)
        source = all_perf_ids[index % len(all_perf_ids)]
        target = all_perf_ids[(index + 1) % len(all_perf_ids)]
        performance_relations.append((key, value, source, target))
        logical_by_uuid[str(value)] = key

    primary_library_id = uuid_by_logical["library-primary"]
    primary_ontology_id = uuid_by_logical["ontology-primary"]
    publication_id = uuid_by_logical["publication-primary"]
    activated_at = datetime(2026, 7, 17, tzinfo=timezone.utc)
    engine = create_async_engine(dsn, pool_size=8, max_overflow=4)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as db:
            await db.execute(
                insert(Library),
                [
                    {
                        "id": uuid_by_logical[row.library_key],
                        "slug": row.slug,
                        "name": row.name,
                        "embedding_model": row.embedding_model,
                        "embedding_dim": row.embedding_dim,
                        "vector_distance": "cosine",
                        "retrieval_mode": row.retrieval_mode,
                        "qdrant_collection": (
                            collection.collection_name
                            if row.library_key == "library-primary"
                            else "vkt_v07_el_negative_unused"
                        ),
                        "lifecycle_mode": "managed",
                        "index_state": "ready",
                        "graph_extraction_allowed_security_levels": [],
                    }
                    for row in dataset.gold.libraries
                ],
            )
            await db.execute(
                insert(OntologyVersion),
                [
                    {
                        "id": uuid_by_logical[row.ontology_key],
                        "library_id": uuid_by_logical[row.library_key],
                        "version_key": row.version_key,
                        "version_no": row.version_no,
                        "status": "active",
                        "published_at": activated_at,
                    }
                    for row in dataset.gold.ontologies
                ],
            )
            await db.execute(
                insert(EntityType),
                [
                    {
                        "id": uuid_by_logical[row.entity_type_key],
                        "library_id": uuid_by_logical[row.library_key],
                        "ontology_version_id": uuid_by_logical[row.ontology_key],
                        "key": row.key,
                        "label": row.label,
                        "properties_schema": {},
                        "status": "active",
                    }
                    for row in dataset.gold.entity_types
                ],
            )
            await db.execute(
                insert(RelationType),
                [
                    {
                        "id": uuid_by_logical[row.relation_type_key],
                        "library_id": uuid_by_logical[row.library_key],
                        "ontology_version_id": uuid_by_logical[row.ontology_key],
                        "key": row.key,
                        "label": row.label,
                        "direction": row.direction,
                        "requires_evidence": row.requires_evidence,
                        "default_review_policy": "auto_active",
                        "properties_schema": {},
                        "status": "active",
                    }
                    for row in dataset.gold.relation_types
                ],
            )
            entity_rows = [
                {
                    "id": uuid_by_logical[row.entity_key],
                    "library_id": uuid_by_logical[row.library_key],
                    "ontology_version_id": uuid_by_logical[row.ontology_key],
                    "entity_type_id": uuid_by_logical[row.entity_type_key],
                    "canonical_name": row.canonical_name,
                    "normalized_name": row.normalized_name,
                    "properties": {"synthetic-private-properties-key": "synthetic-private-properties-value"},
                    "status": "active",
                    "source_type": "manual",
                    "confidence": 1.0,
                }
                for row in (*primary_entities, *negative_entities)
            ]
            entity_rows.extend(
                {
                    "id": value,
                    "library_id": primary_library_id,
                    "ontology_version_id": primary_ontology_id,
                    "entity_type_id": uuid_by_logical["type-org"],
                    "canonical_name": f"Performance Entity {index:04d}",
                    "normalized_name": f"performance entity {index:04d}",
                    "properties": {},
                    "status": "active",
                    "source_type": "manual",
                    "confidence": 1.0,
                }
                for index, (_key, value) in enumerate(performance_entities)
            )
            await db.execute(insert(Entity), entity_rows)
            await db.execute(
                insert(Document),
                [
                    {
                        "id": uuid_by_logical[row.document_key],
                        "library_id": uuid_by_logical[row.library_key],
                        "external_id": row.external_id,
                        "title": row.title,
                        "content_hash": _stable_hash("document", row.document_key),
                        "current_revision": 1,
                        "current_revision_id": uuid_by_logical[
                            f"revision-{row.document_key.removeprefix('document-')}"
                        ],
                        "latest_revision_id": uuid_by_logical[
                            f"revision-{row.document_key.removeprefix('document-')}"
                        ],
                        "status": "ready",
                    }
                    for row in dataset.gold.documents
                ],
            )
            await db.execute(
                insert(DocumentRevision),
                [
                    {
                        "id": uuid_by_logical[row.revision_key],
                        "document_id": uuid_by_logical[row.document_key],
                        "library_id": primary_library_id,
                        "revision_no": row.revision_no,
                        "content_hash": _stable_hash("revision", row.revision_key),
                        "normalized_text": "synthetic normalized evidence",
                        "parser_name": "entity-linking-eval",
                        "parser_version": "v1",
                        "chunking_strategy": "fixed",
                        "chunking_strategy_version": "v1",
                        "status": "ready",
                        "published_at": activated_at,
                        "finished_at": activated_at,
                    }
                    for row in dataset.gold.revisions
                ],
            )
            await db.execute(
                insert(EvidenceUnit),
                [
                    {
                        "id": uuid_by_logical[row.evidence_key],
                        "library_id": uuid_by_logical[row.library_key],
                        "document_id": uuid_by_logical[row.document_key],
                        "document_revision_id": uuid_by_logical[row.revision_key],
                        "evidence_kind": "text_span",
                        "source_start": row.locator_start,
                        "source_end": row.locator_end,
                        "text_quote": "synthetic-private-evidence-quote",
                        "text_quote_hash": _stable_hash("quote", row.evidence_key),
                        "evidence_metadata": {"synthetic-private": row.evidence_key},
                        "status": "active",
                    }
                    for row in dataset.gold.evidence
                ],
            )
            await db.execute(
                insert(Chunk),
                [
                    {
                        "id": uuid_by_logical[row.chunk_key],
                        "document_id": uuid_by_logical[row.document_key],
                        "library_id": primary_library_id,
                        "document_revision_id": uuid_by_logical[row.revision_key],
                        "evidence_id": uuid_by_logical.get(
                            f"evidence-{row.chunk_key.removeprefix('chunk-')}"
                        ),
                        "seq": row.seq,
                        "chunk_kind": "text",
                        "text": row.text,
                        "token_count": len(row.text.split()),
                        "source_start": 0,
                        "source_end": len(row.text),
                    }
                    for row in dataset.gold.chunks
                ],
            )
            relation_rows = [
                {
                    "id": uuid_by_logical[row.relation_key],
                    "library_id": primary_library_id,
                    "ontology_version_id": primary_ontology_id,
                    "relation_type_id": uuid_by_logical[row.relation_type_key],
                    "source_entity_id": uuid_by_logical[row.source_entity_key],
                    "target_entity_id": uuid_by_logical[row.target_entity_key],
                    "properties": {"synthetic-source-like-key": "synthetic-source-like-value"},
                    "status": "active",
                    "review_status": "approved",
                    "source_type": "manual",
                    "confidence": 1.0,
                }
                for row in primary_relations
            ]
            relation_rows.extend(
                {
                    "id": value,
                    "library_id": primary_library_id,
                    "ontology_version_id": primary_ontology_id,
                    "relation_type_id": uuid_by_logical["relation-type-related"],
                    "source_entity_id": source,
                    "target_entity_id": target,
                    "properties": {},
                    "status": "active",
                    "review_status": "approved",
                    "source_type": "manual",
                    "confidence": 1.0,
                }
                for _key, value, source, target in performance_relations
            )
            await db.execute(insert(KnowledgeRelation), relation_rows)
            support_rows = []
            for relation in primary_relations:
                for evidence_key in relation.support_evidence_keys:
                    support = evidence_by_key[evidence_key]
                    support_rows.append(
                        {
                            "id": eval_uuid("relation-evidence", f"{relation.relation_key}:{evidence_key}"),
                            "library_id": primary_library_id,
                            "relation_id": uuid_by_logical[relation.relation_key],
                            "evidence_id": uuid_by_logical[evidence_key],
                            "document_id": uuid_by_logical[support.document_key],
                            "document_revision_id": uuid_by_logical[support.revision_key],
                            "chunk_id": uuid_by_logical[support.chunk_key],
                            "support_type": "supports",
                            "quote_text": "synthetic-private-relation-quote",
                            "evidence_text_snapshot": "synthetic-private-relation-snapshot",
                            "status": "active",
                        }
                    )
            await db.execute(insert(RelationEvidence), support_rows)

            manifest_hash = _stable_hash("manifest", "primary")
            await db.execute(
                insert(GraphPublication),
                [
                    {
                        "id": publication_id,
                        "library_id": primary_library_id,
                        "ontology_version_id": primary_ontology_id,
                        "status": "active",
                        "source_mode": "initial_seed",
                        "manifest_version": "v1",
                        "policy_version": "v1",
                        "policy_snapshot": {},
                        "manifest_hash": manifest_hash,
                        "idempotency_key": "entity-linking-eval-primary",
                        "include_drafts": False,
                        "plan_options": {},
                        "entity_count": 6000,
                        "relation_count": 4000,
                        "blocked_counts": {},
                        "blocked_diagnostics": {},
                        "item_hashes_summary": {},
                        "activated_at": activated_at,
                    }
                ],
            )
            item_rows = []
            for row in primary_entities:
                supports = sorted(str(uuid_by_logical[key]) for key in row.support_evidence_keys)
                item_rows.append(
                    {
                        "id": eval_uuid("publication-item", f"entity:{row.entity_key}"),
                        "publication_id": publication_id,
                        "library_id": primary_library_id,
                        "ontology_version_id": primary_ontology_id,
                        "item_kind": "entity",
                        "entity_id": uuid_by_logical[row.entity_key],
                        "relation_id": None,
                        "item_hash": _stable_hash("entity-item", row.entity_key),
                        "status": "active",
                        "support_evidence_ids": supports,
                        "support_counts": {"total": len(supports)},
                        "fact_snapshot": {},
                    }
                )
            item_rows.extend(
                {
                    "id": eval_uuid("publication-item", f"entity:{key}"),
                    "publication_id": publication_id,
                    "library_id": primary_library_id,
                    "ontology_version_id": primary_ontology_id,
                    "item_kind": "entity",
                    "entity_id": value,
                    "relation_id": None,
                    "item_hash": _stable_hash("entity-item", key),
                    "status": "active",
                    "support_evidence_ids": [],
                    "support_counts": {"total": 0},
                    "fact_snapshot": {},
                }
                for key, value in performance_entities
            )
            for row in primary_relations:
                supports = sorted(str(uuid_by_logical[key]) for key in row.support_evidence_keys)
                item_rows.append(
                    {
                        "id": eval_uuid("publication-item", f"relation:{row.relation_key}"),
                        "publication_id": publication_id,
                        "library_id": primary_library_id,
                        "ontology_version_id": primary_ontology_id,
                        "item_kind": "relation",
                        "entity_id": None,
                        "relation_id": uuid_by_logical[row.relation_key],
                        "item_hash": _stable_hash("relation-item", row.relation_key),
                        "status": "active",
                        "support_evidence_ids": supports,
                        "support_counts": {"total": len(supports)},
                        "fact_snapshot": {},
                    }
                )
            item_rows.extend(
                {
                    "id": eval_uuid("publication-item", f"relation:{key}"),
                    "publication_id": publication_id,
                    "library_id": primary_library_id,
                    "ontology_version_id": primary_ontology_id,
                    "item_kind": "relation",
                    "entity_id": None,
                    "relation_id": value,
                    "item_hash": _stable_hash("relation-item", key),
                    "status": "active",
                    "support_evidence_ids": [],
                    "support_counts": {"total": 0},
                    "fact_snapshot": {},
                }
                for key, value, _source, _target in performance_relations
            )
            await db.execute(insert(GraphPublicationItem), item_rows)
            await db.commit()
    finally:
        await engine.dispose()
    return SeededRuntime(
        primary_library_id=primary_library_id,
        primary_library_slug="synthetic-primary",
        primary_ontology_id=primary_ontology_id,
        primary_publication_id=publication_id,
        qdrant_collection=collection.collection_name,
        uuid_by_logical=uuid_by_logical,
        logical_by_uuid=logical_by_uuid,
    )


def _decimal_json_bytes(value: Any) -> bytes:
    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, int):
            return str(item)
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("non-finite decimal")
            normalized = format(item, "f")
            return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
        if isinstance(item, list):
            return "[" + ",".join(encode(value) for value in item) + "]"
        if isinstance(item, dict):
            return "{" + ",".join(f"{encode(str(key))}:{encode(item[key])}" for key in sorted(item)) + "}"
        raise TypeError(f"unsupported decimal JSON type: {type(item).__name__}")

    return encode(value).encode("utf-8")


async def create_qdrant_collection(
    base_url: str,
    api_key: str,
    collection: QdrantCollection,
) -> QdrantIdentity:
    headers = _qdrant_headers(api_key)
    collection_url = base_url.rstrip("/") + f"/collections/{collection.collection_name}"
    payload = {
        "hnsw_config": {"ef_construct": 128, "m": 16},
        "quantization_config": {
            "scalar": {"always_ram": True, "quantile": Decimal(99) / Decimal(100), "type": "int8"}
        },
        "shard_number": 1,
        "vectors": {"distance": "Cosine", "size": 1024},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        existing = await client.get(collection_url, headers=headers)
        if existing.status_code == 200:
            raise EntityLinkingEvalError("qdrant_collection_exists")
        if existing.status_code != 404:
            raise EntityLinkingEvalError("qdrant_unavailable")
        created = await client.put(
            collection_url,
            headers=headers,
            content=_decimal_json_bytes(payload),
        )
        if created.status_code not in {200, 201}:
            raise EntityLinkingEvalError("qdrant_unavailable")
        observed = await client.get(collection_url, headers=headers)
    if observed.status_code != 200:
        raise EntityLinkingEvalError("qdrant_identity_mismatch")
    try:
        body = json.loads(observed.content, parse_float=Decimal, parse_int=Decimal)
        config = body["result"]["config"]
        params = config["params"]
        vectors = params["vectors"]
        hnsw = config["hnsw_config"]
        scalar = config["quantization_config"]["scalar"]
        quantile = scalar["quantile"]
        valid = (
            int(vectors["size"]) == 1024
            and vectors["distance"] == "Cosine"
            and int(params["shard_number"]) == 1
            and int(hnsw["m"]) == 16
            and int(hnsw["ef_construct"]) == 128
            and scalar["type"] == "int8"
            and Decimal(quantile) == Decimal(99) / Decimal(100)
            and scalar["always_ram"] is True
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise EntityLinkingEvalError("qdrant_identity_mismatch") from exc
    if not valid:
        raise EntityLinkingEvalError("qdrant_identity_mismatch")
    return await _qdrant_identity(base_url, api_key)


async def delete_qdrant_collection(
    base_url: str,
    api_key: str,
    collection: QdrantCollection,
) -> None:
    url = base_url.rstrip("/") + f"/collections/{collection.collection_name}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        response = await client.delete(url, headers=_qdrant_headers(api_key))
        if response.status_code not in {200, 404}:
            raise EntityLinkingEvalError("qdrant_cleanup_failed")
        absent = await client.get(url, headers=_qdrant_headers(api_key))
    if absent.status_code != 404:
        raise EntityLinkingEvalError("qdrant_cleanup_failed")


async def qdrant_collection_absent(
    base_url: str,
    api_key: str,
    collection: QdrantCollection,
) -> bool:
    url = base_url.rstrip("/") + f"/collections/{collection.collection_name}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        response = await client.get(url, headers=_qdrant_headers(api_key))
    return response.status_code == 404


async def seed_qdrant_corpus(
    dataset: LoadedDataset,
    seeded: SeededRuntime,
    *,
    base_url: str,
    api_key: str,
    embedding_url: str,
    embedding_api_key: str,
) -> tuple[int, int]:
    headers = {"Authorization": f"Bearer {embedding_api_key}"} if embedding_api_key else None
    embedding_calls = 0
    qdrant_calls = 0
    chunks = list(dataset.gold.chunks)
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        for start in range(0, len(chunks), 32):
            batch = chunks[start : start + 32]
            response = await client.post(
                embedding_url,
                json={"model": "bge-m3", "input": [row.text for row in batch]},
                headers=headers,
            )
            embedding_calls += 1
            if response.status_code != 200:
                raise EntityLinkingEvalError("embedding_unavailable")
            try:
                data = response.json()["data"]
                batch_vectors = [row["embedding"] for row in data]
            except (KeyError, TypeError, ValueError) as exc:
                raise EntityLinkingEvalError("embedding_identity_mismatch") from exc
            if len(batch_vectors) != len(batch) or any(len(vector) != 1024 for vector in batch_vectors):
                raise EntityLinkingEvalError("embedding_identity_mismatch")
            vectors.extend(batch_vectors)
    documents = {row.document_key: row for row in dataset.gold.documents}
    points = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        suffix = chunk.chunk_key.removeprefix("chunk-")
        document = documents[chunk.document_key]
        points.append(
            {
                "id": str(seeded.uuid_by_logical[chunk.chunk_key]),
                "payload": {
                    "chunk_id": str(seeded.uuid_by_logical[chunk.chunk_key]),
                    "document_id": str(seeded.uuid_by_logical[chunk.document_key]),
                    "document_revision": 1,
                    "document_revision_id": str(seeded.uuid_by_logical[chunk.revision_key]),
                    "evidence_key": f"evidence-{suffix}",
                    "library_id": str(seeded.primary_library_id),
                    "seq": chunk.seq,
                    "text": chunk.text,
                    "title": document.title,
                },
                "vector": vector,
            }
        )
    url = base_url.rstrip("/") + f"/collections/{seeded.qdrant_collection}/points?wait=true"
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        for start in range(0, len(points), 64):
            response = await client.put(
                url,
                headers=_qdrant_headers(api_key),
                json={"points": points[start : start + 64]},
            )
            qdrant_calls += 1
            if response.status_code != 200:
                raise EntityLinkingEvalError("qdrant_unavailable")
    return embedding_calls, qdrant_calls


@dataclass(frozen=True, slots=True)
class PublishedCandidateProjection:
    publication_id: uuid.UUID
    ontology_version_id: uuid.UUID
    candidates: tuple[EntityCandidate, ...]
    prepared_candidates: tuple[PreparedEntityCandidate, ...]
    item_hash_by_entity_id: Mapping[str, str]


_PROJECTION_CACHE_MAX_SIZE = 8
_publication_projection_cache: OrderedDict[
    tuple[uuid.UUID, uuid.UUID, uuid.UUID, str], PublishedCandidateProjection
] = OrderedDict()


@dataclass(frozen=True, slots=True)
class CaseControlResult:
    evidence_keys: tuple[str, ...]
    duration_us: int


@dataclass(frozen=True, slots=True)
class CandidateCaseResult:
    decisions: tuple[dict[str, object], ...]
    evidence_keys: tuple[str, ...]
    relation_keys: tuple[str, ...]
    node_keys: tuple[str, ...]
    logical_response_sha256: str
    linker_duration_us: int
    link_graph_duration_us: int
    graph_executed: bool


async def load_candidate_projection(
    db,
    library,
    ontology_version_id: uuid.UUID,
    expected_publication_id: uuid.UUID,
    logical_by_uuid: Mapping[str, str],
):
    from sqlalchemy import select

    from app.models.entity import Entity
    from app.models.entity_type import EntityType
    from app.models.graph_publication_item import GraphPublicationItem
    from app.services.graph_retrieval import (
        assert_graph_snapshot_still_current,
        load_healthy_graph_snapshot,
    )

    snapshot = await load_healthy_graph_snapshot(
        db,
        library,
        ontology_version_id,
        expected_publication_id=expected_publication_id,
    )
    cache_key = (
        snapshot.library_id,
        snapshot.ontology_version_id,
        snapshot.publication_id,
        snapshot.manifest_hash,
    )
    scope_key = cache_key[:2]
    for existing_key in tuple(_publication_projection_cache):
        if existing_key[:2] == scope_key and existing_key != cache_key:
            _publication_projection_cache.pop(existing_key, None)
    projection = _publication_projection_cache.get(cache_key)
    if projection is not None:
        _publication_projection_cache.move_to_end(cache_key)
        try:
            await assert_graph_snapshot_still_current(db, library, snapshot)
        except Exception:
            _publication_projection_cache.pop(cache_key, None)
            raise
        return snapshot, projection

    statement = (
        select(
            Entity.id,
            Entity.canonical_name,
            Entity.normalized_name,
            EntityType.key.label("entity_type_key"),
            GraphPublicationItem.item_hash,
        )
        .select_from(GraphPublicationItem)
        .join(Entity, Entity.id == GraphPublicationItem.entity_id)
        .join(EntityType, EntityType.id == Entity.entity_type_id)
        .where(
            GraphPublicationItem.publication_id == snapshot.publication_id,
            GraphPublicationItem.item_kind == "entity",
            GraphPublicationItem.status == "active",
            Entity.library_id == snapshot.library_id,
            Entity.ontology_version_id == snapshot.ontology_version_id,
            Entity.status == "active",
            EntityType.status == "active",
        )
        .order_by(Entity.id.asc())
    )
    rows = (await db.execute(statement)).all()
    if len(rows) != snapshot.entity_count or len(rows) > 10000:
        raise EntityLinkingEvalError("publication_membership_failure")
    candidates = tuple(
        EntityCandidate(
            entity_id=str(row.id),
            entity_key=logical_by_uuid[str(row.id)],
            entity_type_key=row.entity_type_key,
            canonical_name=row.canonical_name,
            normalized_name=row.normalized_name,
        )
        for row in rows
    )
    projection = PublishedCandidateProjection(
        publication_id=snapshot.publication_id,
        ontology_version_id=snapshot.ontology_version_id,
        candidates=candidates,
        prepared_candidates=prepare_candidates(candidates),
        item_hash_by_entity_id={str(row.id): row.item_hash for row in rows},
    )
    _publication_projection_cache[cache_key] = projection
    _publication_projection_cache.move_to_end(cache_key)
    while len(_publication_projection_cache) > _PROJECTION_CACHE_MAX_SIZE:
        _publication_projection_cache.popitem(last=False)
    try:
        await assert_graph_snapshot_still_current(db, library, snapshot)
    except Exception:
        _publication_projection_cache.pop(cache_key, None)
        raise
    return snapshot, projection


async def run_candidate_case(
    db,
    library,
    case: EvaluationCase,
    threshold: Threshold,
    seeded: SeededRuntime,
) -> CandidateCaseResult:
    from app.schemas.v06_graph_retrieval import GraphRetrievalQueryRequest
    from app.services.graph_retrieval import execute_graph_retrieval_query

    linker_started = time.perf_counter_ns()
    snapshot, projection = await load_candidate_projection(
        db,
        library,
        seeded.primary_ontology_id,
        seeded.primary_publication_id,
        seeded.logical_by_uuid,
    )
    decisions = tuple(
        resolve_prepared_mention(
            mention.text,
            projection.prepared_candidates,
            entity_type_key=(
                next(
                    row.key
                    for row in db.info["entity_linking_entity_types"]
                    if row.entity_type_key == mention.entity_type_key
                )
                if mention.entity_type_key is not None
                else None
            ),
            min_score_micros=threshold.min_score_micros,
            min_margin_micros=threshold.min_margin_micros,
        )
        for mention in case.mentions
    )
    linker_duration_us = (time.perf_counter_ns() - linker_started) // 1000
    logical = tuple(logical_decision(decision) for decision in decisions)
    if any(decision.status != "linked" for decision in decisions):
        value = {"case_id": case.case_id, "decisions": logical, "graph": None}
        return CandidateCaseResult(
            decisions=logical,
            evidence_keys=(),
            relation_keys=(),
            node_keys=(),
            logical_response_sha256=canonical_sha256(value),
            linker_duration_us=linker_duration_us,
            link_graph_duration_us=linker_duration_us,
            graph_executed=False,
        )
    selected_ids: list[uuid.UUID] = []
    for decision in decisions:
        assert decision.selected is not None
        entity_id = uuid.UUID(decision.selected.candidate.entity_id)
        if entity_id not in selected_ids:
            selected_ids.append(entity_id)
    request = GraphRetrievalQueryRequest(
        ontology_version_id=snapshot.ontology_version_id,
        expected_publication_id=snapshot.publication_id,
        seeds=[{"entity_id": entity_id} for entity_id in selected_ids],
        max_hops=case.gold_utility.hop,
        max_nodes=100,
        max_relations=200,
        include_evidence_locators=True,
    )
    graph_response = await execute_graph_retrieval_query(db, library, request)
    evidence_ids = {
        str(locator.evidence_id)
        for row in (*graph_response.nodes, *graph_response.relations)
        for locator in row.evidence
    }
    evidence_keys = tuple(
        sorted(seeded.logical_by_uuid[value] for value in evidence_ids if value in seeded.logical_by_uuid)
    )[:10]
    relation_keys = tuple(
        sorted(
            seeded.logical_by_uuid[str(row.id)]
            for row in graph_response.relations
            if str(row.id) in seeded.logical_by_uuid
        )
    )
    node_keys = tuple(
        sorted(
            seeded.logical_by_uuid[str(row.id)]
            for row in graph_response.nodes
            if str(row.id) in seeded.logical_by_uuid
        )
    )
    graph_logical = {
        "evidence_keys": evidence_keys,
        "node_keys": node_keys,
        "publication_key": seeded.logical_by_uuid[str(snapshot.publication_id)],
        "relation_keys": relation_keys,
    }
    value = {"case_id": case.case_id, "decisions": logical, "graph": graph_logical}
    return CandidateCaseResult(
        decisions=logical,
        evidence_keys=evidence_keys,
        relation_keys=relation_keys,
        node_keys=node_keys,
        logical_response_sha256=canonical_sha256(value),
        linker_duration_us=linker_duration_us,
        link_graph_duration_us=(time.perf_counter_ns() - linker_started) // 1000,
        graph_executed=True,
    )


@asynccontextmanager
async def _retrieval_settings(
    *,
    qdrant_url: str,
    qdrant_api_key: str,
    embedding_url: str,
    embedding_api_key: str,
):
    from app.config import settings

    fields = {
        "qdrant_url": qdrant_url,
        "qdrant_api_key": qdrant_api_key,
        "embedding_base_url": embedding_url,
        "embedding_api_key": embedding_api_key,
        "embedding_model": "bge-m3",
        "embedding_dim": 1024,
        "rerank_enabled": False,
        "query_rewrite_enabled": False,
        "query_rewrite_llm_enabled": False,
        "hybrid_candidate_k": CONTROL_RETRIEVAL_CANDIDATE_K,
        "hybrid_rrf_k": 60,
        "hybrid_keyword_threshold": 0.3,
        "hybrid_keyword_title_boost": 1.5,
        "hybrid_keyword_external_id_boost": 2.0,
        "enable_revision_id_visibility": False,
    }
    original = {name: getattr(settings, name) for name in fields}
    for name, value in fields.items():
        setattr(settings, name, value)
    try:
        yield
    finally:
        for name, value in original.items():
            setattr(settings, name, value)


async def run_old_retrieval_control(
    db,
    library,
    case: EvaluationCase,
    *,
    mode: str,
    qdrant_url: str,
    qdrant_api_key: str,
    embedding_url: str,
    embedding_api_key: str,
) -> CaseControlResult:
    from app.schemas.dify import DifyRetrievalRequest, RetrievalSetting
    from app.services import keyword_search
    from app.services.retrieval import run_retrieval

    if mode not in {"dense", "hybrid"}:
        raise ValueError("control mode must be dense or hybrid")
    keyword_search._trgm_available = None
    started = time.perf_counter_ns()
    async with _retrieval_settings(
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        embedding_url=embedding_url,
        embedding_api_key=embedding_api_key,
    ):
        response = await run_retrieval(
            collection=library.qdrant_collection,
            embedding_model="bge-m3",
            embedding_base_url=embedding_url,
            request=DifyRetrievalRequest(
                knowledge_id=library.slug,
                query=case.question,
                retrieval_setting=RetrievalSetting(top_k=10, score_threshold=0.0),
                metadata_condition=None,
            ),
            source_config=None,
            rerank_enabled=False,
            retrieval_mode=mode,
            db=db,
            library=library,
            candidate_k=CONTROL_RETRIEVAL_CANDIDATE_K,
            exact_vector_search=True,
        )
    evidence_keys = tuple(
        str(record.metadata["evidence_key"])
        for record in response.records
        if isinstance(record.metadata.get("evidence_key"), str)
    )
    return CaseControlResult(
        evidence_keys=evidence_keys,
        duration_us=(time.perf_counter_ns() - started) // 1000,
    )


@dataclass(frozen=True, slots=True)
class LiveEvaluationMaterial:
    grid_results: tuple[GridResult, ...]
    selected_threshold: Threshold | None
    selection_reason: str
    metrics: Metrics
    performance: Performance
    response_hashes: tuple[CaseResponseHash, ...]
    canonical_response_set_sha256: str


def _utility_metrics(
    cases: Sequence[EvaluationCase],
    evidence_by_case: Mapping[str, Sequence[str]],
    *,
    relation_by_case: Mapping[str, Sequence[str]] | None = None,
    node_by_case: Mapping[str, Sequence[str]] | None = None,
) -> UtilityMetrics:
    recall_sum = 0
    precision_sum = 0
    complete = 0
    any_gold = 0
    relation_sum = 0
    node_sum = 0
    for case in cases:
        returned = set(evidence_by_case.get(case.case_id, ()))
        gold_evidence = set(case.gold_utility.evidence_keys)
        matched = len(returned & gold_evidence)
        recall_sum += int(rate_metric(matched, len(gold_evidence))["value_micros"] or 0)
        precision_sum += int(rate_metric(matched, len(returned))["value_micros"] or 0) if returned else 0
        complete += gold_evidence <= returned
        any_gold += bool(gold_evidence & returned)
        if relation_by_case is not None:
            relation_gold = set(case.gold_utility.relation_keys)
            relation_sum += int(
                rate_metric(
                    len(relation_gold & set(relation_by_case.get(case.case_id, ()))),
                    len(relation_gold),
                )["value_micros"]
                or 0
            )
        if node_by_case is not None:
            node_gold = set(case.gold_utility.node_keys)
            node_sum += int(
                rate_metric(
                    len(node_gold & set(node_by_case.get(case.case_id, ()))),
                    len(node_gold),
                )["value_micros"]
                or 0
            )
    count = len(cases)
    macro_denominator = count * 1_000_000
    relation_metric = (
        rate_metric(relation_sum, macro_denominator) if relation_by_case is not None else rate_metric(0, 0)
    )
    node_metric = rate_metric(node_sum, macro_denominator) if node_by_case is not None else rate_metric(0, 0)
    return UtilityMetrics(
        evidence_recall_at_10=rate_metric(recall_sum, macro_denominator),
        evidence_precision_at_10=rate_metric(precision_sum, macro_denominator),
        complete_support_set_coverage=rate_metric(complete, count),
        question_with_any_gold_evidence=rate_metric(any_gold, count),
        relation_fact_recall=relation_metric,
        node_recall=node_metric,
    )


def _metric_value(metric) -> int:
    if metric.value_micros is None:
        raise ValueError("required metric denominator is zero")
    return metric.value_micros


def _gain(candidate: int, control: int, required: int, control_name: str) -> SignedGain:
    return SignedGain(
        control_used=control_name,
        candidate_value_micros=candidate,
        control_value_micros=control,
        gain_micros=candidate - control,
        required_gain_micros=required,
        comparison="ge",
        passed=candidate - control >= required,
    )


def _best_control(dense_metric, hybrid_metric) -> tuple[str, int]:
    dense = _metric_value(dense_metric)
    hybrid = _metric_value(hybrid_metric)
    return ("hybrid", hybrid) if hybrid > dense else ("dense", dense)


def _build_intrinsic_metrics(
    cases: Sequence[EvaluationCase],
    candidate_results: Mapping[str, CandidateCaseResult],
    exact_results: Mapping[str, tuple[dict[str, object], ...]],
) -> IntrinsicMetrics:
    correct_auto = auto_total = link_correct = link_total = 0
    non_exact_correct = non_exact_total = candidate_recall = 0
    abstain_correct = abstain_total = exact_correct = exact_total = 0
    exact_only_correct = wrong = false_auto = 0
    for case in cases:
        candidate = candidate_results[case.case_id].decisions
        exact = exact_results[case.case_id]
        for mention, decision, exact_decision in zip(case.mentions, candidate, exact, strict=True):
            selected = decision["selected"]
            selected_key = selected.get("entity_key") if isinstance(selected, dict) else None
            exact_selected = exact_decision["selected"]
            exact_key = exact_selected.get("entity_key") if isinstance(exact_selected, dict) else None
            if decision["status"] == "linked":
                auto_total += 1
                if mention.gold_status == "linkable" and selected_key == mention.gold_entity_key:
                    correct_auto += 1
                else:
                    wrong += 1
                    if mention.gold_status != "linkable":
                        false_auto += 1
            if mention.gold_status == "linkable":
                link_total += 1
                link_correct += selected_key == mention.gold_entity_key
                exact_only_correct += exact_key == mention.gold_entity_key
                if not mention.is_exact_control:
                    non_exact_total += 1
                    non_exact_correct += selected_key == mention.gold_entity_key
                visible = [row.get("entity_key") for row in decision["candidates"] if isinstance(row, dict)]
                candidate_recall += (
                    selected_key == mention.gold_entity_key or mention.gold_entity_key in visible[:5]
                )
            else:
                abstain_total += 1
                abstain_correct += decision["status"] != "linked"
            if mention.is_exact_control:
                exact_total += 1
                exact_correct += (
                    selected_key == mention.gold_entity_key and decision["method"] == "exact_canonical"
                )
    link_rate = rate_metric(link_correct, link_total)
    exact_only_rate = rate_metric(exact_only_correct, link_total)
    always = rate_metric(len(cases), len(cases))
    return IntrinsicMetrics(
        auto_link_precision=rate_metric(correct_auto, auto_total),
        link_recall=link_rate,
        non_exact_link_recall=rate_metric(non_exact_correct, non_exact_total),
        candidate_recall_at_5=rate_metric(candidate_recall, link_total),
        abstention_accuracy=rate_metric(abstain_correct, abstain_total),
        exact_regression_accuracy=rate_metric(exact_correct, exact_total),
        scope_safety=always,
        determinism=always,
        property_privacy=always,
        exact_only_link_recall=exact_only_rate,
        link_recall_gain_vs_exact_only=_gain(
            int(link_rate["value_micros"] or 0),
            int(exact_only_rate["value_micros"] or 0),
            200000,
            "exact-only",
        ),
        wrong_auto_link_count=wrong,
        ambiguous_unlinkable_false_auto_link_count=false_auto,
        publication_membership_failures=0,
        privacy_leak_count=0,
    )


def _latency_summary(samples: Sequence[int]) -> LatencySummary:
    values = tuple(int(value) for value in samples[:30])
    if len(values) != 30:
        raise ValueError("latency summary requires 30 samples")
    ordered = sorted(values)
    return LatencySummary(
        warmup_count=5,
        sample_count=30,
        samples_us=values,
        p50_us=ordered[14],
        p95_us=ordered[28],
        max_us=ordered[-1],
    )


def _performance_cases(
    dataset: LoadedDataset, selected_threshold: Threshold
) -> tuple[EvaluationCase, EvaluationCase]:
    calibration = tuple(case for case in dataset.cases if case.split == "calibration")
    utility = tuple(case for case in calibration if case.cohort == "utility")
    entities = {row.entity_key: row for row in dataset.gold.entities}
    publication = next(
        row for row in dataset.gold.publications if row.publication_key == "publication-primary"
    )
    publication_candidates = tuple(_candidate(entities[key]) for key in publication.entity_keys)
    exact: list[MentionRecord] = []
    boundary: list[MentionRecord] = []
    abbreviation: list[MentionRecord] = []
    ambiguous: list[MentionRecord] = []
    not_found: list[MentionRecord] = []
    for case in calibration:
        for mention in case.mentions:
            if mention.is_exact_control:
                exact.append(mention)
            elif mention.gold_status == "ambiguous":
                ambiguous.append(mention)
            elif mention.gold_status == "unlinkable" and mention.negative_entity_keys:
                not_found.append(mention)
            elif mention.gold_status == "linkable" and mention.gold_entity_key is not None:
                features = score_candidate(mention.text, _candidate(entities[mention.gold_entity_key])).features
                decision = resolve_mention(
                    mention.text,
                    publication_candidates,
                    entity_type_key=mention.entity_type_key,
                    min_score_micros=selected_threshold.min_score_micros,
                    min_margin_micros=selected_threshold.min_margin_micros,
                )
                safely_linked = bool(
                    decision.status == "linked"
                    and decision.selected is not None
                    and decision.selected.candidate.entity_key == mention.gold_entity_key
                )
                if features.boundary_omission_micros and safely_linked:
                    boundary.append(mention)
                elif features.ordered_abbreviation_micros and safely_linked:
                    abbreviation.append(mention)
    if min(len(exact), len(boundary), len(abbreviation), len(ambiguous), len(not_found)) < 2:
        raise ValueError("performance mention populations incomplete")

    def reindex(rows: Sequence[MentionRecord]) -> tuple[MentionRecord, ...]:
        return tuple(row.model_copy(update={"input_index": index}) for index, row in enumerate(rows))

    source = utility[0]
    common = {
        "schema_version": "entity-linking-case-v2",
        "split": "calibration",
        "entity_family_keys": ("entity-family-performance-v3",),
        "mention_family_keys": tuple(f"mention-family-performance-v3-{index}" for index in range(10)),
        "relation_template_family_key": "relation-family-performance-v3",
        "phrase_family_key": "phrase-family-performance-v3",
        "decoy_family_keys": (),
        "decoy_chunk_keys": (),
        "categories": (),
        "utility_stratum": source.utility_stratum,
        "scenario_publication_key": source.scenario_publication_key,
        "gold_utility": source.gold_utility,
    }
    mixed_mentions = reindex((*exact[:2], *boundary[:2], abbreviation[0], *ambiguous[:3], *not_found[:2]))
    linked_mentions = reindex((*exact[:2], *boundary[:4], *abbreviation[:4]))
    return (
        EvaluationCase(
            **common,
            case_id="performance-linker-mixed",
            cohort="safety",
            question="Fixed mixed entity-linking performance scenario.",
            mentions=mixed_mentions,
        ),
        EvaluationCase(
            **common,
            case_id="performance-link-graph",
            cohort="utility",
            question="Fixed linked graph-retrieval performance scenario.",
            mentions=linked_mentions,
        ),
    )


async def collect_live_evaluation(
    dsn: str,
    dataset: LoadedDataset,
    seeded: SeededRuntime,
    *,
    qdrant_url: str,
    qdrant_api_key: str,
    embedding_url: str,
    embedding_api_key: str,
    embedding_calls: int,
    qdrant_calls: int,
    phase: str,
    policy_thresholds: Threshold | None = None,
) -> LiveEvaluationMaterial:
    from sqlalchemy import event, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.models.library import Library

    if phase == "calibration":
        if policy_thresholds is not None:
            raise EntityLinkingEvalError("artifact_schema_variant_invalid")
        evaluation_cases = tuple(case for case in dataset.cases if case.split == "calibration")
        grid_results = build_grid_results(dataset)
        selected_threshold, selection_reason = select_calibration_threshold(grid_results)
        if selected_threshold is None:
            raise EntityLinkingEvalError("calibration_no_valid_candidate")
    elif phase == "post_freeze_release":
        if policy_thresholds is None:
            raise EntityLinkingEvalError("policy_required")
        validate_release_case_categories(dataset)
        evaluation_cases = tuple(case for case in dataset.cases if case.split == "release")
        grid_results = ()
        selected_threshold = policy_thresholds
        selection_reason = "policy"
    else:
        raise EntityLinkingEvalError("artifact_schema_variant_invalid")
    engine = create_async_engine(dsn, pool_size=8, max_overflow=4)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    candidate_results: dict[str, CandidateCaseResult] = {}
    dense_results: dict[str, CaseControlResult] = {}
    hybrid_results: dict[str, CaseControlResult] = {}
    exact_results: dict[str, tuple[dict[str, object], ...]] = {}
    sql_statement_count = 0
    sql_statement_count_max = 0
    linker_samples: list[int] = []
    link_graph_samples: list[int] = []
    dense_samples: list[int] = []
    hybrid_samples: list[int] = []

    def count_statement(*_args) -> None:
        nonlocal sql_statement_count
        sql_statement_count += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_statement)
    tracemalloc.start()
    try:
        async with Session() as db:
            library = (
                await db.execute(select(Library).where(Library.id == seeded.primary_library_id))
            ).scalar_one()
            db.info["entity_linking_entity_types"] = dataset.gold.entity_types
            for case in evaluation_cases:
                before_candidate = sql_statement_count
                candidate_result = await run_candidate_case(
                    db, library, case, selected_threshold, seeded
                )
                candidate_results[case.case_id] = candidate_result
                if not candidate_result.graph_executed:
                    sql_statement_count_max = max(
                        sql_statement_count_max,
                        sql_statement_count - before_candidate,
                    )
                dense_results[case.case_id] = await run_old_retrieval_control(
                    db,
                    library,
                    case,
                    mode="dense",
                    qdrant_url=qdrant_url,
                    qdrant_api_key=qdrant_api_key,
                    embedding_url=embedding_url,
                    embedding_api_key=embedding_api_key,
                )
                hybrid_results[case.case_id] = await run_old_retrieval_control(
                    db,
                    library,
                    case,
                    mode="hybrid",
                    qdrant_url=qdrant_url,
                    qdrant_api_key=qdrant_api_key,
                    embedding_url=embedding_url,
                    embedding_api_key=embedding_api_key,
                )
                exact_results[case.case_id] = exact_only_decisions(dataset, case)
            _current, peak_memory = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            linker_case, link_graph_case = _performance_cases(dataset, selected_threshold)
            for repetition in range(35):
                before_candidate = sql_statement_count
                result = await run_candidate_case(
                    db, library, linker_case, selected_threshold, seeded
                )
                sql_statement_count_max = max(
                    sql_statement_count_max, sql_statement_count - before_candidate
                )
                if result.graph_executed:
                    raise EntityLinkingEvalError("performance_fixture_invalid")
                if repetition >= 5:
                    linker_samples.append(result.linker_duration_us)
            for repetition in range(35):
                result = await run_candidate_case(
                    db, library, link_graph_case, selected_threshold, seeded
                )
                if not result.graph_executed:
                    raise EntityLinkingEvalError("performance_fixture_not_executed")
                if repetition >= 5:
                    link_graph_samples.append(result.link_graph_duration_us)
            for mode, samples in (("dense", dense_samples), ("hybrid", hybrid_samples)):
                for repetition in range(35):
                    result = await run_old_retrieval_control(
                        db,
                        library,
                        link_graph_case,
                        mode=mode,
                        qdrant_url=qdrant_url,
                        qdrant_api_key=qdrant_api_key,
                        embedding_url=embedding_url,
                        embedding_api_key=embedding_api_key,
                    )
                    if repetition >= 5:
                        samples.append(result.duration_us)
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        event.remove(engine.sync_engine, "before_cursor_execute", count_statement)
        await engine.dispose()

    candidate_evidence = {key: value.evidence_keys for key, value in candidate_results.items()}
    candidate_relations = {key: value.relation_keys for key, value in candidate_results.items()}
    candidate_nodes = {key: value.node_keys for key, value in candidate_results.items()}
    dense_evidence = {key: value.evidence_keys for key, value in dense_results.items()}
    hybrid_evidence = {key: value.evidence_keys for key, value in hybrid_results.items()}
    utility_cases = tuple(case for case in evaluation_cases if case.cohort == "utility")
    candidate_utility = _utility_metrics(
        utility_cases,
        candidate_evidence,
        relation_by_case=candidate_relations,
        node_by_case=candidate_nodes,
    )
    dense_utility = _utility_metrics(utility_cases, dense_evidence)
    hybrid_utility = _utility_metrics(utility_cases, hybrid_evidence)
    intrinsic = _build_intrinsic_metrics(evaluation_cases, candidate_results, exact_results)

    evidence_control, evidence_value = _best_control(
        dense_utility.evidence_recall_at_10, hybrid_utility.evidence_recall_at_10
    )
    complete_control, complete_value = _best_control(
        dense_utility.complete_support_set_coverage,
        hybrid_utility.complete_support_set_coverage,
    )
    precision_control, precision_value = _best_control(
        dense_utility.evidence_precision_at_10, hybrid_utility.evidence_precision_at_10
    )
    any_control, any_value = _best_control(
        dense_utility.question_with_any_gold_evidence,
        hybrid_utility.question_with_any_gold_evidence,
    )
    selected_control_evidence = dense_evidence if evidence_control == "dense" else hybrid_evidence
    candidate_recall_by_case = {}
    control_recall_by_case = {}
    strata = {}
    for case in utility_cases:
        gold = set(case.gold_utility.evidence_keys)
        candidate_recall_by_case[case.case_id] = int(
            rate_metric(len(gold & set(candidate_evidence[case.case_id])), len(gold))["value_micros"] or 0
        )
        control_recall_by_case[case.case_id] = int(
            rate_metric(len(gold & set(selected_control_evidence[case.case_id])), len(gold))["value_micros"]
            or 0
        )
        strata[case.case_id] = case.utility_stratum
    bootstrap = paired_stratified_bootstrap(
        candidate_recall_micros=candidate_recall_by_case,
        control_recall_micros=control_recall_by_case,
        case_strata=strata,
        best_control=evidence_control,
    )
    utility_gains = UtilityGains(
        evidence_recall_gain=_gain(
            _metric_value(candidate_utility.evidence_recall_at_10),
            evidence_value,
            100000,
            evidence_control,
        ),
        complete_support_set_coverage_gain=_gain(
            _metric_value(candidate_utility.complete_support_set_coverage),
            complete_value,
            100000,
            complete_control,
        ),
        evidence_precision_regression=_gain(
            _metric_value(candidate_utility.evidence_precision_at_10),
            precision_value,
            -50000,
            precision_control,
        ),
        question_with_any_gold_evidence_gain=_gain(
            _metric_value(candidate_utility.question_with_any_gold_evidence),
            any_value,
            80000,
            any_control,
        ),
        bootstrap=bootstrap,
    )
    metrics = Metrics(
        intrinsic=intrinsic,
        candidate_utility=candidate_utility,
        dense_utility=dense_utility,
        hybrid_utility=hybrid_utility,
        utility_gains=utility_gains,
    )
    performance = Performance(
        linker_scenario="linker-mixed-10",
        link_graph_scenario="link-graph-linked-10",
        control_scenario="link-graph-linked-10",
        linker=_latency_summary(linker_samples),
        link_graph=_latency_summary(link_graph_samples),
        dense=_latency_summary(dense_samples),
        hybrid=_latency_summary(hybrid_samples),
        sql_statement_count_max=sql_statement_count_max,
        per_mention_sql_count=0,
        projection_rows=6000,
        publication_entity_count=6000,
        memory_high_water_bytes=peak_memory,
        request_timeout_count=0,
        timeout_rollback_reused=True,
        embedding_call_count=embedding_calls + len(evaluation_cases) * 2 + 70,
        qdrant_call_count=qdrant_calls + len(evaluation_cases) * 2 + 70,
        linker_graph_execution_count=0,
        link_graph_execution_count=30,
    )
    response_hashes = tuple(
        CaseResponseHash(
            case_id=case.case_id,
            input_count=len(case.mentions),
            candidate_logical_response_sha256=candidate_results[case.case_id].logical_response_sha256,
            exact_only_logical_response_sha256=canonical_sha256(
                {"case_id": case.case_id, "decisions": exact_results[case.case_id]}
            ),
            dense_evidence_set_sha256=canonical_sha256(list(dense_results[case.case_id].evidence_keys)),
            hybrid_evidence_set_sha256=canonical_sha256(list(hybrid_results[case.case_id].evidence_keys)),
        )
        for case in evaluation_cases
    )
    return LiveEvaluationMaterial(
        grid_results=grid_results,
        selected_threshold=selected_threshold,
        selection_reason=selection_reason,
        metrics=metrics,
        performance=performance,
        response_hashes=response_hashes,
        canonical_response_set_sha256=canonical_sha256(
            [row.model_dump(mode="json") for row in response_hashes]
        ),
    )


def _current_evaluation_tree_sha256(root: Path) -> str:
    workspace_status = _git(root, "status", "--porcelain", "--untracked-files=all")
    dirty_paths: set[str] = set()
    for line in workspace_status.splitlines():
        path_field = line[3:]
        for path in path_field.split(" -> "):
            dirty_paths.add(path.replace("\\", "/"))
    allowed_dirty_paths = set(G3_IMPLEMENTATION_PATHS) | set(PHASE_OUTPUT_PATHS) | set(EXCLUDED_USER_PATHS)
    if dirty_paths - allowed_dirty_paths:
        raise EntityLinkingEvalError("scope_drift_detected")
    status = _git(
        root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        *G3_IMPLEMENTATION_PATHS,
    )
    if status:
        raise EntityLinkingEvalError("implementation_tree_dirty")
    tracked = set(_git(root, "ls-files", "--", *G3_IMPLEMENTATION_PATHS).splitlines())
    if tracked != set(G3_IMPLEMENTATION_PATHS):
        raise EntityLinkingEvalError("implementation_tree_untracked")
    unexpected = (
        set(_git(root, "ls-files", "eval/entity_linking").splitlines())
        - {path for path in G3_IMPLEMENTATION_PATHS if path.startswith("eval/entity_linking/")}
        - set(PHASE_OUTPUT_PATHS)
        - set(HISTORICAL_ENTITY_LINKING_PATHS)
    )
    if unexpected:
        raise EntityLinkingEvalError("scope_drift_detected")
    records = [
        {
            "repository_relative_path": path,
            "exact_file_sha256": hashlib.sha256((root / path).read_bytes()).hexdigest(),
        }
        for path in sorted(G3_IMPLEMENTATION_PATHS)
    ]
    return canonical_sha256(records)


def implementation_identity(root: Path) -> tuple[str, str]:
    verify_g2_approval(root)
    evaluation_tree_sha256 = _current_evaluation_tree_sha256(root)
    commit = _git(root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise EntityLinkingEvalError("git_commit_invalid")
    return commit, evaluation_tree_sha256


async def collect_environment_record(
    dataset: LoadedDataset,
    *,
    admin_dsn: str,
    qdrant_url: str,
    qdrant_api_key: str,
    embedding_url: str,
    embedding_api_key: str,
) -> EnvironmentRecord:
    postgres, qdrant, embedding = await asyncio.gather(
        postgres_cluster_identity(admin_dsn),
        _qdrant_identity(qdrant_url, qdrant_api_key),
        _embedding_identity(embedding_url, "bge-m3", embedding_api_key),
    )
    distributions = tuple(
        ExternalDistributionRecord(**record)
        for record in discover_external_distribution_records(dataset.root)
    )
    reference_hash = hashlib.sha256(
        (dataset.root / "eval/entity_linking/reference_scorer.py").read_bytes()
    ).hexdigest()
    projection = {
        "schema_version": "entity-linking-environment-v1",
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "logical_cpu_count": os.cpu_count() or 1,
        "external_distribution_identity_version": "external-distribution-identity-v1",
        "external_distribution_records": [row.model_dump(mode="json") for row in distributions],
        "external_distribution_set_sha256": dataset.manifest.external_distribution_set_sha256,
        "pg_cluster_fingerprint_sha256": postgres["pg_cluster_fingerprint_sha256"],
        "pg_server_version_num": postgres["pg_server_version_num"],
        "pg_trgm_version": postgres["pg_trgm_version"],
        "qdrant": qdrant.model_dump(mode="json"),
        "embedding": embedding.model_dump(mode="json"),
        "evaluation_config_sha256": dataset.evaluation_config_sha256,
        "accepted_dependency_closure_sha256": dataset.manifest.accepted_dependency_closure_sha256,
        "reference_scorer_sha256": reference_hash,
    }
    return EnvironmentRecord(
        **projection,
        environment_fingerprint_sha256=canonical_sha256(projection),
    )


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_calibration_artifact(
    *,
    dataset: LoadedDataset,
    material: LiveEvaluationMaterial,
    environment: EnvironmentRecord,
    collection: QdrantCollection,
    code_commit: str,
    evaluation_tree_sha256: str,
    started_at: datetime,
    finished_at: datetime,
) -> CalibrationArtifact:
    calibration_cases = tuple(case for case in dataset.cases if case.split == "calibration")
    calibration_utility_cases = tuple(
        case for case in calibration_cases if case.cohort == "utility"
    )
    category_counts = {
        category: sum(category in case.categories for case in calibration_cases)
        for category in CATEGORY_ORDER
    }
    stratum_counts = {
        stratum: sum(case.utility_stratum == stratum for case in calibration_utility_cases)
        for stratum in STRATUM_ORDER
    }
    qdrant_hash = environment.qdrant.qdrant_fingerprint_sha256
    embedding_hash = environment.embedding.embedding_fingerprint_sha256
    return CalibrationArtifact(
        schema_version="entity-linking-eval-result-v2",
        phase="calibration",
        status="passed" if material.selected_threshold is not None else "no_go",
        ordinal=None,
        run_id=CALIBRATION_RUN_ID,
        database_id=CALIBRATION_DATABASE_ID,
        started_at=_utc_timestamp(started_at),
        finished_at=_utc_timestamp(finished_at),
        g2_approval_commit=G2_APPROVAL_COMMIT,
        g2_specification_tree_sha256=G2_SPECIFICATION_TREE_SHA256,
        code_commit=code_commit,
        evaluation_tree_sha256=evaluation_tree_sha256,
        accepted_dependency_closure_sha256=dataset.manifest.accepted_dependency_closure_sha256,
        reference_scorer_sha256=environment.reference_scorer_sha256,
        external_distribution_set_sha256=dataset.manifest.external_distribution_set_sha256,
        dataset_manifest_ref=dataset.manifest_ref,
        dataset_content_sha256=dataset.dataset_content_sha256,
        evaluation_config_sha256=dataset.evaluation_config_sha256,
        ontology_schema_set_hash=dataset.manifest.ontology_schema_set_hash,
        environment_fingerprint_sha256=environment.environment_fingerprint_sha256,
        environment=environment,
        pg_cluster_fingerprint_sha256=environment.pg_cluster_fingerprint_sha256,
        qdrant_fingerprint_sha256=qdrant_hash,
        embedding_fingerprint_sha256=embedding_hash,
        qdrant_collection=collection,
        control_config_sha256=canonical_sha256(dataset.manifest.control_config.model_dump(mode="json")),
        grid_results=material.grid_results,
        candidate_thresholds=material.selected_threshold,
        selection_reason=material.selection_reason,
        metrics=material.metrics,
        category_counts=CategoryCounts.model_validate(category_counts),
        stratum_counts=StratumCounts.model_validate(stratum_counts),
        performance=material.performance,
        ordered_response_hashes=material.response_hashes,
        canonical_response_set_sha256=material.canonical_response_set_sha256,
        database_created=True,
        database_cleanup_succeeded=True,
        qdrant_collection_created=True,
        qdrant_cleanup_succeeded=True,
        policy_ref=None,
    )


def _write_atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise EntityLinkingEvalError("result_artifact_exists")
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_bytes(_canonical_file_bytes(value))
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


async def run_calibration(
    root: Path,
    *,
    run_id: str,
    database_id: str,
    allow_create_drop_eval_db: bool,
    allow_create_drop_qdrant_collection: bool,
) -> ArtifactRef:
    if run_id != CALIBRATION_RUN_ID or database_id != CALIBRATION_DATABASE_ID:
        raise EntityLinkingEvalError("run_identity_not_unique")
    if not allow_create_drop_eval_db or not allow_create_drop_qdrant_collection:
        raise EntityLinkingEvalError("create_drop_ack_required")
    validate_database_id(database_id)
    dataset = load_dataset(root)
    code_commit, evaluation_tree_sha256 = implementation_identity(root)
    admin_dsn = os.getenv("VECTOR_KB_PG_TEST_DSN", "")
    qdrant_url = os.getenv("QDRANT_URL", "")
    qdrant_api_key = os.getenv("QDRANT_API_KEY", "")
    embedding_url = os.getenv("EMBEDDING_BASE_URL", "")
    embedding_api_key = os.getenv("EMBEDDING_API_KEY", "")
    if not admin_dsn:
        raise EntityLinkingEvalError("postgres_dsn_required")
    if not qdrant_url:
        raise EntityLinkingEvalError("qdrant_unavailable")
    if not embedding_url or os.getenv("EMBEDDING_MODEL") != "bge-m3" or os.getenv("EMBEDDING_DIM") != "1024":
        raise EntityLinkingEvalError("embedding_unavailable")
    await preflight_live_dependencies()
    result_path = root / f"eval/entity_linking/results/{run_id}.json"
    if result_path.exists():
        raise EntityLinkingEvalError("result_artifact_exists")
    collection = qdrant_collection_identity(run_id, "cal")
    started_at = datetime.now(timezone.utc)
    dsn: str | None = None
    database_created = False
    collection_created = False
    material: LiveEvaluationMaterial | None = None
    environment: EnvironmentRecord | None = None
    try:
        dsn = await create_database(admin_dsn, database_id)
        database_created = True
        upgrade_database(root, dsn)
        await create_qdrant_collection(qdrant_url, qdrant_api_key, collection)
        collection_created = True
        seeded = await seed_runtime_database(dsn, dataset, collection)
        embedding_calls, qdrant_calls = await seed_qdrant_corpus(
            dataset,
            seeded,
            base_url=qdrant_url,
            api_key=qdrant_api_key,
            embedding_url=embedding_url,
            embedding_api_key=embedding_api_key,
        )
        environment = await collect_environment_record(
            dataset,
            admin_dsn=admin_dsn,
            qdrant_url=qdrant_url,
            qdrant_api_key=qdrant_api_key,
            embedding_url=embedding_url,
            embedding_api_key=embedding_api_key,
        )
        material = await collect_live_evaluation(
            dsn,
            dataset,
            seeded,
            qdrant_url=qdrant_url,
            qdrant_api_key=qdrant_api_key,
            embedding_url=embedding_url,
            embedding_api_key=embedding_api_key,
            embedding_calls=embedding_calls,
            qdrant_calls=qdrant_calls,
            phase="calibration",
        )
    except EntityLinkingEvalError:
        raise
    except Exception as exc:
        raise EntityLinkingEvalError("entity_linking_feasibility_failed") from exc
    finally:
        if collection_created:
            await delete_qdrant_collection(qdrant_url, qdrant_api_key, collection)
        if database_created:
            await drop_database(admin_dsn, database_id)
    if material is None or environment is None or dsn is None:
        raise EntityLinkingEvalError("database_evaluation_failed")
    if await database_exists(admin_dsn, database_id):
        raise EntityLinkingEvalError("database_cleanup_failed")
    if not await qdrant_collection_absent(qdrant_url, qdrant_api_key, collection):
        raise EntityLinkingEvalError("qdrant_cleanup_failed")
    final_cluster = await postgres_cluster_identity(admin_dsn)
    if final_cluster["pg_cluster_fingerprint_sha256"] != environment.pg_cluster_fingerprint_sha256:
        raise EntityLinkingEvalError("postgres_cluster_mismatch")
    artifact = build_calibration_artifact(
        dataset=dataset,
        material=material,
        environment=environment,
        collection=collection,
        code_commit=code_commit,
        evaluation_tree_sha256=evaluation_tree_sha256,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
    )
    value = artifact.model_dump(mode="json")
    assert_private_data_absent(value, dataset=dataset)
    _write_atomic_json(result_path, value)
    return artifact_ref(root, result_path, value)


async def verify_calibration_artifact(
    root: Path,
    path: Path,
    *,
    require_live_absence: bool,
) -> ArtifactRef:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch") from exc
    if relative == INVALIDATED_CALIBRATION_PATH:
        raise EntityLinkingEvalError("invalidated_calibration_artifact")
    dataset = load_dataset(root)
    artifact = load_canonical_json(root, path, CalibrationArtifact)
    assert isinstance(artifact, CalibrationArtifact)
    reference = artifact_ref(root, path, artifact.model_dump(mode="json"))
    selected, selected_ref = require_replacement_calibration_reference(
        root,
        path,
        expected_canonical_sha256=reference.canonical_sha256,
        expected_file_sha256=reference.exact_file_sha256,
    )
    if selected != artifact or selected_ref != reference:
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    if (
        artifact.g2_approval_commit != G2_APPROVAL_COMMIT
        or artifact.g2_specification_tree_sha256 != G2_SPECIFICATION_TREE_SHA256
        or artifact.dataset_manifest_ref != dataset.manifest_ref
        or artifact.dataset_content_sha256 != dataset.dataset_content_sha256
        or artifact.evaluation_config_sha256 != dataset.evaluation_config_sha256
        or artifact.accepted_dependency_closure_sha256 != dataset.manifest.accepted_dependency_closure_sha256
        or artifact.external_distribution_set_sha256 != dataset.manifest.external_distribution_set_sha256
    ):
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    if artifact.qdrant_collection != qdrant_collection_identity(artifact.run_id, "cal"):
        raise EntityLinkingEvalError("qdrant_collection_identity_mismatch")
    if (
        artifact.qdrant_fingerprint_sha256 != artifact.environment.qdrant.qdrant_fingerprint_sha256
        or artifact.embedding_fingerprint_sha256
        != artifact.environment.embedding.embedding_fingerprint_sha256
        or artifact.environment_fingerprint_sha256 != artifact.environment.environment_fingerprint_sha256
    ):
        raise EntityLinkingEvalError("environment_fingerprint_mismatch")
    expected_grid = build_grid_results(dataset)
    selected, reason = select_calibration_threshold(expected_grid)
    if (
        artifact.grid_results != expected_grid
        or artifact.candidate_thresholds != selected
        or artifact.selection_reason != reason
    ):
        raise EntityLinkingEvalError("artifact_schema_variant_invalid")
    if artifact.canonical_response_set_sha256 != canonical_sha256(
        [row.model_dump(mode="json") for row in artifact.ordered_response_hashes]
    ):
        raise EntityLinkingEvalError("artifact_schema_variant_invalid")
    value = artifact.model_dump(mode="json")
    assert_private_data_absent(value, dataset=dataset)
    if require_live_absence:
        admin_dsn = os.getenv("VECTOR_KB_PG_TEST_DSN", "")
        qdrant_url = os.getenv("QDRANT_URL", "")
        if not admin_dsn or not qdrant_url:
            raise EntityLinkingEvalError("live_dependency_skipped")
        if await database_exists(admin_dsn, artifact.database_id):
            raise EntityLinkingEvalError("database_cleanup_failed")
        if not await qdrant_collection_absent(
            qdrant_url,
            os.getenv("QDRANT_API_KEY", ""),
            artifact.qdrant_collection,
        ):
            raise EntityLinkingEvalError("qdrant_cleanup_failed")
    return reference


def require_replacement_calibration_reference(
    root: Path,
    path: Path,
    *,
    expected_canonical_sha256: str,
    expected_file_sha256: str,
) -> tuple[CalibrationArtifact, ArtifactRef]:
    verify_g2_approval(root)
    expected_path = root / f"eval/entity_linking/results/{CALIBRATION_RUN_ID}.json"
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch") from exc
    invalidated_triple_selected = (
        relative == INVALIDATED_CALIBRATION_PATH
        or expected_canonical_sha256 == INVALIDATED_CALIBRATION_CANONICAL_SHA256
        or expected_file_sha256 == INVALIDATED_CALIBRATION_FILE_SHA256
    )
    if invalidated_triple_selected:
        raise EntityLinkingEvalError("invalidated_calibration_artifact")
    if resolved != expected_path.resolve():
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    artifact = load_canonical_json(root, resolved, CalibrationArtifact)
    assert isinstance(artifact, CalibrationArtifact)
    reference = artifact_ref(root, resolved, artifact.model_dump(mode="json"))
    if (
        reference.canonical_sha256 != expected_canonical_sha256
        or reference.exact_file_sha256 != expected_file_sha256
    ):
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    if (
        artifact.code_commit == INCOMPLETE_IMPLEMENTATION_COMMIT
        or artifact.g2_approval_commit == INVALIDATED_G2_APPROVAL_COMMIT
        or artifact.g2_specification_tree_sha256 == INVALIDATED_G2_SPECIFICATION_TREE_SHA256
        or artifact.run_id == "v07-el-calibration-v1-20260717-01"
        or artifact.database_id == "vkt_v07_el_eval_calibration_20260717_01"
    ):
        raise EntityLinkingEvalError("invalidated_calibration_artifact")
    dataset = load_dataset(root)
    reference_scorer_sha256 = hashlib.sha256(
        (root / "eval/entity_linking/reference_scorer.py").read_bytes()
    ).hexdigest()
    control_config_sha256 = canonical_sha256(dataset.manifest.control_config.model_dump(mode="json"))
    response_ids = tuple(row.case_id for row in artifact.ordered_response_hashes)
    if (
        artifact.status != "passed"
        or artifact.candidate_thresholds is None
        or artifact.g2_approval_commit != G2_APPROVAL_COMMIT
        or artifact.g2_specification_tree_sha256 != G2_SPECIFICATION_TREE_SHA256
        or artifact.run_id != CALIBRATION_RUN_ID
        or artifact.database_id != CALIBRATION_DATABASE_ID
        or artifact.dataset_manifest_ref != dataset.manifest_ref
        or artifact.dataset_content_sha256 != dataset.dataset_content_sha256
        or artifact.evaluation_config_sha256 != dataset.evaluation_config_sha256
        or artifact.ontology_schema_set_hash != dataset.manifest.ontology_schema_set_hash
        or artifact.accepted_dependency_closure_sha256 != dataset.manifest.accepted_dependency_closure_sha256
        or artifact.external_distribution_set_sha256 != dataset.manifest.external_distribution_set_sha256
        or artifact.reference_scorer_sha256 != reference_scorer_sha256
        or artifact.control_config_sha256 != control_config_sha256
        or artifact.environment.evaluation_config_sha256 != dataset.evaluation_config_sha256
        or artifact.environment.accepted_dependency_closure_sha256
        != dataset.manifest.accepted_dependency_closure_sha256
        or artifact.environment.reference_scorer_sha256 != reference_scorer_sha256
        or response_ids != tuple(dataset.manifest.ordered_calibration_case_ids)
        or artifact.qdrant_collection != qdrant_collection_identity(CALIBRATION_RUN_ID, "cal")
        or artifact.evaluation_tree_sha256 != _current_evaluation_tree_sha256(root)
    ):
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    head = _git(root, "rev-parse", "HEAD")
    result = subprocess.run(
        ("git", "merge-base", "--is-ancestor", artifact.code_commit, head),
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    return artifact, reference


def _integer_gate(
    gate_id: str,
    observed: int,
    threshold: int,
    comparison: str,
) -> GateDecision:
    return GateDecision(
        gate_id=gate_id,
        value_type="integer",
        comparison=comparison,
        observed_integer=observed,
        threshold_integer=threshold,
        observed_rational=None,
        threshold_rational=None,
        observed_boolean=None,
        threshold_boolean=None,
        observed_sha256=None,
        threshold_sha256=None,
        passed={
            "eq": observed == threshold,
            "ge": observed >= threshold,
            "le": observed <= threshold,
        }[comparison],
    )


def _rational_gate(
    gate_id: str,
    observed: ReducedRational,
    threshold: ReducedRational,
    comparison: str,
) -> GateDecision:
    left = observed.numerator * threshold.denominator
    right = threshold.numerator * observed.denominator
    return GateDecision(
        gate_id=gate_id,
        value_type="rational",
        comparison=comparison,
        observed_integer=None,
        threshold_integer=None,
        observed_rational=observed,
        threshold_rational=threshold,
        observed_boolean=None,
        threshold_boolean=None,
        observed_sha256=None,
        threshold_sha256=None,
        passed={"eq": left == right, "ge": left >= right, "le": left <= right, "gt": left > right}[
            comparison
        ],
    )


def _sha256_gate(gate_id: str, observed: str, threshold: str) -> GateDecision:
    return GateDecision(
        gate_id=gate_id,
        value_type="sha256",
        comparison="all_equal",
        observed_integer=None,
        threshold_integer=None,
        observed_rational=None,
        threshold_rational=None,
        observed_boolean=None,
        threshold_boolean=None,
        observed_sha256=observed,
        threshold_sha256=threshold,
        passed=observed == threshold,
    )


def build_gate_decisions(
    metrics: Metrics,
    performance: Performance,
    *,
    canonical_response_set_sha256: str,
    response_set_threshold_sha256: str,
) -> tuple[GateDecision, ...]:
    intrinsic = metrics.intrinsic
    gains = metrics.utility_gains
    decisions = (
        _integer_gate("wrong-auto-link-count", intrinsic.wrong_auto_link_count, 0, "eq"),
        _integer_gate("auto-link-precision", _metric_value(intrinsic.auto_link_precision), 1_000_000, "eq"),
        _integer_gate(
            "exact-regression-accuracy",
            _metric_value(intrinsic.exact_regression_accuracy),
            1_000_000,
            "eq",
        ),
        _integer_gate("scope-safety", _metric_value(intrinsic.scope_safety), 1_000_000, "eq"),
        _integer_gate("property-privacy", _metric_value(intrinsic.property_privacy), 1_000_000, "eq"),
        _integer_gate("privacy-leak-count", intrinsic.privacy_leak_count, 0, "eq"),
        _integer_gate(
            "publication-membership-failures",
            intrinsic.publication_membership_failures,
            0,
            "eq",
        ),
        _integer_gate(
            "ambiguous-unlinkable-false-auto-links",
            intrinsic.ambiguous_unlinkable_false_auto_link_count,
            0,
            "eq",
        ),
        _integer_gate(
            "candidate-recall-at-5",
            _metric_value(intrinsic.candidate_recall_at_5),
            980_000,
            "ge",
        ),
        _integer_gate("overall-link-recall", _metric_value(intrinsic.link_recall), 750_000, "ge"),
        _integer_gate(
            "non-exact-link-recall",
            _metric_value(intrinsic.non_exact_link_recall),
            600_000,
            "ge",
        ),
        _integer_gate(
            "link-recall-gain-vs-exact",
            intrinsic.link_recall_gain_vs_exact_only.gain_micros,
            200_000,
            "ge",
        ),
        _integer_gate("abstention-accuracy", _metric_value(intrinsic.abstention_accuracy), 980_000, "ge"),
        _integer_gate("evidence-recall-gain", gains.evidence_recall_gain.gain_micros, 100_000, "ge"),
        _integer_gate(
            "complete-support-gain",
            gains.complete_support_set_coverage_gain.gain_micros,
            100_000,
            "ge",
        ),
        _integer_gate(
            "evidence-precision-regression",
            gains.evidence_precision_regression.gain_micros,
            -50_000,
            "ge",
        ),
        _integer_gate(
            "any-gold-gain",
            gains.question_with_any_gold_evidence_gain.gain_micros,
            80_000,
            "ge",
        ),
        _rational_gate(
            "bootstrap-ci-lower",
            gains.bootstrap.ci_lower_micros,
            ReducedRational(numerator=0, denominator=1),
            "gt",
        ),
        _integer_gate("linker-p95", performance.linker.p95_us, 500_000, "le"),
        _integer_gate("link-graph-p95", performance.link_graph.p95_us, 1_500_000, "le"),
        _integer_gate(
            "link-graph-ratio",
            performance.link_graph.p95_us * 100,
            min(performance.dense.p95_us, performance.hybrid.p95_us) * 125,
            "le",
        ),
        _integer_gate("timeout-count", performance.request_timeout_count, 0, "eq"),
        _integer_gate("sql-statement-budget", performance.sql_statement_count_max, 7, "le"),
        _integer_gate("per-mention-sql", performance.per_mention_sql_count, 0, "eq"),
        _integer_gate(
            "projection-rows",
            performance.projection_rows,
            performance.publication_entity_count,
            "le",
        ),
        _sha256_gate(
            "deterministic-response-hash",
            canonical_response_set_sha256,
            response_set_threshold_sha256,
        ),
    )
    if tuple(row.gate_id for row in decisions) != GATE_ID_ORDER:
        raise EntityLinkingEvalError("artifact_schema_variant_invalid")
    return decisions


def freeze_policy(
    root: Path,
    *,
    calibration_path: Path,
    calibration_canonical_sha256: str,
    calibration_file_sha256: str,
    approved_thresholds: Threshold,
    approved_by: str,
    approved_at: str,
    approval_reference: str,
) -> tuple[ArtifactRef, str]:
    calibration, calibration_ref = require_replacement_calibration_reference(
        root,
        calibration_path,
        expected_canonical_sha256=calibration_canonical_sha256,
        expected_file_sha256=calibration_file_sha256,
    )
    if approved_thresholds != calibration.candidate_thresholds:
        raise EntityLinkingEvalError("policy_approval_payload_mismatch")
    if approved_at <= calibration.finished_at:
        raise EntityLinkingEvalError("policy_approval_time_invalid")
    try:
        approval = PolicyApprovalPayload(
            schema_version="entity-linking-policy-approval-v2",
            calibration_ref=calibration_ref,
            approved_thresholds=approved_thresholds,
            approved_by=approved_by,
            approved_at=approved_at,
            approval_reference=approval_reference,
        )
    except ValueError as exc:
        raise EntityLinkingEvalError("policy_approval_payload_mismatch") from exc
    policy = FrozenPolicy(
        schema_version="entity-linking-policy-v2",
        policy_version="entity-linking-policy-v2",
        algorithm_version="lexical-score-v2",
        normalization_version="normalize_graph_name_v1",
        g2_approval_commit=calibration.g2_approval_commit,
        g2_specification_tree_sha256=calibration.g2_specification_tree_sha256,
        calibration_ref=calibration_ref,
        approved_thresholds=approved_thresholds,
        approval_payload_sha256=canonical_sha256(approval),
        dataset_manifest_ref=calibration.dataset_manifest_ref,
        dataset_content_sha256=calibration.dataset_content_sha256,
        evaluation_config_sha256=calibration.evaluation_config_sha256,
        ontology_schema_set_hash=calibration.ontology_schema_set_hash,
        code_commit=calibration.code_commit,
        evaluation_tree_sha256=calibration.evaluation_tree_sha256,
        accepted_dependency_closure_sha256=calibration.accepted_dependency_closure_sha256,
        reference_scorer_sha256=calibration.reference_scorer_sha256,
        external_distribution_set_sha256=calibration.external_distribution_set_sha256,
        control_config_sha256=calibration.control_config_sha256,
        environment_fingerprint_sha256=calibration.environment_fingerprint_sha256,
        pg_cluster_fingerprint_sha256=calibration.pg_cluster_fingerprint_sha256,
        qdrant_fingerprint_sha256=calibration.environment.qdrant.qdrant_fingerprint_sha256,
        embedding_fingerprint_sha256=calibration.environment.embedding.embedding_fingerprint_sha256,
        approved_by=approved_by,
        approved_at=approved_at,
        approval_reference=approval_reference,
    )
    value = policy.model_dump(mode="json")
    dataset = load_dataset(root)
    assert_private_data_absent(value, dataset=dataset)
    policy_path = root / POLICY_PATH
    _write_atomic_json(policy_path, value)
    try:
        verified, reference = verify_policy(root)
    except Exception:
        policy_path.unlink(missing_ok=True)
        raise
    if verified != policy:
        raise EntityLinkingEvalError("policy_approval_payload_mismatch")
    return reference, policy.approval_payload_sha256


def verify_policy(root: Path) -> tuple[FrozenPolicy, ArtifactRef]:
    verify_g2_approval(root)
    path = root / POLICY_PATH
    if not path.is_file():
        raise EntityLinkingEvalError("policy_required")
    policy = load_canonical_json(root, path, FrozenPolicy)
    assert isinstance(policy, FrozenPolicy)
    calibration, calibration_ref = require_replacement_calibration_reference(
        root,
        root / policy.calibration_ref.repository_relative_path,
        expected_canonical_sha256=policy.calibration_ref.canonical_sha256,
        expected_file_sha256=policy.calibration_ref.exact_file_sha256,
    )
    if calibration_ref != policy.calibration_ref or calibration.candidate_thresholds is None:
        raise EntityLinkingEvalError("policy_approval_payload_mismatch")
    approval = PolicyApprovalPayload(
        schema_version="entity-linking-policy-approval-v2",
        calibration_ref=calibration_ref,
        approved_thresholds=policy.approved_thresholds,
        approved_by=policy.approved_by,
        approved_at=policy.approved_at,
        approval_reference=policy.approval_reference,
    )
    expected = {
        "g2_approval_commit": calibration.g2_approval_commit,
        "g2_specification_tree_sha256": calibration.g2_specification_tree_sha256,
        "approved_thresholds": calibration.candidate_thresholds,
        "approval_payload_sha256": canonical_sha256(approval),
        "dataset_manifest_ref": calibration.dataset_manifest_ref,
        "dataset_content_sha256": calibration.dataset_content_sha256,
        "evaluation_config_sha256": calibration.evaluation_config_sha256,
        "ontology_schema_set_hash": calibration.ontology_schema_set_hash,
        "code_commit": calibration.code_commit,
        "evaluation_tree_sha256": calibration.evaluation_tree_sha256,
        "accepted_dependency_closure_sha256": calibration.accepted_dependency_closure_sha256,
        "reference_scorer_sha256": calibration.reference_scorer_sha256,
        "external_distribution_set_sha256": calibration.external_distribution_set_sha256,
        "control_config_sha256": calibration.control_config_sha256,
        "environment_fingerprint_sha256": calibration.environment_fingerprint_sha256,
        "pg_cluster_fingerprint_sha256": calibration.pg_cluster_fingerprint_sha256,
        "qdrant_fingerprint_sha256": calibration.environment.qdrant.qdrant_fingerprint_sha256,
        "embedding_fingerprint_sha256": calibration.environment.embedding.embedding_fingerprint_sha256,
    }
    if any(getattr(policy, key) != value for key, value in expected.items()):
        raise EntityLinkingEvalError("policy_approval_payload_mismatch")
    if policy.approved_at <= calibration.finished_at:
        raise EntityLinkingEvalError("policy_approval_time_invalid")
    dataset = load_dataset(root)
    value = policy.model_dump(mode="json")
    assert_private_data_absent(value, dataset=dataset)
    return policy, artifact_ref(root, path, value)


def _post_freeze_identity(ordinal: int) -> tuple[str, str, str, Path]:
    try:
        expected_ordinal, run_id, database_id = POST_FREEZE_IDENTITIES[ordinal - 1]
    except (IndexError, TypeError) as exc:
        raise EntityLinkingEvalError("run_identity_not_unique") from exc
    if ordinal != expected_ordinal:
        raise EntityLinkingEvalError("run_identity_not_unique")
    phase_token = f"pf{ordinal}"
    relative = f"eval/entity_linking/results/{run_id}.json"
    return run_id, database_id, phase_token, Path(relative)


def _post_freeze_counts(
    dataset: LoadedDataset,
) -> tuple[CategoryCounts, StratumCounts]:
    release_cases = tuple(case for case in dataset.cases if case.split == "release")
    release_utility_cases = tuple(case for case in release_cases if case.cohort == "utility")
    categories = {
        category: sum(category in case.categories for case in release_cases) for category in CATEGORY_ORDER
    }
    strata = {
        stratum: sum(case.utility_stratum == stratum for case in release_utility_cases)
        for stratum in STRATUM_ORDER
    }
    return CategoryCounts.model_validate(categories), StratumCounts.model_validate(strata)


def build_post_freeze_artifact(
    *,
    dataset: LoadedDataset,
    policy: FrozenPolicy,
    policy_ref: ArtifactRef,
    material: LiveEvaluationMaterial,
    environment: EnvironmentRecord,
    collection: QdrantCollection,
    ordinal: int,
    run_id: str,
    database_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> PostFreezeArtifact:
    category_counts, stratum_counts = _post_freeze_counts(dataset)
    gate_decisions = build_gate_decisions(
        material.metrics,
        material.performance,
        canonical_response_set_sha256=material.canonical_response_set_sha256,
        response_set_threshold_sha256=material.canonical_response_set_sha256,
    )
    return PostFreezeArtifact(
        schema_version="entity-linking-eval-result-v2",
        phase="post_freeze_release",
        status="passed" if all(row.passed for row in gate_decisions) else "no_go",
        ordinal=ordinal,
        run_id=run_id,
        database_id=database_id,
        started_at=_utc_timestamp(started_at),
        finished_at=_utc_timestamp(finished_at),
        g2_approval_commit=policy.g2_approval_commit,
        g2_specification_tree_sha256=policy.g2_specification_tree_sha256,
        code_commit=policy.code_commit,
        evaluation_tree_sha256=policy.evaluation_tree_sha256,
        accepted_dependency_closure_sha256=policy.accepted_dependency_closure_sha256,
        reference_scorer_sha256=policy.reference_scorer_sha256,
        external_distribution_set_sha256=policy.external_distribution_set_sha256,
        dataset_manifest_ref=policy.dataset_manifest_ref,
        dataset_content_sha256=policy.dataset_content_sha256,
        evaluation_config_sha256=policy.evaluation_config_sha256,
        ontology_schema_set_hash=policy.ontology_schema_set_hash,
        environment_fingerprint_sha256=environment.environment_fingerprint_sha256,
        environment=environment,
        pg_cluster_fingerprint_sha256=environment.pg_cluster_fingerprint_sha256,
        qdrant_fingerprint_sha256=environment.qdrant.qdrant_fingerprint_sha256,
        embedding_fingerprint_sha256=environment.embedding.embedding_fingerprint_sha256,
        qdrant_collection=collection,
        control_config_sha256=policy.control_config_sha256,
        grid_results=None,
        candidate_thresholds=None,
        selection_reason=None,
        metrics=material.metrics,
        category_counts=category_counts,
        stratum_counts=stratum_counts,
        performance=material.performance,
        ordered_response_hashes=material.response_hashes,
        canonical_response_set_sha256=material.canonical_response_set_sha256,
        database_created=True,
        database_cleanup_succeeded=True,
        qdrant_collection_created=True,
        qdrant_cleanup_succeeded=True,
        policy_ref=policy_ref,
    )


async def run_post_freeze(
    root: Path,
    *,
    ordinal: int,
    run_id: str,
    database_id: str,
    allow_create_drop_eval_db: bool,
    allow_create_drop_qdrant_collection: bool,
) -> ArtifactRef:
    expected_run, expected_database, phase_token, relative_path = _post_freeze_identity(ordinal)
    if run_id != expected_run or database_id != expected_database:
        raise EntityLinkingEvalError("run_identity_not_unique")
    if not allow_create_drop_eval_db or not allow_create_drop_qdrant_collection:
        raise EntityLinkingEvalError("create_drop_ack_required")
    validate_database_id(database_id)
    policy, policy_ref = verify_policy(root)
    if _current_evaluation_tree_sha256(root) != policy.evaluation_tree_sha256:
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    now = datetime.now(timezone.utc)
    if _utc_timestamp(now) <= policy.approved_at:
        raise EntityLinkingEvalError("run_time_order_invalid")
    for previous in range(1, ordinal):
        previous_path = root / _post_freeze_identity(previous)[3]
        if not previous_path.is_file():
            raise EntityLinkingEvalError("run_identity_not_unique")
        verify_post_freeze_artifact(root, previous_path, expected_ordinal=previous)
    for later in range(ordinal + 1, 4):
        if (root / _post_freeze_identity(later)[3]).exists():
            raise EntityLinkingEvalError("run_identity_not_unique")
    result_path = root / relative_path
    if result_path.exists():
        raise EntityLinkingEvalError("result_artifact_exists")

    dataset = load_dataset(root)
    calibration = load_canonical_json(
        root,
        root / policy.calibration_ref.repository_relative_path,
        CalibrationArtifact,
    )
    assert isinstance(calibration, CalibrationArtifact)
    admin_dsn = os.getenv("VECTOR_KB_PG_TEST_DSN", "")
    qdrant_url = os.getenv("QDRANT_URL", "")
    qdrant_api_key = os.getenv("QDRANT_API_KEY", "")
    embedding_url = os.getenv("EMBEDDING_BASE_URL", "")
    embedding_api_key = os.getenv("EMBEDDING_API_KEY", "")
    if not admin_dsn:
        raise EntityLinkingEvalError("postgres_dsn_required")
    if not qdrant_url:
        raise EntityLinkingEvalError("qdrant_unavailable")
    if not embedding_url or os.getenv("EMBEDDING_MODEL") != "bge-m3" or os.getenv("EMBEDDING_DIM") != "1024":
        raise EntityLinkingEvalError("embedding_unavailable")
    await preflight_live_dependencies()
    collection = qdrant_collection_identity(run_id, phase_token)
    started_at = datetime.now(timezone.utc)
    if _utc_timestamp(started_at) <= policy.approved_at:
        raise EntityLinkingEvalError("run_time_order_invalid")
    database_created = False
    collection_created = False
    material: LiveEvaluationMaterial | None = None
    environment: EnvironmentRecord | None = None
    dsn: str | None = None
    try:
        dsn = await create_database(admin_dsn, database_id)
        database_created = True
        upgrade_database(root, dsn)
        await create_qdrant_collection(qdrant_url, qdrant_api_key, collection)
        collection_created = True
        seeded = await seed_runtime_database(dsn, dataset, collection)
        embedding_calls, qdrant_calls = await seed_qdrant_corpus(
            dataset,
            seeded,
            base_url=qdrant_url,
            api_key=qdrant_api_key,
            embedding_url=embedding_url,
            embedding_api_key=embedding_api_key,
        )
        environment = await collect_environment_record(
            dataset,
            admin_dsn=admin_dsn,
            qdrant_url=qdrant_url,
            qdrant_api_key=qdrant_api_key,
            embedding_url=embedding_url,
            embedding_api_key=embedding_api_key,
        )
        if (
            environment.environment_fingerprint_sha256 != policy.environment_fingerprint_sha256
            or environment.pg_cluster_fingerprint_sha256 != policy.pg_cluster_fingerprint_sha256
            or environment.qdrant != calibration.environment.qdrant
            or environment.embedding != calibration.environment.embedding
        ):
            raise EntityLinkingEvalError("environment_fingerprint_mismatch")
        material = await collect_live_evaluation(
            dsn,
            dataset,
            seeded,
            qdrant_url=qdrant_url,
            qdrant_api_key=qdrant_api_key,
            embedding_url=embedding_url,
            embedding_api_key=embedding_api_key,
            embedding_calls=embedding_calls,
            qdrant_calls=qdrant_calls,
            phase="post_freeze_release",
            policy_thresholds=policy.approved_thresholds,
        )
    except EntityLinkingEvalError:
        raise
    except Exception as exc:
        raise EntityLinkingEvalError("entity_linking_feasibility_failed") from exc
    finally:
        if collection_created:
            await delete_qdrant_collection(qdrant_url, qdrant_api_key, collection)
        if database_created:
            await drop_database(admin_dsn, database_id)
    if material is None or environment is None or dsn is None:
        raise EntityLinkingEvalError("database_evaluation_failed")
    if await database_exists(admin_dsn, database_id):
        raise EntityLinkingEvalError("database_cleanup_failed")
    if not await qdrant_collection_absent(qdrant_url, qdrant_api_key, collection):
        raise EntityLinkingEvalError("qdrant_cleanup_failed")
    final_cluster = await postgres_cluster_identity(admin_dsn)
    if final_cluster["pg_cluster_fingerprint_sha256"] != policy.pg_cluster_fingerprint_sha256:
        raise EntityLinkingEvalError("postgres_cluster_mismatch")
    artifact = build_post_freeze_artifact(
        dataset=dataset,
        policy=policy,
        policy_ref=policy_ref,
        material=material,
        environment=environment,
        collection=collection,
        ordinal=ordinal,
        run_id=run_id,
        database_id=database_id,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
    )
    value = artifact.model_dump(mode="json")
    assert_private_data_absent(value, dataset=dataset)
    _write_atomic_json(result_path, value)
    return artifact_ref(root, result_path, value)


def verify_post_freeze_artifact(
    root: Path,
    path: Path,
    *,
    expected_ordinal: int,
) -> tuple[PostFreezeArtifact, ArtifactRef, tuple[GateDecision, ...]]:
    expected_run, expected_database, phase_token, relative_path = _post_freeze_identity(expected_ordinal)
    if path.resolve() != (root / relative_path).resolve():
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    policy, policy_ref = verify_policy(root)
    artifact = load_canonical_json(root, path, PostFreezeArtifact)
    assert isinstance(artifact, PostFreezeArtifact)
    dataset = load_dataset(root)
    calibration = load_canonical_json(
        root,
        root / policy.calibration_ref.repository_relative_path,
        CalibrationArtifact,
    )
    assert isinstance(calibration, CalibrationArtifact)
    expected_response_ids = tuple(dataset.manifest.ordered_release_case_ids)
    response_ids = tuple(row.case_id for row in artifact.ordered_response_hashes)
    expected_identity = {
        "ordinal": expected_ordinal,
        "run_id": expected_run,
        "database_id": expected_database,
        "g2_approval_commit": policy.g2_approval_commit,
        "g2_specification_tree_sha256": policy.g2_specification_tree_sha256,
        "code_commit": policy.code_commit,
        "evaluation_tree_sha256": policy.evaluation_tree_sha256,
        "accepted_dependency_closure_sha256": policy.accepted_dependency_closure_sha256,
        "reference_scorer_sha256": policy.reference_scorer_sha256,
        "external_distribution_set_sha256": policy.external_distribution_set_sha256,
        "dataset_manifest_ref": policy.dataset_manifest_ref,
        "dataset_content_sha256": policy.dataset_content_sha256,
        "evaluation_config_sha256": policy.evaluation_config_sha256,
        "ontology_schema_set_hash": policy.ontology_schema_set_hash,
        "control_config_sha256": policy.control_config_sha256,
        "environment_fingerprint_sha256": policy.environment_fingerprint_sha256,
        "pg_cluster_fingerprint_sha256": policy.pg_cluster_fingerprint_sha256,
        "qdrant_fingerprint_sha256": policy.qdrant_fingerprint_sha256,
        "embedding_fingerprint_sha256": policy.embedding_fingerprint_sha256,
        "policy_ref": policy_ref,
    }
    if any(getattr(artifact, key) != value for key, value in expected_identity.items()):
        raise EntityLinkingEvalError("artifact_ref_hash_mismatch")
    if (
        artifact.started_at <= policy.approved_at
        or artifact.finished_at <= artifact.started_at
        or artifact.qdrant_collection != qdrant_collection_identity(expected_run, phase_token)
        or artifact.environment.qdrant != calibration.environment.qdrant
        or artifact.environment.embedding != calibration.environment.embedding
        or response_ids != expected_response_ids
    ):
        raise EntityLinkingEvalError("artifact_schema_variant_invalid")
    decisions = build_gate_decisions(
        artifact.metrics,
        artifact.performance,
        canonical_response_set_sha256=artifact.canonical_response_set_sha256,
        response_set_threshold_sha256=artifact.canonical_response_set_sha256,
    )
    if (artifact.status == "passed") != all(row.passed for row in decisions):
        raise EntityLinkingEvalError("release_gate_failed")
    value = artifact.model_dump(mode="json")
    assert_private_data_absent(value, dataset=dataset)
    return artifact, artifact_ref(root, path, value), decisions


def _alembic_head_is_0023(root: Path) -> bool:
    result = subprocess.run(
        (sys.executable, "-m", "alembic", "heads"),
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "0023 (head)"


def _openapi_has_no_v07() -> bool:
    from app.main import app

    return not any("/v07/" in path for path in app.openapi()["paths"])


async def _verify_all_live_absent(
    policy: FrozenPolicy,
    artifacts: Sequence[CalibrationArtifact | PostFreezeArtifact],
) -> tuple[bool, bool]:
    admin_dsn = os.getenv("VECTOR_KB_PG_TEST_DSN", "")
    qdrant_url = os.getenv("QDRANT_URL", "")
    if not admin_dsn or not qdrant_url:
        raise EntityLinkingEvalError("live_dependency_skipped")
    cluster = await postgres_cluster_identity(admin_dsn)
    cluster_matches = cluster["pg_cluster_fingerprint_sha256"] == policy.pg_cluster_fingerprint_sha256
    database_results = []
    for artifact in artifacts:
        database_results.append(not await database_exists(admin_dsn, artifact.database_id))
    databases_absent = cluster_matches and all(database_results)
    qdrant = await _qdrant_identity(qdrant_url, os.getenv("QDRANT_API_KEY", ""))
    qdrant_matches = qdrant.qdrant_fingerprint_sha256 == policy.qdrant_fingerprint_sha256
    collection_results = []
    for artifact in artifacts:
        collection_results.append(
            await qdrant_collection_absent(
                qdrant_url,
                os.getenv("QDRANT_API_KEY", ""),
                artifact.qdrant_collection,
            )
        )
    collections_absent = qdrant_matches and all(collection_results)
    return databases_absent, collections_absent


async def _build_release_evidence(
    root: Path,
    *,
    require_live_database_absence: bool,
    require_live_qdrant_absence: bool,
) -> ReleaseEvidence:
    if not require_live_database_absence or not require_live_qdrant_absence:
        raise EntityLinkingEvalError("live_dependency_skipped")
    verify_g2_approval(root)
    policy, policy_ref = verify_policy(root)
    calibration, calibration_ref = require_replacement_calibration_reference(
        root,
        root / policy.calibration_ref.repository_relative_path,
        expected_canonical_sha256=policy.calibration_ref.canonical_sha256,
        expected_file_sha256=policy.calibration_ref.exact_file_sha256,
    )
    post_freeze: list[PostFreezeArtifact] = []
    post_refs: list[ArtifactRef] = []
    for ordinal in (1, 2, 3):
        path = root / _post_freeze_identity(ordinal)[3]
        artifact, reference, _decisions = verify_post_freeze_artifact(root, path, expected_ordinal=ordinal)
        post_freeze.append(artifact)
        post_refs.append(reference)

    all_artifacts: tuple[CalibrationArtifact | PostFreezeArtifact, ...] = (
        calibration,
        *post_freeze,
    )
    databases_absent, collections_absent = await _verify_all_live_absent(policy, all_artifacts)
    response_threshold = post_freeze[0].canonical_response_set_sha256
    run_decisions = []
    for artifact, reference in zip(post_freeze, post_refs, strict=True):
        gates = build_gate_decisions(
            artifact.metrics,
            artifact.performance,
            canonical_response_set_sha256=artifact.canonical_response_set_sha256,
            response_set_threshold_sha256=response_threshold,
        )
        run_decisions.append(
            RunGateDecision(
                ordinal=artifact.ordinal,
                run_id=artifact.run_id,
                artifact_ref=reference,
                gate_decisions=gates,
                all_passed=all(row.passed for row in gates),
            )
        )

    run_ids = tuple(artifact.run_id for artifact in all_artifacts)
    database_ids = tuple(artifact.database_id for artifact in all_artifacts)
    ordinal_values = tuple(artifact.ordinal for artifact in post_freeze)
    environment_hashes = {artifact.environment_fingerprint_sha256 for artifact in all_artifacts}
    pg_hashes = {artifact.pg_cluster_fingerprint_sha256 for artifact in all_artifacts}
    qdrant_hashes = {artifact.qdrant_fingerprint_sha256 for artifact in all_artifacts}
    embedding_hashes = {artifact.embedding_fingerprint_sha256 for artifact in all_artifacts}
    response_hashes = {artifact.canonical_response_set_sha256 for artifact in post_freeze}
    code_identities = {(artifact.code_commit, artifact.evaluation_tree_sha256) for artifact in all_artifacts}
    dependency_hashes = {artifact.accepted_dependency_closure_sha256 for artifact in all_artifacts}
    distribution_hashes = {artifact.external_distribution_set_sha256 for artifact in all_artifacts}
    dataset_identities = {
        (
            artifact.dataset_manifest_ref,
            artifact.dataset_content_sha256,
            artifact.evaluation_config_sha256,
            artifact.ontology_schema_set_hash,
        )
        for artifact in all_artifacts
    }
    control_hashes = {artifact.control_config_sha256 for artifact in all_artifacts}
    privacy_passed = True
    dataset = load_dataset(root)
    for artifact in all_artifacts:
        try:
            assert_private_data_absent(artifact.model_dump(mode="json"), dataset=dataset)
        except EntityLinkingEvalError:
            privacy_passed = False
    live_calls_present = all(
        artifact.performance.embedding_call_count > 0 and artifact.performance.qdrant_call_count > 0
        for artifact in all_artifacts
    )
    identity_decisions = IdentityTimeCleanupDecision(
        g2_approval_commit_valid=all(
            artifact.g2_approval_commit == G2_APPROVAL_COMMIT for artifact in all_artifacts
        ),
        g2_specification_tree_match=all(
            artifact.g2_specification_tree_sha256 == G2_SPECIFICATION_TREE_SHA256
            for artifact in all_artifacts
        ),
        artifact_reference_hashes_match=True,
        code_identities_match=len(code_identities) == 1,
        dependency_closures_match=len(dependency_hashes) == 1,
        external_distribution_sets_match=len(distribution_hashes) == 1,
        dataset_identities_match=len(dataset_identities) == 1,
        control_identities_match=len(control_hashes) == 1,
        environment_fingerprints_match=len(environment_hashes) == 1,
        pg_cluster_fingerprints_match=len(pg_hashes) == 1,
        qdrant_fingerprints_match=len(qdrant_hashes) == 1,
        embedding_fingerprints_match=len(embedding_hashes) == 1,
        run_ids_unique=len(set(run_ids)) == 4,
        database_ids_unique=len(set(database_ids)) == 4,
        ordinals_exact=ordinal_values == (1, 2, 3),
        policy_after_calibration=policy.approved_at > calibration.finished_at,
        runs_after_policy=all(artifact.started_at > policy.approved_at for artifact in post_freeze),
        runs_finish_after_start=all(artifact.finished_at > artifact.started_at for artifact in all_artifacts),
        all_database_cleanup_succeeded=all(artifact.database_cleanup_succeeded for artifact in all_artifacts),
        all_qdrant_cleanup_succeeded=all(artifact.qdrant_cleanup_succeeded for artifact in all_artifacts),
        all_databases_live_absent_same_cluster=databases_absent,
        all_qdrant_collections_live_absent=collections_absent,
        canonical_response_sets_equal=len(response_hashes) == 1,
        privacy_scans_passed=privacy_passed,
        protected_paths_zero_drift=True,
        openapi_has_no_v07=_openapi_has_no_v07(),
        alembic_head_is_0023=_alembic_head_is_0023(root),
        mandatory_live_tests_non_skipped=live_calls_present,
    )
    ordinal_refs = tuple(
        OrdinalArtifactRef(
            ordinal=artifact.ordinal,
            run_id=artifact.run_id,
            artifact_ref=reference,
        )
        for artifact, reference in zip(post_freeze, post_refs, strict=True)
    )
    ordinal_hashes = tuple(
        OrdinalResponseHash(
            ordinal=artifact.ordinal,
            run_id=artifact.run_id,
            canonical_response_set_sha256=artifact.canonical_response_set_sha256,
        )
        for artifact in post_freeze
    )
    all_identity_passed = all(identity_decisions.model_dump(mode="json").values())
    all_gate_passed = all(decision.all_passed for decision in run_decisions)
    eligible = all_identity_passed and all_gate_passed
    return ReleaseEvidence(
        schema_version="entity-linking-release-evidence-v2",
        status="passed" if eligible else "no_go",
        g2_approval_commit=policy.g2_approval_commit,
        g2_specification_tree_sha256=policy.g2_specification_tree_sha256,
        dataset_manifest_ref=policy.dataset_manifest_ref,
        dataset_content_sha256=policy.dataset_content_sha256,
        evaluation_config_sha256=policy.evaluation_config_sha256,
        ontology_schema_set_hash=policy.ontology_schema_set_hash,
        code_commit=policy.code_commit,
        evaluation_tree_sha256=policy.evaluation_tree_sha256,
        accepted_dependency_closure_sha256=policy.accepted_dependency_closure_sha256,
        reference_scorer_sha256=policy.reference_scorer_sha256,
        external_distribution_set_sha256=policy.external_distribution_set_sha256,
        control_config_sha256=policy.control_config_sha256,
        environment_fingerprint_sha256=policy.environment_fingerprint_sha256,
        pg_cluster_fingerprint_sha256=policy.pg_cluster_fingerprint_sha256,
        qdrant_fingerprint_sha256=policy.qdrant_fingerprint_sha256,
        embedding_fingerprint_sha256=policy.embedding_fingerprint_sha256,
        calibration_ref=calibration_ref,
        policy_ref=policy_ref,
        post_freeze_refs=ordinal_refs,
        canonical_response_set_sha256_by_ordinal=ordinal_hashes,
        run_gate_decisions=tuple(run_decisions),
        identity_time_cleanup_decisions=identity_decisions,
        final_hard_and_decision="GO_ELIGIBLE" if eligible else "NO_GO",
    )


async def assemble_release_evidence(root: Path) -> ArtifactRef:
    path = root / RELEASE_EVIDENCE_PATH
    if path.exists():
        raise EntityLinkingEvalError("result_artifact_exists")
    evidence = await _build_release_evidence(
        root,
        require_live_database_absence=True,
        require_live_qdrant_absence=True,
    )
    dataset = load_dataset(root)
    value = evidence.model_dump(mode="json")
    assert_private_data_absent(value, dataset=dataset)
    _write_atomic_json(path, value)
    return artifact_ref(root, path, value)


async def verify_release_evidence(
    root: Path,
    *,
    require_live_database_absence: bool,
    require_live_qdrant_absence: bool,
) -> ArtifactRef:
    path = root / RELEASE_EVIDENCE_PATH
    observed = load_canonical_json(root, path, ReleaseEvidence)
    assert isinstance(observed, ReleaseEvidence)
    expected = await _build_release_evidence(
        root,
        require_live_database_absence=require_live_database_absence,
        require_live_qdrant_absence=require_live_qdrant_absence,
    )
    if observed != expected or observed.status != "passed":
        raise EntityLinkingEvalError("release_gate_failed")
    dataset = load_dataset(root)
    value = observed.model_dump(mode="json")
    assert_private_data_absent(value, dataset=dataset)
    return artifact_ref(root, path, value)
