from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from app.schemas.documents import DocumentRead


def test_document_read_maps_orm_doc_metadata_to_metadata():
    now = datetime.now(timezone.utc)
    document = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        external_id=None,
        title="测试文档",
        doc_metadata={"source": "docx"},
        content_hash="a" * 64,
        current_revision=1,
        status="ready",
        last_error=None,
        created_at=now,
        updated_at=now,
    )

    result = DocumentRead.model_validate(document)

    assert result.metadata == {"source": "docx"}


def test_document_source_model_is_exported():
    from app.models import DocumentSource

    assert DocumentSource.__tablename__ == "document_sources"
    assert DocumentSource.document_id.property.columns[0].primary_key
    assert DocumentSource.normalized_text.property.columns[0].nullable is False


def test_document_source_migration_file_exists():
    from pathlib import Path

    migration = Path("alembic/versions/0016_document_sources.py").read_text(encoding="utf-8")
    assert 'revision: str = "0016"' in migration
    assert 'down_revision: Union[str, None] = "0015"' in migration
    assert 'op.create_table("document_sources"' in migration
    assert 'ondelete="CASCADE"' in migration
