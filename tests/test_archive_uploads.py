import asyncio
import stat
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import import_uploads
from app.workers import importer


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ArchiveDb:
    def __init__(self, parent, library):
        self.execute_calls = []
        self.rows = {
            (import_uploads.DocumentImportJob, parent.id): parent,
            (import_uploads.Library, library.id): library,
        }

    async def get(self, model, row_id):
        return self.rows.get((model, row_id))

    def add(self, row):
        if isinstance(row, import_uploads.DocumentImportJob) and row.status is None:
            row.status = "uploading"
            row.current_stage = "uploading"
        self.rows[(type(row), row.id)] = row

    async def execute(self, _statement):
        self.execute_calls.append(_statement)
        return _Result(self.rows[next(iter(self.rows))].id)

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Lease:
    thread_stop_event = None

    def ensure_current(self):
        return None

    async def stop_renewal(self):
        return None


@asynccontextmanager
async def _claim_lease(*_args, **_kwargs):
    yield _Lease()


def test_archive_relative_path_keeps_archive_prefix_and_filename():
    assert import_uploads._archive_relative_path("docs/report.pdf", "bundle [ZIP]") == (
        "report.pdf",
        "bundle [ZIP]/docs/report.pdf",
    )


def test_archive_relative_path_rejects_traversal_and_absolute_names():
    assert import_uploads._archive_relative_path("../secret.txt", "bundle [ZIP]") is None
    assert import_uploads._archive_relative_path("/absolute.txt", "bundle [ZIP]") is None
    assert import_uploads._archive_relative_path("C:/absolute.txt", "bundle [ZIP]") is None
    assert import_uploads._archive_relative_path("docs\\..\\secret.txt", "bundle [ZIP]") is None


def test_archive_folder_name_is_visible_without_exposing_zip_as_a_document():
    assert import_uploads.archive_folder_name("年度资料.zip") == "年度资料 [ZIP]"
    assert ".zip" in import_uploads.ALLOWED_IMPORT_EXTENSIONS


def test_archive_entry_plan_keeps_supported_files_and_skips_unknown_types():
    supported = zipfile.ZipInfo("docs/report.pdf")
    supported.file_size = 20
    supported.compress_size = 10
    unknown = zipfile.ZipInfo("tools/run.exe")
    unknown.file_size = 20
    unknown.compress_size = 10
    metadata = zipfile.ZipInfo("__MACOSX/._report.pdf")
    metadata.file_size = 20
    metadata.compress_size = 10

    planned, skipped = import_uploads._validated_archive_entries(
        [supported, unknown, metadata],
        archive_folder="bundle [ZIP]",
        max_files=100,
        max_file_bytes=1024,
    )

    assert [(item[1], item[2]) for item in planned] == [
        ("report.pdf", "bundle [ZIP]/docs/report.pdf")
    ]
    assert skipped == ["tools/run.exe", "__MACOSX/._report.pdf"]


@pytest.mark.parametrize("unsafe", ["encrypted", "symlink"])
def test_archive_entry_plan_rejects_unsafe_entries(unsafe):
    info = zipfile.ZipInfo("docs/report.pdf")
    info.file_size = 20
    info.compress_size = 10
    if unsafe == "encrypted":
        info.flag_bits |= 0x1
    else:
        info.external_attr = (stat.S_IFLNK | 0o777) << 16

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        import_uploads._validated_archive_entries(
            [info],
            archive_folder="bundle [ZIP]",
            max_files=100,
            max_file_bytes=1024,
        )

    assert exc_info.value.code == "unsafe_archive"


