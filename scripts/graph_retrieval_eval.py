from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.graph_canonical import canonical_graph_json_v1  # noqa: E402
from app.services.graph_retrieval_eval import (  # noqa: E402
    assert_sanitized_graph_retrieval_artifact,
    canonical_json_sha256,
    load_graph_retrieval_eval_dataset,
    load_graph_retrieval_eval_policy,
    load_graph_retrieval_release_evidence,
    write_canonical_json_file,
)
from app.services.graph_retrieval_eval_runtime import (  # noqa: E402
    build_graph_retrieval_calibration_artifact,
    build_graph_retrieval_post_freeze_artifact,
    collect_graph_retrieval_eval_run,
    create_graph_retrieval_eval_database,
    drop_graph_retrieval_eval_database,
    graph_retrieval_eval_database_exists,
    upgrade_graph_retrieval_eval_database,
    validate_graph_retrieval_eval_database_id,
)


MANIFEST = ROOT / "eval/graph_retrieval/manifests/release_v1.json"
POLICY = ROOT / "eval/graph_retrieval/release_policy_v1.json"
RELEASE_EVIDENCE = ROOT / "eval/graph_retrieval/release_evidence_v1.json"
IMPLEMENTATION_PATHS = (
    ".env.example",
    "app/api/v06_graph_retrieval.py",
    "app/config.py",
    "app/main.py",
    "app/schemas/v06_graph_retrieval.py",
    "app/services/graph_canonical.py",
    "app/services/graph_normalization.py",
    "app/services/graph_retrieval.py",
    "app/services/graph_retrieval_eval.py",
    "app/services/graph_retrieval_eval_runtime.py",
    "app/services/graph_retrieval_observability.py",
    "scripts/graph_retrieval_eval.py",
)
DATASET_PATHS = (
    "eval/graph_retrieval/gold_v1.json",
    "eval/graph_retrieval/release_v1.jsonl",
    "eval/graph_retrieval/manifests/release_v1.json",
)
CLEAN_BOUNDARY_PATHS = IMPLEMENTATION_PATHS + DATASET_PATHS


class GraphRetrievalEvalCliError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _emit(value: dict[str, Any]) -> None:
    print(canonical_graph_json_v1(value))


