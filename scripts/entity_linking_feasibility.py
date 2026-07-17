from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.graph_canonical import canonical_graph_json_v1  # noqa: E402
from eval.entity_linking.runtime import (  # noqa: E402
    G2_APPROVAL_COMMIT,
    EntityLinkingEvalError,
    implementation_identity,
    load_dataset,
    preflight_live_dependencies,
    run_calibration,
    validate_conformance,
    verify_calibration_artifact,
    verify_g2_approval,
)


def _emit(value: dict[str, Any]) -> None:
    print(canonical_graph_json_v1(value))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="entity_linking_feasibility")
    parser.add_argument("--g2-approval-commit", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-dataset")
    commands.add_parser("verify-reference-scorer")
    commands.add_parser("verify-boundary")
    commands.add_parser("preflight-live-dependencies")
    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("--run-id", required=True)
    calibrate.add_argument("--database-id", required=True)
    calibrate.add_argument("--allow-create-drop-eval-db", action="store_true")
    calibrate.add_argument("--allow-create-drop-qdrant-collection", action="store_true")
    verify = commands.add_parser("verify-calibration")
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--require-live-absence", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.g2_approval_commit != G2_APPROVAL_COMMIT:
            raise EntityLinkingEvalError("g2_approval_commit_invalid")
        verify_g2_approval(ROOT)
        if args.command == "validate-dataset":
            dataset = load_dataset(ROOT)
            result = {
                "case_count": len(dataset.cases),
                "dataset_content_sha256": dataset.dataset_content_sha256,
                "evaluation_config_sha256": dataset.evaluation_config_sha256,
                "manifest_canonical_sha256": dataset.manifest_ref.canonical_sha256,
                "manifest_file_sha256": dataset.manifest_ref.exact_file_sha256,
                "phase": args.command,
                "status": "passed",
            }
        elif args.command == "verify-reference-scorer":
            dataset = load_dataset(ROOT)
            validate_conformance(dataset.conformance)
            result = {
                "conformance_case_count": (
                    len(dataset.conformance.feature_cases)
                    + len(dataset.conformance.decision_cases)
                    + len(dataset.conformance.ordering_cases)
                    + len(dataset.conformance.category_predicate_cases)
                ),
                "phase": args.command,
                "reference_scorer_sha256": hashlib.sha256(
                    (ROOT / "eval/entity_linking/reference_scorer.py").read_bytes()
                ).hexdigest(),
                "status": "passed",
            }
        elif args.command == "verify-boundary":
            code_commit, evaluation_tree_sha256 = implementation_identity(ROOT)
            result = {
                "code_commit": code_commit,
                "evaluation_tree_sha256": evaluation_tree_sha256,
                "phase": args.command,
                "status": "passed",
            }
        elif args.command == "preflight-live-dependencies":
            result = asyncio.run(preflight_live_dependencies())
            result["phase"] = args.command
        elif args.command == "calibrate":
            reference = asyncio.run(
                run_calibration(
                    ROOT,
                    run_id=args.run_id,
                    database_id=args.database_id,
                    allow_create_drop_eval_db=args.allow_create_drop_eval_db,
                    allow_create_drop_qdrant_collection=args.allow_create_drop_qdrant_collection,
                )
            )
            result = {
                "artifact_path": reference.repository_relative_path,
                "canonical_sha256": reference.canonical_sha256,
                "exact_file_sha256": reference.exact_file_sha256,
                "phase": args.command,
                "status": "passed",
            }
        else:
            path = (ROOT / args.artifact).resolve()
            reference = asyncio.run(
                verify_calibration_artifact(
                    ROOT,
                    path,
                    require_live_absence=args.require_live_absence,
                )
            )
            result = {
                "artifact_path": reference.repository_relative_path,
                "canonical_sha256": reference.canonical_sha256,
                "exact_file_sha256": reference.exact_file_sha256,
                "phase": args.command,
                "status": "passed",
            }
        _emit(result)
        return 0
    except EntityLinkingEvalError as exc:
        _emit({"error_code": exc.code, "phase": args.command, "status": "failed"})
        return 1
    except Exception:
        _emit(
            {
                "error_code": "entity_linking_feasibility_failed",
                "phase": args.command,
                "status": "failed",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