def test_zip_expansion_creates_normal_child_jobs_with_saved_sources(
    tmp_path, monkeypatch
):
    parent_id = uuid.uuid4()
    library_id = uuid.uuid4()
    user_id = uuid.uuid4()
    parent = SimpleNamespace(
        id=parent_id,
        security_level="internal",
        graph_extraction_requested=True,
    )
    library = SimpleNamespace(
        id=library_id,
        deleted_at=None,
        import_max_file_bytes=1024 * 1024,
        import_max_files_per_selection=100,
    )
    db = _ArchiveDb(parent, library)
    archive_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("docs/readme.txt", "hello archive")
        archive.writestr("tools/run.exe", b"ignored")

    async def store_resource(*, resource, **_kwargs):
        locator = SimpleNamespace(
            object_key=resource.object_key,
            provider=resource.storage_provider,
            endpoint_ref=resource.endpoint_ref,
            bucket=resource.bucket,
            object_version=None,
            etag="etag",
            immutability_mode="content_hash",
        )
        return SimpleNamespace(locator=locator, verified_at=datetime.now(timezone.utc))

    monkeypatch.setattr(import_uploads, "store_file_resource_object", store_resource)
    config = SimpleNamespace(
        import_staging_dir=str(tmp_path / "staging"),
        import_staging_max_file_bytes=1024 * 1024,
        import_selection_max_files=100,
    )
    claim = import_uploads.UploadOperationClaim(
        job_id=parent_id,
        owner_token="upload:test:complete:token",
        operation="complete",
        staging_key=f"{parent_id.hex}.upload",
        file_name="bundle.zip",
        size_bytes=archive_path.stat().st_size,
        upload_offset=archive_path.stat().st_size,
        library_id=library_id,
        uploaded_by_user_id=user_id,
    )
    adapter = SimpleNamespace(
        provider="local",
        endpoint_ref="primary",
        bucket=None,
    )

    count = asyncio.run(
        import_uploads._expand_zip_upload(
            db,
            claim=claim,
            path=archive_path,
            adapter=adapter,
            config=config,
        )
    )

    child_id = import_uploads._archive_child_job_id(
        parent_id, "bundle [ZIP]/docs/readme.txt"
    )
    child = asyncio.run(db.get(import_uploads.DocumentImportJob, child_id))
    resource = asyncio.run(
        db.get(
            import_uploads.FileResource,
            import_uploads.resource_id_for_upload_context(child_id),
        )
    )
    assert count == 1
    assert child.relative_path == "bundle [ZIP]/docs/readme.txt"
    assert child.status == "uploading"
    assert child.graph_extraction_requested is True
    assert child.security_level == "internal"
    assert resource.storage_status == "available"
    assert resource.relative_path == child.relative_path
    activate_values = db.execute_calls[-2].compile().params
    parent_values = db.execute_calls[-1].compile().params
    assert activate_values["status"] == "queued"
    assert activate_values["current_stage"] == "queued"
    assert parent_values["status"] == "succeeded"
    assert parent_values["result_operation"] == "archive_expanded"
    assert parent_values["graph_extraction_requested"] is False


def test_zip_completion_only_saves_and_queues_parent(
    tmp_path, monkeypatch
):
    parent_id = uuid.uuid4()
    library_id = uuid.uuid4()
    user_id = uuid.uuid4()
    archive_path = tmp_path / f"{parent_id.hex}.upload"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("readme.txt", "hello")
    parent = SimpleNamespace(
        id=parent_id,
        security_level="internal",
        graph_extraction_requested=True,
    )
    library = SimpleNamespace(id=library_id, deleted_at=None)
    db = _ArchiveDb(parent, library)
    claim = import_uploads.UploadOperationClaim(
        job_id=parent_id,
        owner_token="upload:test:complete:token",
        operation="complete",
        staging_key=archive_path.name,
        file_name="bundle.zip",
        size_bytes=archive_path.stat().st_size,
        upload_offset=archive_path.stat().st_size,
        library_id=library_id,
        uploaded_by_user_id=user_id,
    )
    adapter = SimpleNamespace(
        provider="local",
        endpoint_ref="primary",
        bucket=None,
    )

    async def store_resource(*, resource, **_kwargs):
        locator = SimpleNamespace(
            object_key=resource.object_key,
            provider=resource.storage_provider,
            endpoint_ref=resource.endpoint_ref,
            bucket=resource.bucket,
            object_version=None,
            etag="etag",
            immutability_mode="content_hash",
        )
        return SimpleNamespace(locator=locator, verified_at=datetime.now(timezone.utc))

    monkeypatch.setattr(import_uploads, "keep_upload_claim_alive", _claim_lease)
    monkeypatch.setattr(import_uploads, "build_object_storage_adapter", lambda _config: adapter)
    monkeypatch.setattr(import_uploads, "store_file_resource_object", store_resource)
    config = SimpleNamespace(
        import_staging_dir=str(tmp_path),
        import_upload_retry_after_seconds=2,
        import_upload_claim_heartbeat_seconds=30,
    )

    asyncio.run(import_uploads.complete_claimed_upload(db, claim=claim, config=config))

    values = db.execute_calls[-1].compile().params
    assert values["status"] == "queued"
    assert values["current_stage"] == "queued"
    assert "result_operation" not in values
    assert archive_path.exists()


def test_import_worker_routes_zip_to_archive_expansion(monkeypatch):
    job_id = uuid.uuid4()
    calls = []

    class Db:
        async def get(self, _model, _job_id):
            return SimpleNamespace(file_name="bundle.zip")

    @asynccontextmanager
    async def session_factory():
        yield Db()

    async def process_archive(actual_job_id):
        calls.append(actual_job_id)

    async def reject_parser_stage(*_args):
        raise AssertionError("ZIP must not enter the document parser")

    monkeypatch.setattr(importer, "async_session_factory", session_factory)
    monkeypatch.setattr(importer, "_process_archive_job", process_archive)
    monkeypatch.setattr(importer, "_set_stage", reject_parser_stage)

    asyncio.run(importer._process_claimed_job(job_id))

    assert calls == [job_id]
