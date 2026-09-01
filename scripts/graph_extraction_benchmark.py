"""Graph extraction performance benchmark CLI."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.graph_extraction_benchmark import (  # noqa: E402
    compare_graph_eval_runs,
    load_graph_eval_run,
    write_benchmark_comparison,
)
from app.services.graph_extraction_eval import (  # noqa: E402
    assert_sanitized_eval_artifact,
    load_graph_eval_dataset,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, run, or compare graph extraction performance"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-dataset")
    validate.add_argument("--manifest", type=Path, required=True)
    run = commands.add_parser("run")
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
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path)
    return parser


def _validate_dataset(manifest: Path) -> int:
    loaded = load_graph_eval_dataset(
        repository_root=ROOT_DIR,
        manifest_path=manifest,
    )
    output = {
        "dataset_id": loaded.manifest.dataset_id,
        "counts": loaded.counts.model_dump(mode="json"),
        "dataset_manifest_sha256": loaded.dataset_manifest_sha256,
        "dataset_content_sha256": loaded.dataset_content_sha256,
    }
    assert_sanitized_eval_artifact(output)
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


def _run(args: argparse.Namespace) -> int:
    if not args.real_provider:
        raise ValueError("real benchmark requires --real-provider")
    if not args.retain_database:
        raise ValueError("real benchmark requires --retain-database through acceptance")
    if args.phase == "post-freeze" and args.policy is None:
        raise ValueError("post-freeze benchmark requires --policy")
    if args.phase != "post-freeze" and args.policy is not None:
        raise ValueError("--policy is allowed only for post-freeze benchmark")
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


def _compare(args: argparse.Namespace) -> int:
    comparison = compare_graph_eval_runs(
        load_graph_eval_run(args.baseline),
        load_graph_eval_run(args.candidate),
    )
    if args.output is not None:
        write_benchmark_comparison(comparison, args.output)
    print(
        json.dumps(
            comparison.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if comparison.passed else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate-dataset":
        return _validate_dataset(args.manifest)
    if args.command == "run":
        return _run(args)
    if args.command == "compare":
        return _compare(args)
    raise RuntimeError("unreachable benchmark command")


if __name__ == "__main__":
    raise SystemExit(main())
