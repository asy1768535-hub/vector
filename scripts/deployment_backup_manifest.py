"""Create or verify a content-free deployment backup manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "vector-kb-backup-v1"
REQUIRED_ROLES = ("postgresql", "object_inventory", "qdrant_inventory")


class BackupManifestError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def create_manifest(files: dict[str, Path], output: Path) -> dict[str, object]:
    if set(files) != set(REQUIRED_ROLES):
        raise BackupManifestError("all required backup roles must be provided")
    artifacts = []
    output_root = output.resolve().parent
    for role in REQUIRED_ROLES:
        path = files[role].resolve()
        if not path.is_file():
            raise BackupManifestError(f"{role} artifact is missing")
        if path.parent != output_root:
            raise BackupManifestError("backup artifacts and manifest must share one directory")
        artifacts.append(
            {
                "role": role,
                "file": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _digest(path),
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_manifest(manifest_path: Path) -> dict[str, object]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupManifestError("manifest is unreadable") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BackupManifestError("manifest schema is unsupported")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise BackupManifestError("manifest artifacts are invalid")
    by_role = {
        item.get("role"): item for item in artifacts if isinstance(item, dict)
    }
    if set(by_role) != set(REQUIRED_ROLES):
        raise BackupManifestError("manifest backup roles are incomplete")
    root = manifest_path.resolve().parent
    for role in REQUIRED_ROLES:
        item = by_role[role]
        filename = item.get("file")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
        ):
            raise BackupManifestError(f"{role} filename is unsafe")
        path = root / filename
        if not path.is_file():
            raise BackupManifestError(f"{role} artifact is missing")
        if path.stat().st_size != item.get("size_bytes") or _digest(path) != item.get("sha256"):
            raise BackupManifestError(f"{role} artifact failed integrity verification")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--postgresql", type=Path, required=True)
    create.add_argument("--object-inventory", type=Path, required=True)
    create.add_argument("--qdrant-inventory", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            create_manifest(
                {
                    "postgresql": args.postgresql,
                    "object_inventory": args.object_inventory,
                    "qdrant_inventory": args.qdrant_inventory,
                },
                args.output,
            )
        else:
            verify_manifest(args.manifest)
    except BackupManifestError as exc:
        print(f"backup manifest rejected: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print("backup manifest OK")


if __name__ == "__main__":
    main()
