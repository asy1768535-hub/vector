from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import documents, me
from app.auth.backend import current_active_user
from app.db import get_db
from app.main import app
from app.services import import_uploads, knowledge_catalog
from app.services.knowledge_catalog_contracts import (
    CatalogDocumentQuery,
    catalog_filter_fingerprint,
)

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def test_personal_task_list_accepts_explicit_numeric_page_size():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=True)

    async def override_user():
        return user

    async def override_db():
        return SimpleNamespace()

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(me, "_personal_task_libraries", new=AsyncMock(return_value={})),
            patch.object(
                import_uploads,
                "list_personal_import_tasks",
                new=AsyncMock(return_value=SimpleNamespace(jobs=(), next_cursor=None)),
            ),
            patch.object(import_uploads, "personal_task_projections", new=AsyncMock(return_value=[])),
        ):
            response = TestClient(app).get("/me/import-tasks?scope=30d&limit=20")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


def test_personal_files_api_lists_only_the_selected_library_directory():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=False)
    library = SimpleNamespace(id=uuid.uuid4(), slug="legal", name="法务资料")
    folder = SimpleNamespace(id=uuid.uuid4(), name="合同", path="/项目甲/合同")
    document = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        title="主合同.pdf",
        source_path="/项目甲/合同/主合同.pdf",
        created_at=NOW,
    )

    async def override_user():
        return user

    async def override_db():
        return SimpleNamespace()

    page = SimpleNamespace(
        path="/项目甲",
        folders=(folder,),
        files=(document,),
        folder_total=1,
        file_total=1,
        page=1,
        page_size=50,
    )
    list_files = AsyncMock(return_value=page)
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(
                me,
                "_personal_task_libraries",
                new=AsyncMock(return_value={library.id: library}),
            ),
            patch.object(
                import_uploads,
                "list_personal_files",
                new=list_files,
                create=True,
            ),
        ):
            response = TestClient(app).get(
                "/me/files?library_slug=legal&path=/项目甲&page=1&page_size=50"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {
        "library_slug": "legal",
        "path": "/项目甲",
        "folders": [
            {
                "id": str(folder.id),
                "name": "合同",
                "path": "/项目甲/合同",
            }
        ],
        "files": [
            {
                "document_id": str(document.id),
                "file_name": "主合同.pdf",
                "created_at": NOW.isoformat().replace("+00:00", "Z"),
            }
        ],
        "folder_total": 1,
        "file_total": 1,
        "page": 1,
        "page_size": 50,
    }
    list_files.assert_awaited_once_with(
        SimpleNamespace(),
        user_id=user.id,
        library_id=library.id,
        path="/项目甲",
        page=1,
        page_size=50,
    )


def test_stored_files_api_lists_saved_media_before_knowledge_processing():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=False)
    library = SimpleNamespace(id=uuid.uuid4(), slug="legal", name="法务资料")
    folder = SimpleNamespace(name="项目甲", path="/项目甲", file_total=1)
    stored = SimpleNamespace(
        file_resource_id=uuid.uuid4(),
        document_id=None,
        file_name="现场录音.mp3",
        relative_path="项目甲/现场录音.mp3",
        content_type="audio/mpeg",
        size_bytes=2048,
        storage_status="available",
        processing_status="succeeded",
        processing_stage="completed",
        result_operation="stored_only",
        created_at=NOW,
    )
    page = SimpleNamespace(
        path="",
        folders=(folder,),
        files=(stored,),
        folder_total=1,
        file_total=1,
        page=1,
        page_size=50,
    )

    async def override_user():
        return user

    async def override_db():
        return SimpleNamespace()

    list_stored = AsyncMock(return_value=page)
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(me, "_personal_task_libraries", new=AsyncMock(return_value={library.id: library})),
            patch.object(import_uploads, "list_stored_files", new=list_stored, create=True),
        ):
            response = TestClient(app).get("/me/stored-files?library_slug=legal")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["files"] == [{
        "file_resource_id": str(stored.file_resource_id),
        "document_id": None,
        "file_name": "现场录音.mp3",
        "relative_path": "项目甲/现场录音.mp3",
        "content_type": "audio/mpeg",
        "size_bytes": 2048,
        "storage_status": "available",
        "processing_status": "succeeded",
        "processing_stage": "completed",
        "result_operation": "stored_only",
        "created_at": NOW.isoformat().replace("+00:00", "Z"),
    }]
    list_stored.assert_awaited_once_with(
        SimpleNamespace(),
        user_id=user.id,
        library_id=library.id,
        path="",
        page=1,
        page_size=50,
    )


