from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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

