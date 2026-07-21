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
from eval.entity_linking.contracts import Threshold  # noqa: E402
from eval.entity_linking.runtime import (  # noqa: E402
    G2_APPROVAL_COMMIT,
    EntityLinkingEvalError,
    assemble_release_evidence,
    freeze_policy,
    implementation_identity,
    load_dataset,
    preflight_live_dependencies,
    run_calibration,
    run_post_freeze,
    validate_conformance,
    verify_calibration_artifact,
    verify_g2_approval,
    verify_policy,
    verify_release_evidence,
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
    freeze = commands.add_parser("freeze-policy")
    freeze.add_argument("--calibration", required=True)
    freeze.add_argument("--calibration-canonical-sha256", required=True)
    freeze.add_argument("--calibration-file-sha256", required=True)
    freeze.add_argument("--min-score-micros", required=True, type=int)
    freeze.add_argument("--min-margin-micros", required=True, type=int)
    freeze.add_argument("--candidate-floor-micros", required=True, type=int)
    freeze.add_argument("--max-candidates", required=True, type=int)
    freeze.add_argument("--approved-by", required=True)
    freeze.add_argument("--approved-at", required=True)
    freeze.add_argument("--approval-reference", required=True)
    commands.add_parser("verify-policy")
    post_freeze = commands.add_parser("post-freeze")
    post_freeze.add_argument("--ordinal", required=True, type=int)
    post_freeze.add_argument("--run-id", required=True)
    post_freeze.add_argument("--database-id", required=True)
    post_freeze.add_argument("--allow-create-drop-eval-db", action="store_true")
    post_freeze.add_argument("--allow-create-drop-qdrant-collection", action="store_true")
    commands.add_parser("assemble-release-evidence")
    release = commands.add_parser("verify-release")
    release.add_argument("--require-live-database-absence", action="store_true")
    release.add_argument("--require-live-qdrant-absence", action="store_true")
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
        elif args.command == "verify-calibration":
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
        elif args.command == "freeze-policy":
            try:
                thresholds = Threshold(
                    min_score_micros=args.min_score_micros,
                    min_margin_micros=args.min_margin_micros,
                    candidate_floor_micros=args.candidate_floor_micros,
                    max_candidates=args.max_candidates,
                )
            except ValueError as exc:
                raise EntityLinkingEvalError("policy_approval_payload_mismatch") from exc
            reference, approval_payload_sha256 = freeze_policy(
                ROOT,
                calibration_path=(ROOT / args.calibration).resolve(),
                calibration_canonical_sha256=args.calibration_canonical_sha256,
                calibration_file_sha256=args.calibration_file_sha256,
                approved_thresholds=thresholds,
                approved_by=args.approved_by,
                approved_at=args.approved_at,
                approval_reference=args.approval_reference,
            )
            result = {
                "approval_payload_sha256": approval_payload_sha256,
                "artifact_path": reference.repository_relative_path,
                "canonical_sha256": reference.canonical_sha256,
                "exact_file_sha256": reference.exact_file_sha256,
                "phase": args.command,
                "status": "passed",
            }
        elif args.command == "verify-policy":
            policy, reference = verify_policy(ROOT)
            result = {
                "approval_payload_sha256": policy.approval_payload_sha256,
                "artifact_path": reference.repository_relative_path,
                "canonical_sha256": reference.canonical_sha256,
                "exact_file_sha256": reference.exact_file_sha256,
                "phase": args.command,
                "status": "passed",
            }
        elif args.command == "post-freeze":
            reference = asyncio.run(
                run_post_freeze(
                    ROOT,
                    ordinal=args.ordinal,
                    run_id=args.run_id,
                    database_id=args.database_id,
                    allow_create_drop_eval_db=args.allow_create_drop_eval_db,
                    allow_create_drop_qdrant_collection=(args.allow_create_drop_qdrant_collection),
                )
            )
            result = {
                "artifact_path": reference.repository_relative_path,
                "canonical_sha256": reference.canonical_sha256,
                "exact_file_sha256": reference.exact_file_sha256,
                "phase": args.command,
                "status": "passed",
            }
        elif args.command == "assemble-release-evidence":
            reference = asyncio.run(assemble_release_evidence(ROOT))
            result = {
                "artifact_path": reference.repository_relative_path,
                "canonical_sha256": reference.canonical_sha256,
                "exact_file_sha256": reference.exact_file_sha256,
                "phase": args.command,
                "status": "passed",
            }
        else:
            reference = asyncio.run(
                verify_release_evidence(
                    ROOT,
                    require_live_database_absence=args.require_live_database_absence,
                    require_live_qdrant_absence=args.require_live_qdrant_absence,
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