def test_stored_file_download_api_returns_a_short_lived_url_for_the_owner():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=False)
    library = SimpleNamespace(id=uuid.uuid4(), slug="legal", name="法务资料")
    resource_id = uuid.uuid4()
    signed_url = AsyncMock(return_value="https://minio.example.test/download?signature=redacted")

    async def override_user():
        return user

    async def override_db():
        return SimpleNamespace()

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(me, "_personal_task_libraries", new=AsyncMock(return_value={library.id: library})),
            patch.object(
                import_uploads,
                "owned_stored_file_download_url",
                new=signed_url,
                create=True,
            ),
        ):
            response = TestClient(app).get(
                f"/me/stored-files/{resource_id}/download?library_slug=legal"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {
        "url": "https://minio.example.test/download?signature=redacted",
        "expires_in_seconds": 300,
    }
    signed_url.assert_awaited_once_with(
        SimpleNamespace(),
        user_id=user.id,
        library_id=library.id,
        file_resource_id=resource_id,
    )


def test_stored_only_file_delete_api_queues_owner_scoped_deletion():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=False)
    library = SimpleNamespace(id=uuid.uuid4(), slug="legal", name="法务资料")
    resource_id = uuid.uuid4()
    request_delete = AsyncMock()
    db = AsyncMock()

    async def override_user():
        return user

    async def override_db():
        return db

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(me, "_personal_task_libraries", new=AsyncMock(return_value={library.id: library})),
            patch.object(
                import_uploads,
                "request_owned_stored_file_delete",
                new=request_delete,
                create=True,
            ),
        ):
            response = TestClient(app).delete(
                f"/me/stored-files/{resource_id}?library_slug=legal"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202, response.text
    assert response.json() == {"status": "deleting"}
    request_delete.assert_awaited_once_with(
        db,
        user_id=user.id,
        library=library,
        file_resource_id=resource_id,
    )
    db.commit.assert_awaited_once()


def test_stored_only_file_delete_fences_the_resource_and_enqueues_cleanup():
    user_id = uuid.uuid4()
    library = SimpleNamespace(id=uuid.uuid4(), qdrant_collection="legal")
    resource = SimpleNamespace(id=uuid.uuid4(), storage_status="available")
    job = SimpleNamespace(document_id=None, status="succeeded")
    owned = AsyncMock(return_value=(job, resource))
    enqueue = AsyncMock()

    async def run():
        with (
            patch.object(import_uploads, "_owned_stored_file", new=owned),
            patch.object(import_uploads.cleanup_service, "enqueue_delete_file_resource", new=enqueue),
        ):
            await import_uploads.request_owned_stored_file_delete(
                SimpleNamespace(),
                user_id=user_id,
                library=library,
                file_resource_id=resource.id,
            )

    asyncio.run(run())

    assert resource.storage_status == "deleting"
    owned.assert_awaited_once_with(
        ANY,
        user_id=user_id,
        library_id=library.id,
        file_resource_id=resource.id,
        for_update=True,
    )
    enqueue.assert_awaited_once_with(ANY, library, resource.id)


@pytest.mark.parametrize("job", [
    SimpleNamespace(document_id=uuid.uuid4(), status="succeeded"),
    SimpleNamespace(document_id=None, status="processing"),
])
def test_stored_file_delete_rejects_processed_or_active_files(job):
    resource = SimpleNamespace(id=uuid.uuid4(), storage_status="available")

    async def run():
        with patch.object(
            import_uploads,
            "_owned_stored_file",
            new=AsyncMock(return_value=(job, resource)),
        ):
            await import_uploads.request_owned_stored_file_delete(
                SimpleNamespace(),
                user_id=uuid.uuid4(),
                library=SimpleNamespace(id=uuid.uuid4(), qdrant_collection="legal"),
                file_resource_id=resource.id,
            )

    with pytest.raises(import_uploads.ImportUploadError):
        asyncio.run(run())
    assert resource.storage_status == "available"


def test_personal_files_api_rejects_a_library_without_upload_permission():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=False)
    list_files = AsyncMock()

    async def override_user():
        return user

    async def override_db():
        return SimpleNamespace()

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(me, "_personal_task_libraries", new=AsyncMock(return_value={})),
            patch.object(
                import_uploads,
                "list_personal_files",
                new=list_files,
                create=True,
            ),
        ):
            response = TestClient(app).get("/me/files?library_slug=private")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    list_files.assert_not_awaited()


