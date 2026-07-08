from __future__ import annotations

from pathlib import Path


def test_v02_m1_models_are_exported():
    from app.models import (
        ChunkBlock,
        ChunkEvidence,
        DocumentBlock,
        DocumentRevision,
        DocumentRevisionFile,
        EvidenceUnit,
        Folder,
        MigrationBackfillState,
        SyncSource,
    )

    assert Folder.__tablename__ == "folders"
    assert SyncSource.__tablename__ == "sync_sources"
    assert DocumentRevision.__tablename__ == "document_revisions"
    assert DocumentRevisionFile.__tablename__ == "document_revision_files"
    assert DocumentBlock.__tablename__ == "document_blocks"
    assert EvidenceUnit.__tablename__ == "evidence_units"
    assert ChunkBlock.__tablename__ == "chunk_blocks"
    assert ChunkEvidence.__tablename__ == "chunk_evidence"
    assert MigrationBackfillState.__tablename__ == "migration_backfill_state"


def test_documents_have_nullable_m1_columns_and_keep_legacy_revision():
    from app.models.document import Document

    cols = Document.__table__.c
    assert cols.external_id.type.length == 512
    for name in (
        "folder_id",
        "sync_source_id",
        "display_name",
        "current_revision_id",
        "latest_revision_id",
        "visibility_scope",
        "security_level",
    ):
        assert name in cols
        assert cols[name].nullable is True

    assert "current_revision" in cols
    assert cols.current_revision.nullable is False


def test_chunks_have_nullable_m1_provenance_columns():
    from app.models.chunk import Chunk

    cols = Chunk.__table__.c
    for name in (
        "document_revision_id",
        "block_id",
        "evidence_id",
        "chunk_kind",
        "page_start",
        "page_end",
        "title_path",
        "source_start",
        "source_end",
        "position",
        "created_at",
    ):
        assert name in cols
        assert cols[name].nullable is True

    assert "metadata" in cols
    assert "text" in cols


def test_embedding_jobs_have_nullable_revision_id_columns_and_keep_legacy_revision():
    from app.models.embedding_job import EmbeddingJob

    cols = EmbeddingJob.__table__.c
    assert cols.document_revision_id.nullable is True
    assert cols.document_revision_no.nullable is True
    assert "document_revision" in cols
    assert cols.document_revision.nullable is False


def test_v02_m1_migration_file_declares_expected_revision_and_core_operations():
    path = Path("alembic/versions/0018_v02_evidence_foundation_m1.py")
    text = path.read_text(encoding="utf-8")

    assert 'revision: str = "0018"' in text
    assert 'down_revision: Union[str, None] = "0017"' in text

    for table_name in (
        "folders",
        "sync_sources",
        "document_revisions",
        "document_revision_files",
        "document_blocks",
        "evidence_units",
        "chunk_blocks",
        "chunk_evidence",
        "migration_backfill_state",
    ):
        assert f'op.create_table("{table_name}"' in text

    for table_name in (
        "chunk_evidence",
        "chunk_blocks",
        "evidence_units",
        "document_blocks",
        "document_revision_files",
        "document_revisions",
        "sync_sources",
        "folders",
    ):
        assert f'op.drop_table("{table_name}")' in text

    assert 'op.add_column("documents", sa.Column("current_revision_id"' in text
    assert 'op.add_column("chunks", sa.Column("document_revision_id"' in text
    assert 'op.add_column("embedding_jobs", sa.Column("document_revision_id"' in text
    assert "uq_documents_library_sync_external_active" in text
    assert "uq_documents_library_external_no_source_active" in text
