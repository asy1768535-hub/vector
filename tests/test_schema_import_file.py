from __future__ import annotations

import pytest

from app.schemas.schema_lifecycle import SchemaImportFileRequest
from app.services.schema_import_file import parse_schema_import_file
from app.services.schema_lifecycle_contracts import SchemaLifecycleError


@pytest.mark.parametrize(
    ("file_name", "content"),
    [
        (
            "finance.json",
            '{"version_key":"finance","entity_types":[{"key":"company","label":"Company"}]}',
        ),
        (
            "finance.yaml",
            "version_key: finance\nentity_types:\n  - key: company\n    label: Company\n",
        ),
        (
            "finance.yml",
            "version_key: finance\nentity_types:\n  - key: company\n    label: Company\n",
        ),
    ],
)
def test_schema_import_file_supports_json_yaml_and_yml(file_name: str, content: str):
    request = parse_schema_import_file(
        SchemaImportFileRequest(
            file_name=file_name,
            content=content,
            idempotency_key="import-file-1",
        )
    )

    assert request.version_key == "finance"
    assert request.entity_types[0].key == "company"
    assert request.idempotency_key == "import-file-1"


def test_schema_import_file_rejects_unknown_extension_and_invalid_yaml():
    with pytest.raises(SchemaLifecycleError) as extension_error:
        parse_schema_import_file(
            SchemaImportFileRequest(
                file_name="finance.toml",
                content="version_key = 'finance'",
                idempotency_key="import-file-2",
            )
        )
    assert extension_error.value.code == "schema_lifecycle_request_invalid"

    with pytest.raises(SchemaLifecycleError) as yaml_error:
        parse_schema_import_file(
            SchemaImportFileRequest(
                file_name="finance.yaml",
                content="version_key: [",
                idempotency_key="import-file-3",
            )
        )
    assert yaml_error.value.code == "schema_lifecycle_request_invalid"


def test_schema_import_file_converts_extraction_schema_shape():
    request = parse_schema_import_file(
        SchemaImportFileRequest(
            file_name="construction.yaml",
            content=(
                "schema_id: construction_process_kg_schema\n"
                "purpose: Construction graph\n"
                "entity_types:\n"
                "  - id: BusinessModule\n"
                "    label: Business Module\n"
                "    required_properties: [canonical_name]\n"
                "    properties:\n"
                "      canonical_name: string\n"
                "      module_code: enum[workbench, dashboard]\n"
                "  - id: Project\n"
                "    label: Project\n"
                "    properties:\n"
                "      name: string?\n"
                "relation_types:\n"
                "  - id: BELONGS_TO_PROJECT\n"
                "    source: BusinessModule\n"
                "    target: Project\n"
            ),
            idempotency_key="import-file-4",
        )
    )

    assert request.version_key == "construction_process_kg_schema"
    assert [item.key for item in request.entity_types] == ["business_module", "project"]
    assert request.attributes[1].value_type == "enum"
    assert request.attributes[1].enum_values == ["workbench", "dashboard"]
    assert request.constraints[0].source_entity_type_key == "business_module"