def test_personal_file_folder_delete_is_scoped_to_the_current_user_and_library():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=False)
    library = SimpleNamespace(id=uuid.uuid4(), slug="legal", name="法务资料")
    delete_folder = AsyncMock(return_value=(3, True))
    lock_library = AsyncMock(return_value=library)

    async def override_user():
        return user

    db = AsyncMock()

    async def override_db():
        return db

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(me, "_personal_task_libraries", new=AsyncMock(return_value={library.id: library})),
            patch.object(me, "_lock_writable", new=lock_library, create=True),
            patch.object(import_uploads, "delete_personal_file_folder", new=delete_folder, create=True),
        ):
            response = TestClient(app).delete(
                "/me/files/folder?library_slug=legal&path=/项目甲"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_count": 3, "folder_deleted": True}
    lock_library.assert_awaited_once()
    delete_folder.assert_awaited_once_with(
        db,
        user_id=user.id,
        library=library,
        path="/项目甲",
    )


def test_personal_files_api_requires_login():
    response = TestClient(app).get("/me/files?library_slug=legal")

    assert response.status_code in {401, 403}


def test_personal_task_files_api_combines_directories_with_safe_task_details():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=False)
    library = SimpleNamespace(id=uuid.uuid4(), slug="legal", name="法务资料")
    folder = SimpleNamespace(name="项目甲", path="/项目甲", file_total=2)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        library_id=library.id,
        requested_by_user_id=user.id,
        replace_document_id=None,
        file_name="根目录说明.md",
        relative_path="根目录说明.md",
        created_at=NOW,
    )
    page = SimpleNamespace(
        path="",
        folders=(folder,),
        jobs=(job,),
        folder_total=1,
        file_total=1,
        page=1,
        page_size=20,
    )

    async def override_user():
        return user

    async def override_db():
        return SimpleNamespace()

    list_task_files = AsyncMock(return_value=page)
    projections = AsyncMock(return_value=[{
        "status": "processing",
        "current_stage": "embedding",
        "created_at": NOW,
        "finished_at": None,
    }])
    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(
                me,
                "_personal_task_libraries",
                new=AsyncMock(return_value={library.id: library}),
            ),
            patch.object(
                import_uploads,
                "list_personal_import_task_files",
                new=list_task_files,
                create=True,
            ),
            patch.object(
                import_uploads,
                "personal_task_projections",
                new=projections,
            ),
        ):
            response = TestClient(app).get(
                "/me/import-task-files?library_slug=legal&scope=30d&page=1&page_size=20"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["library_slug"] == "legal"
    assert body["path"] == ""
    assert body["folders"] == [{"name": "项目甲", "path": "/项目甲", "file_total": 2}]
    assert body["items"][0]["file_name"] == "根目录说明.md"
    assert body["items"][0]["status"] == "processing"
    assert body["items"][0]["stage"] == "embedding"
    assert body["folder_total"] == 1
    assert body["file_total"] == 1
    list_task_files.assert_awaited_once_with(
        SimpleNamespace(),
        user_id=user.id,
        library_id=library.id,
        scope="30d",
        path="",
        page=1,
        page_size=20,
    )


def test_personal_task_files_api_rejects_a_library_without_upload_permission():
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_superuser=False)
    list_task_files = AsyncMock()

    async def override_user():
        return user

    async def override_db():
        return SimpleNamespace()

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with (
            patch.object(me, "_personal_task_libraries", new=AsyncMock(return_value={})),
            patch.object(
                import_uploads,
                "list_personal_import_task_files",
                new=list_task_files,
                create=True,
            ),
        ):
            response = TestClient(app).get(
                "/me/import-task-files?library_slug=private&scope=all"
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    list_task_files.assert_not_awaited()


@pytest.mark.parametrize("path", ["../secret", "/项目甲/../secret", r"项目甲\secret"])
def test_personal_file_directory_rejects_unsafe_paths(path: str):
    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        import_uploads.normalize_personal_file_path(path)

    assert exc_info.value.code == "personal_file_path_invalid"


def test_personal_files_service_bounds_the_selected_directory_and_owner():
    user_id = uuid.uuid4()
    library_id = uuid.uuid4()
    current_folder = SimpleNamespace(id=uuid.uuid4(), path="/项目甲")
    child_folders = tuple(
        SimpleNamespace(id=uuid.uuid4(), name=f"子目录{index}", path=f"/项目甲/子目录{index}")
        for index in range(5)
    )
    document = SimpleNamespace(
        id=uuid.uuid4(),
        display_name=None,
        title="说明.txt",
        source_path="/项目甲/说明.txt",
        created_at=NOW,
    )

    class Result:
        def __init__(self, *, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = list(rows)

        def scalar_one_or_none(self):
            return self.scalar

        def scalar_one(self):
            return self.scalar

        def scalars(self):
            return self

        def all(self):
            return self.rows

    class Db:
        def __init__(self):
            self.statements = []
            self.results = iter(
                [
                    Result(scalar=current_folder),
                    Result(scalar=25),
                    Result(rows=child_folders),
                    Result(scalar=30),
                    Result(rows=[document] * 15),
                ]
            )

        async def execute(self, statement):
            self.statements.append(statement)
            return next(self.results)

    db = Db()
    page = asyncio.run(
        import_uploads.list_personal_files(
            db,
            user_id=user_id,
            library_id=library_id,
            path="/项目甲",
            page=2,
            page_size=20,
        )
    )

    assert page.path == "/项目甲"
    assert page.folders == child_folders
    assert page.files == (document,) * 15
    assert page.folder_total == 25
    assert page.file_total == 30
    assert len(page.folders) + len(page.files) == 20
    assert len(db.statements) == 5
    folder_sql = str(db.statements[2]).lower()
    file_sql = str(db.statements[4]).lower()
    assert "folders.parent_id" in folder_sql
    assert "exists" in folder_sql
    assert "documents.created_by" in folder_sql
    assert " like " not in folder_sql
    assert "documents.folder_id" in file_sql
    assert "documents.created_by" in file_sql
    assert "documents.library_id" in file_sql
    assert "documents.status" in file_sql
    assert db.statements[2]._limit_clause.value == 20
    assert db.statements[2]._offset_clause.value == 20
    assert db.statements[4]._limit_clause.value == 15
    assert db.statements[4]._offset_clause.value == 0


def test_stored_files_service_scopes_resources_to_the_uploading_user():
    user_id = uuid.uuid4()
    library_id = uuid.uuid4()
    resource = SimpleNamespace(
        id=uuid.uuid4(),
        file_name="现场录音.mp3",
        relative_path="项目甲/现场录音.mp3",
        content_type="audio/mpeg",
        size_bytes=2048,
        storage_status="available",
        created_at=NOW,
    )
    job = SimpleNamespace(
        document_id=None,
        status="succeeded",
        current_stage="completed",
        result_operation="stored_only",
    )

    class Result:
        def __init__(self, *, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = list(rows)

        def scalar_one(self):
            return self.scalar

        def all(self):
            return self.rows

    class Db:
        def __init__(self):
            self.statements = []
            self.results = iter([
                Result(scalar=1),
                Result(rows=[("项目甲", 1)]),
                Result(scalar=1),
                Result(rows=[(job, resource)]),
            ])

        async def execute(self, statement):
            self.statements.append(statement)
            return next(self.results)

    db = Db()
    page = asyncio.run(
        import_uploads.list_stored_files(
            db,
            user_id=user_id,
            library_id=library_id,
            path="",
            page=1,
            page_size=50,
        )
    )

    assert page.files[0].file_resource_id == resource.id
    assert page.files[0].document_id is None
    sql = "\n".join(str(statement).lower() for statement in db.statements)
    assert "file_resources.uploaded_by_user_id" in sql
    assert "document_import_jobs.requested_by_user_id" in sql


def test_delete_personal_file_folder_tombstones_only_owned_documents():
    user_id = uuid.uuid4()
    library = SimpleNamespace(id=uuid.uuid4(), qdrant_collection="personal-files")
    root = SimpleNamespace(id=uuid.uuid4(), path="/项目甲")
    child = SimpleNamespace(id=uuid.uuid4(), path="/项目甲/合同")
    document_ids = [uuid.uuid4(), uuid.uuid4()]

    class Result:
        def __init__(self, *, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = list(rows)

        def scalar_one_or_none(self):
            return self.scalar

        def scalars(self):
            return self

        def all(self):
            return self.rows

        def first(self):
            return self.scalar

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        Result(rows=[root, child]),
        Result(rows=[]),
        Result(rows=document_ids),
        Result(),
        Result(),
        Result(),
        Result(),
    ])
    cleanup = AsyncMock()

    async def run():
        with patch("app.services.import_uploads.cleanup_service.enqueue_delete_document", new=cleanup):
            return await import_uploads.delete_personal_file_folder(
                db,
                user_id=user_id,
                library=library,
                path="/项目甲",
            )

    deleted_count, folder_deleted = asyncio.run(run())

    assert deleted_count == 2
    assert folder_deleted is True
    assert cleanup.await_count == 2
    assert [call.args[2] for call in cleanup.await_args_list] == document_ids
    assert db.execute.await_count == 7
    document_sql = str(db.execute.await_args_list[2].args[0]).lower()
    folder_sql = str(db.execute.await_args_list[6].args[0]).lower()
    assert "documents.created_by" in document_sql
    assert "documents.folder_id in" in document_sql
    assert "folders.id in" in folder_sql


def test_delete_personal_file_folder_keeps_shared_folder_rows():
    user_id = uuid.uuid4()
    library = SimpleNamespace(id=uuid.uuid4(), qdrant_collection="personal-files")
    root = SimpleNamespace(id=uuid.uuid4(), path="/项目甲")
    document_id = uuid.uuid4()

    class Result:
        def __init__(self, *, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = list(rows)

        def scalars(self):
            return self

        def all(self):
            return self.rows

        def first(self):
            return self.scalar

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        Result(rows=[root]),
        Result(rows=[]),
        Result(rows=[document_id]),
        Result(),
        Result(),
        Result(scalar=uuid.uuid4()),
    ])
    cleanup = AsyncMock()

    async def run():
        with patch("app.services.import_uploads.cleanup_service.enqueue_delete_document", new=cleanup):
            return await import_uploads.delete_personal_file_folder(
                db,
                user_id=user_id,
                library=library,
                path="/项目甲",
            )

    deleted_count, folder_deleted = asyncio.run(run())

    assert deleted_count == 1
    assert folder_deleted is False
    assert cleanup.await_count == 1
    assert db.execute.await_count == 6


def test_delete_personal_file_folder_deletes_storage_only_resources_without_folder_rows():
    user_id = uuid.uuid4()
    library = SimpleNamespace(id=uuid.uuid4(), qdrant_collection="personal-files")
    resource = SimpleNamespace(id=uuid.uuid4(), storage_status="available")
    job = SimpleNamespace(document_id=None, status="succeeded")

    class Result:
        def __init__(self, *, rows=()):
            self.rows = list(rows)

        def scalars(self):
            return self

        def all(self):
            return self.rows

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        Result(rows=[]),
        Result(rows=[(job, resource)]),
    ])
    cleanup = AsyncMock()

    async def run():
        with patch("app.services.import_uploads.cleanup_service.enqueue_delete_file_resource", new=cleanup):
            return await import_uploads.delete_personal_file_folder(
                db,
                user_id=user_id,
                library=library,
                path="/项目甲",
            )

    deleted_count, folder_deleted = asyncio.run(run())

    assert (deleted_count, folder_deleted) == (1, True)
    assert resource.storage_status == "deleting"
    cleanup.assert_awaited_once_with(db, library, resource.id)
    resource_sql = str(db.execute.await_args_list[1].args[0]).lower()
    assert "document_import_jobs.requested_by_user_id" in resource_sql
    assert "file_resources.uploaded_by_user_id" in resource_sql


