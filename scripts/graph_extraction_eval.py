"""Internal v0.4 graph extraction evaluation CLI."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.graph_extraction_eval import (  # noqa: E402
    assert_sanitized_eval_artifact,
    load_graph_eval_dataset,
)


def _validate(manifest: Path) -> int:
    if "app.config" in sys.modules or "app.db" in sys.modules:
        raise RuntimeError("offline validation loaded application Settings or database modules")
    loaded = load_graph_eval_dataset(
        repository_root=ROOT_DIR,
        manifest_path=manifest,
    )
    output = {
        "dataset_id": loaded.manifest.dataset_id,
        "synthetic": loaded.manifest.synthetic,
        "counts": loaded.counts.model_dump(mode="json"),
        "dataset_manifest_sha256": loaded.dataset_manifest_sha256,
        "dataset_content_sha256": loaded.dataset_content_sha256,
    }
    assert_sanitized_eval_artifact(output)
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or run isolated v0.4 graph extraction evaluation"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser(
        "validate",
        help="validate a synthetic dataset manifest without database or network access",
    )
    validate.add_argument("--manifest", type=Path, required=True)
    run = subparsers.add_parser(
        "run",
        help="run a real Provider evaluation in a fresh isolated PostgreSQL database",
    )
    run.add_argument(
        "--phase",
        choices=("development-smoke", "calibration", "post-freeze"),
        required=True,
    )
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--policy", type=Path)
    run.add_argument("--database", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--real-provider", action="store_true")
    run.add_argument("--confirm-external-send", required=True)
    run.add_argument("--retain-database", action="store_true")
    run.add_argument("--result", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.manifest)
    if args.command == "run":
        if not args.real_provider:
            raise ValueError("real Eval requires --real-provider")
        if not args.retain_database:
            raise ValueError("real Eval requires --retain-database through acceptance")
        if args.phase == "post-freeze" and args.policy is None:
            raise ValueError("post-freeze Eval requires --policy")
        if args.phase != "post-freeze" and args.policy is not None:
            raise ValueError("--policy is allowed only for post-freeze Eval")
        loaded = load_graph_eval_dataset(
            repository_root=ROOT_DIR,
            manifest_path=args.manifest,
        )
        output = args.result or (
            ROOT_DIR / "eval/graph_extraction/results" / f"{args.run_id}.json"
        )
        from app.services.graph_extraction_eval_runtime import execute_eval_run

        result = asyncio.run(
            execute_eval_run(
                repository_root=ROOT_DIR,
                loaded=loaded,
                database_name=args.database,
                run_id=args.run_id,
                phase=args.phase,
                workers=args.workers,
                confirmation=args.confirm_external_send,
                output_path=output,
                policy_path=args.policy,
            )
        )
        print(
            json.dumps(
                {
                    "run_id": result.artifact.run_id,
                    "status": result.artifact.status,
                    "result_path": str(result.output_path.relative_to(ROOT_DIR)),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0 if result.artifact.status == "passed" else 1
    raise RuntimeError("unreachable graph Eval command")


if __name__ == "__main__":
    raise SystemExit(main())