def _git(*args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise GraphRetrievalEvalCliError("git_state_unavailable")
    return result.stdout.strip()


def _implementation_identity() -> tuple[str, str]:
    status = _git(
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        *CLEAN_BOUNDARY_PATHS,
    )
    if status:
        raise GraphRetrievalEvalCliError("implementation_tree_dirty")
    tracked = set(_git("ls-files", "--", *CLEAN_BOUNDARY_PATHS).splitlines())
    if tracked != set(CLEAN_BOUNDARY_PATHS):
        raise GraphRetrievalEvalCliError("implementation_tree_untracked")
    hashes = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in IMPLEMENTATION_PATHS
    }
    commit = _git("rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise GraphRetrievalEvalCliError("git_commit_invalid")
    return commit, canonical_json_sha256(hashes)


def _load_dataset():
    return load_graph_retrieval_eval_dataset(
        repository_root=ROOT,
        manifest_path=MANIFEST,
    )


def _forbidden_values(dataset) -> tuple[str, ...]:
    return tuple(
        marker
        for row in dataset.gold.privacy_canaries
        for marker in (row.key_marker, row.value_marker)
    )


def _require_database_args(args) -> str:
    if not args.allow_create_drop_eval_db:
        raise GraphRetrievalEvalCliError("create_drop_ack_required")
    validate_graph_retrieval_eval_database_id(args.database_id)
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", args.run_id) is None:
        raise GraphRetrievalEvalCliError("run_id_invalid")
    admin_dsn = os.getenv("VECTOR_KB_PG_TEST_DSN")
    if not admin_dsn:
        raise GraphRetrievalEvalCliError("postgres_dsn_required")
    return admin_dsn


def _run_database_phase(args, *, post_freeze: bool) -> dict[str, Any]:
    admin_dsn = _require_database_args(args)
    dataset = _load_dataset()
    code_commit, implementation_hash = _implementation_identity()
    result_path = ROOT / f"eval/graph_retrieval/results/{args.run_id}.json"
    if result_path.exists():
        raise GraphRetrievalEvalCliError("result_artifact_exists")
    loaded_policy = None
    if post_freeze:
        loaded_policy = load_graph_retrieval_eval_policy(
            repository_root=ROOT,
            policy_path=POLICY,
        )
        policy = loaded_policy.policy
        if (
            policy.implementation_tree_sha256 != implementation_hash
            or policy.dataset_manifest_sha256 != dataset.dataset_manifest_sha256
            or policy.dataset_content_sha256 != dataset.dataset_content_sha256
            or policy.evaluation_config_sha256 != dataset.evaluation_config_sha256
        ):
            raise GraphRetrievalEvalCliError("policy_input_hash_mismatch")

    started_at = datetime.now(timezone.utc)
    material = None
    created = False
    cleanup_succeeded = False
    try:
        dsn = asyncio.run(
            create_graph_retrieval_eval_database(admin_dsn, args.database_id)
        )
        created = True
        upgrade_graph_retrieval_eval_database(ROOT, dsn)
        material = asyncio.run(collect_graph_retrieval_eval_run(dsn, dataset))
    finally:
        if created:
            asyncio.run(
                drop_graph_retrieval_eval_database(admin_dsn, args.database_id)
            )
            cleanup_succeeded = not asyncio.run(
                graph_retrieval_eval_database_exists(admin_dsn, args.database_id)
            )
    if material is None:
        raise GraphRetrievalEvalCliError("database_evaluation_failed")
    finished_at = datetime.now(timezone.utc)
    if post_freeze:
        if loaded_policy is None:
            raise GraphRetrievalEvalCliError("release_policy_required")
        artifact = build_graph_retrieval_post_freeze_artifact(
            run_id=args.run_id,
            database_id=args.database_id,
            started_at=started_at,
            finished_at=finished_at,
            code_commit=code_commit,
            implementation_tree_sha256=implementation_hash,
            dataset=dataset,
            material=material,
            loaded_policy=loaded_policy,
            database_cleanup_succeeded=cleanup_succeeded,
        )
    else:
        artifact = build_graph_retrieval_calibration_artifact(
            run_id=args.run_id,
            database_id=args.database_id,
            started_at=started_at,
            finished_at=finished_at,
            code_commit=code_commit,
            implementation_tree_sha256=implementation_hash,
            dataset=dataset,
            material=material,
            database_cleanup_succeeded=cleanup_succeeded,
        )
    artifact_value = artifact.model_dump(mode="json")
    assert_sanitized_graph_retrieval_artifact(
        artifact_value,
        forbidden_values=_forbidden_values(dataset),
    )
    write_canonical_json_file(result_path, artifact_value)
    return {
        "artifact_path": result_path.relative_to(ROOT).as_posix(),
        "canonical_sha256": canonical_json_sha256(artifact_value),
        "case_count": dataset.manifest.counts.cases,
        "phase": artifact.phase,
        "status": artifact.status,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graph_retrieval_eval")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-dataset")
    for name in ("calibrate", "post-freeze"):
        command_parser = commands.add_parser(name)
        command_parser.add_argument("--run-id", required=True)
        command_parser.add_argument("--database-id", required=True)
        command_parser.add_argument(
            "--allow-create-drop-eval-db",
            action="store_true",
        )
    commands.add_parser("verify-policy")
    commands.add_parser("verify-release")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "validate-dataset":
            dataset = _load_dataset()
            result = {
                "case_count": dataset.manifest.counts.cases,
                "dataset_content_sha256": dataset.dataset_content_sha256,
                "dataset_manifest_sha256": dataset.dataset_manifest_sha256,
                "evaluation_config_sha256": dataset.evaluation_config_sha256,
                "phase": "validate-dataset",
                "status": "passed",
            }
        elif args.command == "calibrate":
            result = _run_database_phase(args, post_freeze=False)
        elif args.command == "post-freeze":
            result = _run_database_phase(args, post_freeze=True)
        elif args.command == "verify-policy":
            loaded = load_graph_retrieval_eval_policy(
                repository_root=ROOT,
                policy_path=POLICY,
            )
            result = {
                "canonical_sha256": loaded.policy_canonical_sha256,
                "phase": "verify-policy",
                "policy_id": loaded.policy.policy_id,
                "status": "passed",
            }
        else:
            loaded = load_graph_retrieval_release_evidence(
                repository_root=ROOT,
                evidence_path=RELEASE_EVIDENCE,
            )
            result = {
                "evidence_id": loaded.evidence.evidence_id,
                "phase": "verify-release",
                "post_freeze_run_count": len(loaded.post_freeze_runs),
                "status": "passed",
            }
        _emit(result)
        return 0 if result["status"] == "passed" else 1
    except GraphRetrievalEvalCliError as exc:
        _emit(
            {
                "error_code": exc.code,
                "phase": args.command,
                "status": "failed",
            }
        )
        return 1
    except Exception:
        _emit(
            {
                "error_code": "graph_retrieval_eval_failed",
                "phase": args.command,
                "status": "failed",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