def test_delete_personal_file_folder_rejects_processing_storage_only_resource():
    user_id = uuid.uuid4()
    library = SimpleNamespace(id=uuid.uuid4(), qdrant_collection="personal-files")
    resource = SimpleNamespace(id=uuid.uuid4(), storage_status="available")
    job = SimpleNamespace(document_id=None, status="processing")

    class Result:
        def __init__(self, *, rows=()):
            self.rows = list(rows)

        def scalars(self):
            return self

        def all(self):
            return self.rows

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        Result(rows=[]),
        Result(rows=[(job, resource)]),
    ])
    cleanup = AsyncMock()

    async def run():
        with patch("app.services.import_uploads.cleanup_service.enqueue_delete_file_resource", new=cleanup):
            with pytest.raises(import_uploads.ImportUploadError) as exc_info:
                await import_uploads.delete_personal_file_folder(
                    db,
                    user_id=user_id,
                    library=library,
                    path="/项目甲",
                )
        return exc_info.value

    error = asyncio.run(run())

    assert error.code == "stored_file_processing_active"
    assert resource.storage_status == "available"
    cleanup.assert_not_awaited()


def test_personal_task_file_service_keeps_all_states_in_the_selected_owner_directory():
    user_id = uuid.uuid4()
    library_id = uuid.uuid4()
    failed = SimpleNamespace(
        id=uuid.uuid4(),
        file_name="失败.pdf",
        status="failed",
        created_at=NOW,
    )
    processing = SimpleNamespace(
        id=uuid.uuid4(),
        file_name="处理中.docx",
        status="processing",
        created_at=NOW,
    )

    class Result:
        def __init__(self, *, scalar=None, rows=()):
            self.scalar = scalar
            self.rows = list(rows)

        def scalar_one(self):
            return self.scalar

        def all(self):
            return self.rows

        def scalars(self):
            return self

    class Db:
        def __init__(self):
            self.statements = []
            self.results = iter([
                Result(scalar=1),
                Result(rows=[("合同", 2)]),
                Result(scalar=2),
                Result(rows=[failed, processing]),
            ])

        async def execute(self, statement):
            self.statements.append(statement)
            return next(self.results)

    db = Db()
    page = asyncio.run(
        import_uploads.list_personal_import_task_files(
            db,
            user_id=user_id,
            library_id=library_id,
            scope="all",
            path="/项目甲",
            page=1,
            page_size=20,
        )
    )

    assert page.path == "/项目甲"
    assert [(folder.name, folder.path, folder.file_total) for folder in page.folders] == [
        ("合同", "/项目甲/合同", 2)
    ]
    assert page.jobs == (failed, processing)
    assert page.folder_total == 1
    assert page.file_total == 2
    assert len(db.statements) == 4
    for statement in db.statements:
        where_sql = str(statement.whereclause).lower()
        assert "requested_by_user_id" in where_sql
        assert "library_id" in where_sql
    file_where_sql = str(db.statements[3].whereclause).lower()
    assert "document_import_jobs.status" not in file_where_sql
    assert "项目甲/" in {
        value
        for statement in db.statements
        for value in statement.compile().params.values()
        if isinstance(value, str)
    }


