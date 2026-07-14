"""Internal v0.4 graph extraction evaluation CLI."""
from __future__ import annotations

import argparse
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        return _validate(args.manifest)
    raise RuntimeError("unreachable graph Eval command")


if __name__ == "__main__":
    raise SystemExit(main())
