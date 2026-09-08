"""所有 ORM 模型集中 import，方便 Alembic autogenerate 发现。"""
from app.models.api_key import ApiKey
from app.models.audit import AuditLog
from app.models.attribute_definition import AttributeDefinition
from app.models.chat_history import ChatConversation, ChatMessage, ChatMessageSource
from app.models.canonical_entity import CanonicalEntity
from app.models.canonical_entity_evolution import (
    CanonicalEntityEvolutionCommand,
    CanonicalEntityEvolutionDecision,
    CanonicalEntityEvolutionSource,
    CanonicalEntityEvolutionSuccessor,
    CanonicalEntityProjectionAssignment,
)
from app.models.classification_taxonomy import (
    ClassificationLabel,
    ClassificationTaxonomy,
    LibraryClassificationLabel,
)
from app.models.classification_decision import (
    DocumentClassificationDecision,
    DocumentClassificationDecisionSet,
    DocumentClassificationProposal,
    DocumentClassificationRun,
)
from app.models.classification_job import DocumentClassificationJob
from app.models.classification_taxonomy_bootstrap import (
    TaxonomyBootstrapRun,
    TaxonomyBootstrapSource,
)
from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.cleanup_outbox import CleanupOutbox
from app.models.document_block import DocumentBlock
from app.models.document_file import DocumentFile
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.document_source import DocumentSource
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.embedding_job import EmbeddingJob
from app.models.entity import Entity
from app.models.entity_alias import EntityAlias
from app.models.entity_mention import EntityMention
from app.models.entity_resolution_decision import EntityResolutionDecision
from app.models.entity_type import EntityType
from app.models.evidence_unit import EvidenceUnit
from app.models.fact_foundation import (
    FactAssertion,
    FactResolutionDecision,
    LogicalFact,
    StablePredicateIdentity,
    StablePredicateMapping,
)
from app.models.external_graph_sync import (
    GraphExternalFactMapping,
    GraphExternalSyncConflict,
    GraphExternalSyncOperation,
    GraphSyncSourcePolicy,
)
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.folder import Folder
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.graph_governance_action import (
    GraphGovernanceAction,
    GraphGovernanceActionItem,
)
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.graph_candidate_evidence import (
    GraphEntityCandidateEvidence,
    GraphRelationCandidateEvidence,
)
from app.models.graph_candidates import GraphEntityCandidate, GraphRelationCandidate
from app.models.graph_occurrences import GraphEntityOccurrence, GraphRelationOccurrence
from app.models.graph_review import GraphEntityMergeCandidate, GraphExtractionConflict
from app.models.knowledge_artifact import KnowledgeArtifact
from app.models.knowledge_artifact_job import KnowledgeArtifactJob
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.library_faq import LibraryFAQQuestion
from app.models.migration_backfill_state import MigrationBackfillState
from app.models.ontology_version import OntologyVersion
from app.models.organization import Organization
from app.models.organization_capability_rollout import OrganizationCapabilityRollout
from app.models.organization_membership import OrganizationMembership
from app.models.public_api_operations import (
    PublicAPIAnswerLease,
    PublicAPIRateWindow,
    PublicAPIRequestRecord,
)
from app.models.rebuild_operation import RebuildOperation
from app.models.revision_retention import RevisionRetentionRecord
from app.models.revision_purge_operation import RevisionPurgeOperation
from app.models.relation_evidence import RelationEvidence
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.models.raw_claim_projection_binding import GraphRawClaimProjectionBinding
from app.models.claim_decision import GraphClaimDecision
from app.models.schema_lifecycle_action import SchemaLifecycleAction
from app.models.schema_discovery_run import SchemaDiscoveryRun
from app.models.stable_predicate_evolution import (
    StablePredicateEvolutionCommand,
    StablePredicateEvolutionDecision,
    StablePredicateEvolutionSource,
    StablePredicateEvolutionSuccessor,
    StablePredicateMappingEvolutionAssignment,
)
from app.models.service_heartbeat import ServiceHeartbeat
from app.models.sync_source import SyncSource
from app.models.user import User
from app.models.user_library_scope import UserLibraryScope, UserLibraryScopeItem

__all__ = [
    "ApiKey",
    "AuditLog",
    "AttributeDefinition",
    "ChatConversation",
    "ChatMessage",
    "ChatMessageSource",
    "CanonicalEntity",
    "CanonicalEntityEvolutionCommand",
    "CanonicalEntityEvolutionDecision",
    "CanonicalEntityEvolutionSource",
    "CanonicalEntityEvolutionSuccessor",
    "CanonicalEntityProjectionAssignment",
    "ClassificationLabel",
    "ClassificationTaxonomy",
    "DocumentClassificationDecision",
    "DocumentClassificationDecisionSet",
    "DocumentClassificationProposal",
    "DocumentClassificationRun",
    "DocumentClassificationJob",
    "TaxonomyBootstrapRun",
    "TaxonomyBootstrapSource",
    "Chunk",
    "ChunkBlock",
    "ChunkEvidence",
    "CleanupOutbox",
    "Document",
    "DocumentImportJob",
    "DocumentBlock",
    "DocumentFile",
    "DocumentRevision",
    "DocumentRevisionFile",
    "DocumentSource",
    "EmbeddingJob",
    "Entity",
    "EntityAlias",
    "EntityMention",
    "EntityResolutionDecision",
    "EntityType",
    "EvidenceUnit",
    "StablePredicateIdentity",
    "StablePredicateMapping",
    "LogicalFact",
    "FactAssertion",
    "FactResolutionDecision",
    "GraphExternalFactMapping",
    "GraphExternalSyncConflict",
    "GraphExternalSyncOperation",
    "GraphSyncSourcePolicy",
    "ExtractionContextSnapshot",
    "ExtractionRawOutputAttempt",
    "Folder",
    "GraphExtractionJob",
    "GraphExtractionUnit",
    "GraphGovernanceAction",
    "GraphGovernanceActionItem",
    "GraphPublication",
    "GraphPublicationItem",
    "GraphEntityCandidate",
    "GraphRelationCandidate",
    "GraphEntityOccurrence",
    "GraphRelationOccurrence",
    "GraphEntityCandidateEvidence",
    "GraphRelationCandidateEvidence",
    "GraphEntityMergeCandidate",
    "GraphExtractionConflict",
    "KnowledgeArtifact",
    "KnowledgeArtifactJob",
    "KnowledgeRelation",
    "Library",
    "LibraryClassificationLabel",
    "LibraryFAQQuestion",
    "MigrationBackfillState",
    "OntologyVersion",
    "SchemaDiscoveryRun",
    "StablePredicateEvolutionCommand",
    "StablePredicateEvolutionDecision",
    "StablePredicateEvolutionSource",
    "StablePredicateEvolutionSuccessor",
    "StablePredicateMappingEvolutionAssignment",
    "Organization",
    "OrganizationCapabilityRollout",
    "OrganizationMembership",
    "PublicAPIAnswerLease",
    "PublicAPIRateWindow",
    "PublicAPIRequestRecord",
    "RebuildOperation",
    "RevisionRetentionRecord",
    "RevisionPurgeOperation",
    "RelationEvidence",
    "RelationType",
    "RelationTypeConstraint",
    "GraphRawClaim",
    "GraphRawClaimOccurrence",
    "GraphRawClaimProjectionBinding",
    "GraphClaimDecision",
    "SchemaLifecycleAction",
    "ServiceHeartbeat",
    "SyncSource",
    "User",
    "UserLibraryScope",
    "UserLibraryScopeItem",
]