def _import_job(*, requested_by_user_id: uuid.UUID | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        requested_by_user_id=requested_by_user_id or uuid.uuid4(),
        replace_document_id=None,
        file_name="incident-report.pdf",
        created_at=NOW,
        finished_at=NOW,
    )


def test_personal_task_projection_hides_worker_error_and_exposes_chinese_retry_state():
    job = _import_job()
    result = import_uploads.personal_task_projection(
        job,
        library_name="合规资料",
        library_slug="compliance",
        projection={
            "status": "failed",
            "current_stage": "embedding",
            "retry_target_type": "embedding",
            "retry_target_id": uuid.uuid4(),
            "last_error": "provider trace: token=secret-value",
            "created_at": NOW,
            "finished_at": NOW,
        },
    )

    assert result["library_name"] == "合规资料"
    assert result["status"] == "failed"
    assert result["stage"] == "embedding"
    assert result["failure_message"] == "知识内容处理失败"
    assert result["failure_action"] == "稍后重试任务"
    assert result["can_retry"] is True
    assert result["operation_type"] == "import"
    assert "last_error" not in result
    assert "secret-value" not in str(result)
    assert "token=" not in str(result)


def test_personal_task_lookup_rejects_a_job_owned_by_another_user():
    owner_id = uuid.uuid4()
    foreign_job = _import_job(requested_by_user_id=uuid.uuid4())

    class Result:
        def scalar_one_or_none(self):
            return foreign_job

    class Db:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return Result()

    db = Db()
    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(
            import_uploads.get_personal_import_task(
                db,
                job_id=foreign_job.id,
                user_id=owner_id,
                library_ids={foreign_job.library_id},
                for_update=True,
            )
        )

    assert exc_info.value.code == "job_not_found"
    compiled = str(db.statements[0]).lower()
    assert "requested_by_user_id" in compiled
    assert "for update" in compiled


