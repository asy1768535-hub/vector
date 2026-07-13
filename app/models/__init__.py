"""所有 ORM 模型集中 import，方便 Alembic autogenerate 发现。"""
from app.models.api_key import ApiKey
from app.models.audit import AuditLog
from app.models.attribute_definition import AttributeDefinition
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
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.entity_mention import EntityMention
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.folder import Folder
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.library_faq import LibraryFAQQuestion
from app.models.migration_backfill_state import MigrationBackfillState
from app.models.ontology_version import OntologyVersion
from app.models.rebuild_operation import RebuildOperation
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.models.service_heartbeat import ServiceHeartbeat
from app.models.sync_source import SyncSource
from app.models.user import User

__all__ = [
    "ApiKey",
    "AuditLog",
    "AttributeDefinition",
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
    "Entity",
    "EntityAlias",
    "EntityMention",
    "EntityType",
    "EvidenceUnit",
    "ExtractionContextSnapshot",
    "ExtractionRawOutputAttempt",
    "Folder",
    "GraphExtractionJob",
    "GraphExtractionUnit",
    "KnowledgeRelation",
    "Library",
    "LibraryFAQQuestion",
    "MigrationBackfillState",
    "OntologyVersion",
    "RebuildOperation",
    "RelationEvidence",
    "RelationType",
    "RelationTypeConstraint",
    "ServiceHeartbeat",
    "SyncSource",
    "User",
]
