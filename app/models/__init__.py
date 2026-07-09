"""所有 ORM 模型集中 import，方便 Alembic autogenerate 发现。"""
from app.models.api_key import ApiKey
from app.models.audit import AuditLog
from app.models.chat_history import ChatConversation, ChatMessage, ChatMessageSource
from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.cleanup_outbox import CleanupOutbox
from app.models.document_block import DocumentBlock
from app.models.document_file import DocumentFile
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.document_source import DocumentSource
from app.models.document import Document
from app.models.embedding_job import EmbeddingJob
from app.models.evidence_unit import EvidenceUnit
from app.models.folder import Folder
from app.models.library import Library
from app.models.library_faq import LibraryFAQQuestion
from app.models.migration_backfill_state import MigrationBackfillState
from app.models.rebuild_operation import RebuildOperation
from app.models.service_heartbeat import ServiceHeartbeat
from app.models.sync_source import SyncSource
from app.models.user import User

__all__ = [
    "ApiKey",
    "AuditLog",
    "ChatConversation",
    "ChatMessage",
    "ChatMessageSource",
    "Chunk",
    "ChunkBlock",
    "ChunkEvidence",
    "CleanupOutbox",
    "Document",
    "DocumentBlock",
    "DocumentFile",
    "DocumentRevision",
    "DocumentRevisionFile",
    "DocumentSource",
    "EmbeddingJob",
    "EvidenceUnit",
    "Folder",
    "Library",
    "LibraryFAQQuestion",
    "MigrationBackfillState",
    "RebuildOperation",
    "ServiceHeartbeat",
    "SyncSource",
    "User",
]
