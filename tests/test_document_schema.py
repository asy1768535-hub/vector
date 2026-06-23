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