def test_personal_task_cursor_cannot_cross_the_selected_time_scope():
    cursor = import_uploads.encode_personal_import_task_cursor(
        import_uploads.PersonalImportTaskCursor(
            created_at=NOW,
            job_id=uuid.uuid4(),
            scope="30d",
        )
    )

    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        import_uploads.decode_personal_import_task_cursor(cursor, scope="all")

    assert exc_info.value.code == "personal_task_cursor_invalid"


@pytest.mark.parametrize(
    "deleted_at,latest_revision_matches",
    [
        (NOW, True),
        (None, False),
    ],
)
def test_personal_retry_never_requeues_a_deleted_or_stale_embedding_revision(
    monkeypatch: pytest.MonkeyPatch,
    deleted_at: datetime | None,
    latest_revision_matches: bool,
):
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    embedding_id = uuid.uuid4()
    root_job = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        embedding_job_id=embedding_id,
        status="processing",
        current_stage="embedding",
    )
    embedding = SimpleNamespace(
        id=embedding_id,
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        document_revision=3,
        status="failed",
        attempt_count=7,
        worker_id="worker-internal",
        claimed_at=NOW,
        finished_at=NOW,
        last_error="provider trace: token=secret-value",
    )
    document = SimpleNamespace(
        id=document_id,
        library_id=library_id,
        deleted_at=deleted_at,
        latest_revision_id=revision_id if latest_revision_matches else uuid.uuid4(),
        current_revision=3,
    )

    class Result:
        def __init__(self, row):
            self.row = row

        def scalar_one_or_none(self):
            return self.row

    class Db:
        def __init__(self):
            self.rows = [embedding, document]
            self.flush_calls = 0

        async def execute(self, _statement):
            return Result(self.rows.pop(0))

        async def flush(self):
            self.flush_calls += 1

    async def failed_embedding_projection(_db, _jobs):
        return [
            {
                "status": "failed",
                "current_stage": "embedding",
                "retry_target_type": "embedding",
                "retry_target_id": embedding_id,
            }
        ]

    monkeypatch.setattr(
        import_uploads,
        "personal_task_projections",
        failed_embedding_projection,
    )
    db = Db()
    with pytest.raises(import_uploads.ImportUploadError) as exc_info:
        asyncio.run(import_uploads.retry_personal_import_task(db, job=root_job))

    assert exc_info.value.code == "task_stale"
    assert embedding.status == "failed"
    assert embedding.attempt_count == 7
    assert db.flush_calls == 0


