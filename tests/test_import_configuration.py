from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.api import import_uploads as import_uploads_api
from app.config import Settings
from app.schemas.documents import (
    ImportConfigurationRead,
    ImportConfigurationUpdate,
    ImportSessionCreate,
)
from app.services import import_uploads


DAILY_FILE_BYTES = 500 * 1024 * 1024
INITIAL_IMPORT_FILE_BYTES = 50 * 1024 * 1024 * 1024
INITIAL_IMPORT_FILE_COUNT = 100_000
DOC_FILE_BYTES = 200 * 1024 * 1024


def _settings() -> Settings:
    return Settings(_env_file=None)


def test_import_configuration_defaults_remain_daily_limits() -> None:
    result = import_uploads.import_configuration(_settings())

    assert result["max_file_bytes"] == DAILY_FILE_BYTES
    assert result["chunk_bytes"] == 32 * 1024 * 1024
    assert result["max_files_per_selection"] == 1000
    assert result["upload_concurrency"] == 1
    assert result["doc_max_file_bytes"] == DOC_FILE_BYTES
    assert result["max_configurable_file_bytes"] == INITIAL_IMPORT_FILE_BYTES
    assert result["max_configurable_files_per_selection"] == INITIAL_IMPORT_FILE_COUNT
    assert {
        ".cfg",
        ".conf",
        ".doc",
        ".htm",
        ".html",
        ".ini",
        ".log",
        ".pptx",
        ".rst",
        ".tsv",
        ".xls",
        ".xml",
        ".yaml",
        ".yml",
    }.issubset(result["allowed_extensions"])
    assert ImportConfigurationRead.model_validate(result).doc_max_file_bytes == DOC_FILE_BYTES


def test_doc_upload_has_a_separate_conversion_limit() -> None:
    library = SimpleNamespace(
        import_max_file_bytes=DAILY_FILE_BYTES,
        import_max_files_per_selection=1000,
    )
    accepted = ImportSessionCreate(
        batch_id=uuid.uuid4(),
        file_name="legacy.doc",
        size_bytes=DOC_FILE_BYTES,
    )

    assert import_uploads._validate_payload(accepted, _settings(), library=library) is None

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        import_uploads._validate_payload(
            accepted.model_copy(update={"size_bytes": DOC_FILE_BYTES + 1}),
            _settings(),
            library=library,
        )
    assert exc_info.value.code == "doc_too_large"
    assert exc_info.value.status_code == 413


def test_doc_replacement_stays_on_the_legacy_rejection_path() -> None:
    payload = ImportSessionCreate(
        batch_id=uuid.uuid4(),
        file_name="legacy.doc",
        size_bytes=1024,
        replace_document_id=uuid.uuid4(),
    )

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        import_uploads._validate_payload(payload, _settings())
    assert exc_info.value.code == "doc_replacement_unsupported"


def test_library_import_configuration_overrides_defaults() -> None:
    library = SimpleNamespace(
        import_max_file_bytes=100 * 1024 * 1024,
        import_max_files_per_selection=250,
    )

    result = import_uploads.import_configuration(_settings(), library=library)

    assert result["max_file_bytes"] == 100 * 1024 * 1024
    assert result["max_files_per_selection"] == 250


def test_maximum_library_limits_remain_effective() -> None:
    library = SimpleNamespace(
        import_max_file_bytes=INITIAL_IMPORT_FILE_BYTES,
        import_max_files_per_selection=INITIAL_IMPORT_FILE_COUNT,
    )

    assert import_uploads.effective_import_limits(library, _settings()) == (
        INITIAL_IMPORT_FILE_BYTES,
        INITIAL_IMPORT_FILE_COUNT,
    )


def test_payload_validation_uses_library_file_limit() -> None:
    library = SimpleNamespace(
        import_max_file_bytes=100 * 1024 * 1024,
        import_max_files_per_selection=250,
    )
    payload = ImportSessionCreate(
        batch_id=uuid.uuid4(),
        file_name="archive.pdf",
        size_bytes=100 * 1024 * 1024,
    )

    assert import_uploads._validate_payload(payload, _settings(), library=library) is None

    oversized = payload.model_copy(update={"size_bytes": 100 * 1024 * 1024 + 1})
    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        import_uploads._validate_payload(oversized, _settings(), library=library)
    assert exc_info.value.code == "file_too_large"
    assert exc_info.value.status_code == 413


def test_update_schema_accepts_initial_import_profile_and_rejects_higher_values() -> None:
    accepted = ImportConfigurationUpdate(
        max_file_bytes=INITIAL_IMPORT_FILE_BYTES,
        max_files_per_selection=INITIAL_IMPORT_FILE_COUNT,
    )
    assert accepted.max_file_bytes == INITIAL_IMPORT_FILE_BYTES
    assert accepted.max_files_per_selection == INITIAL_IMPORT_FILE_COUNT

    with pytest.raises(ValidationError):
        ImportConfigurationUpdate(
            max_file_bytes=INITIAL_IMPORT_FILE_BYTES + 1,
            max_files_per_selection=INITIAL_IMPORT_FILE_COUNT,
        )
    with pytest.raises(ValidationError):
        ImportConfigurationUpdate(
            max_file_bytes=INITIAL_IMPORT_FILE_BYTES,
            max_files_per_selection=INITIAL_IMPORT_FILE_COUNT + 1,
        )


def test_admin_update_persists_and_audits_library_limits(monkeypatch) -> None:
    library = SimpleNamespace(
        slug="public",
        import_max_file_bytes=None,
        import_max_files_per_selection=None,
    )
    actor = SimpleNamespace(id=uuid.uuid4())
    db = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(import_uploads_api.audit_log, "record", record)

    result = asyncio.run(
        import_uploads_api.update_import_configuration(
            ImportConfigurationUpdate(
                max_file_bytes=100 * 1024 * 1024,
                max_files_per_selection=250,
            ),
            lib=library,
            actor=actor,
            db=db,
        )
    )

    assert library.import_max_file_bytes == 100 * 1024 * 1024
    assert library.import_max_files_per_selection == 250
    assert result["max_file_bytes"] == 100 * 1024 * 1024
    assert result["max_files_per_selection"] == 250
    record.assert_awaited_once()
    db.commit.assert_awaited_once()


def test_staging_capacity_guard_is_bounded_but_holds_multiple_max_files() -> None:
    assert import_uploads.STAGING_LIBRARY_MAX_BYTES == 2 * 1024**4
    assert import_uploads.STAGING_LIBRARY_MAX_BYTES > INITIAL_IMPORT_FILE_BYTES


def test_doc_staging_reserves_space_for_original_and_converted_files() -> None:
    assert import_uploads.staging_reservation_bytes("legacy.doc", 100) == 200
    assert import_uploads.staging_reservation_bytes("native.docx", 100) == 100
