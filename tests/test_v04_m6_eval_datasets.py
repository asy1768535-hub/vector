from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.graph_extraction_eval import (
    ENTERPRISE_EVAL_RELATION_CONSTRAINTS,
    load_graph_eval_dataset,
)
from app.services.graph_seed import expanded_default_relation_constraints


ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "eval/graph_extraction/manifests/development_smoke_v1.json"
RELEASE = ROOT / "eval/graph_extraction/manifests/release_v1.json"


def _load(path: Path):
    return load_graph_eval_dataset(repository_root=ROOT, manifest_path=path)


def test_smoke_manifest_is_a_fixed_ten_document_subset():
    loaded = _load(SMOKE)
    assert loaded.counts.model_dump() == {
        "documents": 10,
        "units": 40,
        "entities": 50,
        "relations": 40,
    }
    assert tuple(row.document_key for row in loaded.documents) == tuple(
        f"release-{index:03d}" for index in range(1, 11)
    )


def test_release_manifest_meets_every_frozen_cardinality():
    loaded = _load(RELEASE)
    assert loaded.counts.model_dump() == {
        "documents": 30,
        "units": 120,
        "entities": 150,
        "relations": 120,
    }
    assert loaded.dataset_manifest_sha256 == (
        "8cb812a33dac20745b78cca342a27e065684032a25e7adc020cc3d0ad19b5d22"
    )
    assert loaded.dataset_content_sha256 == (
        "6b316cbbcfc7357cb27bb0b8500e8c49ec041ce2d85c52715aba7108c3d6e96b"
    )


def test_release_source_is_sorted_unique_and_fully_synthetic():
    loaded = _load(RELEASE)
    assert [row.document_key for row in loaded.documents] == sorted(
        row.document_key for row in loaded.documents
    )
    for document in loaded.documents:
        assert document.security_level == "internal"
        assert document.title.startswith("Synthetic enterprise document ")
        assert all(unit.text.endswith("。") for unit in document.units)


def test_every_gold_relation_uses_a_valid_local_enterprise_constraint():
    loaded = _load(RELEASE)
    for document in loaded.documents:
        entities = {row.gold_id: row for row in document.gold_entities}
        for relation in document.gold_relations:
            assert relation.source_gold_id in entities
            assert relation.target_gold_id in entities
            assert len(relation.evidence_unit_keys) == 1


def test_offline_constraint_matrix_exactly_matches_the_runtime_enterprise_seed():
    runtime = {
        (
            row.relation_type_key,
            row.source_entity_type_key,
            row.target_entity_type_key,
        )
        for row in expanded_default_relation_constraints()
    }
    assert ENTERPRISE_EVAL_RELATION_CONSTRAINTS == runtime


def test_loader_rejects_an_undersized_release_manifest(tmp_path: Path):
    source_dir = tmp_path / "eval/graph_extraction"
    source_dir.mkdir(parents=True)
    source_dir.joinpath("release_v1.jsonl").write_text(
        (ROOT / "eval/graph_extraction/release_v1.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    manifest = json.loads(RELEASE.read_text(encoding="utf-8"))
    manifest["source_file"] = "eval/graph_extraction/release_v1.jsonl"
    manifest["document_keys"] = manifest["document_keys"][:1]
    manifest_path = tmp_path / "release.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="minimum documents"):
        load_graph_eval_dataset(repository_root=tmp_path, manifest_path=manifest_path)


def _write_single_document_fixture(
    tmp_path: Path,
    document: dict,
) -> Path:
    source_dir = tmp_path / "eval/graph_extraction"
    source_dir.mkdir(parents=True)
    source_dir.joinpath("fixture.jsonl").write_text(
        json.dumps(document, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "graph-extraction-eval-manifest-v1",
        "dataset_id": "fixture-v1",
        "source_file": "eval/graph_extraction/fixture.jsonl",
        "synthetic": True,
        "document_keys": [document["document_key"]],
        "minimums": {"documents": 1, "units": 1, "entities": 1, "relations": 1},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_loader_rejects_relation_outside_enterprise_ontology_constraint(
    tmp_path: Path,
):
    document = json.loads(
        (ROOT / "eval/graph_extraction/release_v1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    document["gold_relations"][0]["target_gold_id"] = "e-person"
    manifest = _write_single_document_fixture(tmp_path, document)
    with pytest.raises(ValueError, match="violates enterprise ontology constraint"):
        load_graph_eval_dataset(repository_root=tmp_path, manifest_path=manifest)


def test_loader_rejects_uuid_or_url_in_committed_synthetic_source(tmp_path: Path):
    document = json.loads(
        (ROOT / "eval/graph_extraction/release_v1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    document["title"] = "123e4567-e89b-12d3-a456-426614174000"
    manifest = _write_single_document_fixture(tmp_path, document)
    with pytest.raises(ValueError, match="UUID is forbidden"):
        load_graph_eval_dataset(repository_root=tmp_path, manifest_path=manifest)


def test_loader_rejects_unsorted_source_documents(tmp_path: Path):
    source_documents = [
        json.loads(line)
        for line in (ROOT / "eval/graph_extraction/release_v1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[:2]
    ]
    source_dir = tmp_path / "eval/graph_extraction"
    source_dir.mkdir(parents=True)
    source_dir.joinpath("fixture.jsonl").write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False) for row in reversed(source_documents)
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "graph-extraction-eval-manifest-v1",
        "dataset_id": "fixture-v1",
        "source_file": "eval/graph_extraction/fixture.jsonl",
        "synthetic": True,
        "document_keys": ["release-001", "release-002"],
        "minimums": {"documents": 2, "units": 8, "entities": 10, "relations": 8},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="source documents must be sorted"):
        load_graph_eval_dataset(repository_root=tmp_path, manifest_path=manifest_path)


def test_loader_rejects_manifest_outside_repository():
    manifest_path = ROOT.parent / "outside-eval-manifest.json"
    with pytest.raises(ValueError, match="inside the repository"):
        load_graph_eval_dataset(repository_root=ROOT, manifest_path=manifest_path)


def test_offline_validate_is_deterministic_and_does_not_emit_process_secret():
    env = dict(os.environ)
    env["GRAPH_EXTRACTION_API_KEY"] = "sk-must-not-appear-in-validation"
    command = [
        sys.executable,
        "scripts/graph_extraction_eval.py",
        "validate",
        "--manifest",
        str(RELEASE),
    ]
    first = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert first.stdout == second.stdout
    assert "sk-must-not-appear" not in first.stdout
    assert json.loads(first.stdout)["counts"]["relations"] == 120


def test_offline_validate_never_imports_settings_or_database_modules():
    source = (ROOT / "scripts/graph_extraction_eval.py").read_text(encoding="utf-8")
    assert '"app.config" in sys.modules' in source
    assert '"app.db" in sys.modules' in source
    assert "GRAPH_EXTRACTION_API_KEY" not in source