def test_catalog_uploader_and_uploaded_time_filters_are_cursor_bound():
    library_id = uuid.uuid4()
    uploader_id = uuid.uuid4()
    query = CatalogDocumentQuery(
        uploader_id=uploader_id,
        uploaded_from=date(2026, 8, 1),
        uploaded_to=date(2026, 8, 31),
        limit=20,
    )
    same_except_uploader = CatalogDocumentQuery(
        uploader_id=uuid.uuid4(),
        uploaded_from=date(2026, 8, 1),
        uploaded_to=date(2026, 8, 31),
        limit=20,
    )

    assert query.canonical_payload()["uploader_id"] == str(uploader_id)
    assert query.canonical_payload()["uploaded_from"] == "2026-08-01"
    assert catalog_filter_fingerprint(library_id, query) != catalog_filter_fingerprint(
        library_id,
        same_except_uploader,
    )


def test_catalog_uploader_email_is_masked_for_members_and_full_for_managers():
    uploader = SimpleNamespace(
        display_name="张三",
        username="zhangsan",
        email="zhangsan@example.com",
        deleted_at=None,
    )

    member = knowledge_catalog.catalog_uploader_read(uploader, reveal_email=False)
    manager = knowledge_catalog.catalog_uploader_read(uploader, reveal_email=True)

    assert member.email != uploader.email
    assert member.email.endswith("@example.com")
    assert uploader.email not in member.model_dump_json()
    assert manager.email == uploader.email


def test_document_delete_policy_allows_only_owner_or_existing_delete_permission(
    monkeypatch: pytest.MonkeyPatch,
):
    library = SimpleNamespace(slug="compliance")
    owner = SimpleNamespace(id=uuid.uuid4(), is_superuser=False)
    other_user = SimpleNamespace(id=uuid.uuid4(), is_superuser=False)
    owned = SimpleNamespace(created_by=owner.id)
    historical = SimpleNamespace(created_by=None)

    monkeypatch.setattr(documents, "has_permission", lambda *_args: False)
    assert documents.can_delete_document(owner, library, owned) is True
    assert documents.can_delete_document(other_user, library, owned) is False
    assert documents.can_delete_document(owner, library, historical) is False

    monkeypatch.setattr(documents, "has_permission", lambda *_args: True)
    assert documents.can_delete_document(other_user, library, owned) is True
