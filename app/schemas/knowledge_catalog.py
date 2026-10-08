from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal
from pydantic import (
    BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator,
    model_validator,
)


from app.schemas.knowledge_artifact import OutlineItemV1


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


CapabilityState = Literal[
    "disabled",
    "unavailable",
    "processing",
    "ready",
    "pending_review",
    "failed",
]


class CatalogCapabilitiesRead(StrictBaseModel):
    source: CapabilityState
    search: CapabilityState
    chat: CapabilityState
    summary: CapabilityState
    outline: CapabilityState
    classification: CapabilityState
    graph: CapabilityState


class CatalogClassificationLabelRead(StrictBaseModel):
    id: uuid.UUID
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=160)
    role: Literal["primary", "secondary"]
    ordinal: int = Field(ge=0, le=8)


class CatalogClassificationRead(StrictBaseModel):
    state: Literal["unclassified", "pending_review", "classified", "failed"]
    decision_set_id: uuid.UUID | None
    taxonomy_version_id: uuid.UUID | None
    source: Literal["model", "manual"] | None
    labels: list[CatalogClassificationLabelRead] = Field(default_factory=list, max_length=9)
    latest_run_id: uuid.UUID | None
    latest_run_status: str | None = Field(default=None, max_length=32)


class CatalogGraphCountsRead(StrictBaseModel):
    entities: int = Field(ge=0)
    relations: int = Field(ge=0)


class CatalogUploaderRead(StrictBaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    username: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    is_system: bool = False


class CatalogUploaderOptionRead(StrictBaseModel):
    value: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=255)


class CatalogUploaderOptionsRead(StrictBaseModel):
    items: list[CatalogUploaderOptionRead] = Field(max_length=101)


class CatalogDocumentListItemRead(StrictBaseModel):
    document_id: uuid.UUID
    library_id: uuid.UUID
    title: str = Field(min_length=1, max_length=512)
    document_status: str = Field(min_length=1, max_length=16)
    revision_id: uuid.UUID
    revision_no: int = Field(ge=1)
    revision_status: str = Field(min_length=1, max_length=32)
    revision_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime
    uploaded_at: datetime | None = None
    uploader: CatalogUploaderRead | None = None
    can_delete: bool = False
    overall_state: Literal["processing", "usable", "partial", "failed"]
    capabilities: CatalogCapabilitiesRead
    classification: CatalogClassificationRead
    summary_excerpt: str | None = Field(default=None, max_length=1000)
    graph_counts: CatalogGraphCountsRead


class CatalogDocumentPageRead(StrictBaseModel):
    contract_version: Literal["catalog-documents-v1"] = "catalog-documents-v1"
    items: list[CatalogDocumentListItemRead] = Field(max_length=100)
    total: int = Field(ge=0)
    next_cursor: str | None = Field(default=None, max_length=2048)


class CatalogRevisionFileRead(StrictBaseModel):
    id: uuid.UUID
    file_name: str = Field(min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CatalogSummaryRead(StrictBaseModel):
    artifact_id: uuid.UUID
    contract_version: Literal["summary-v1"]
    extractor_version: str = Field(min_length=1, max_length=64)
    generation_mode: Literal["deterministic", "model"]
    summary: str = Field(min_length=1, max_length=16000)
    source_character_count: int = Field(ge=0)
    truncated: bool


class CatalogOutlineRead(StrictBaseModel):
    artifact_id: uuid.UUID
    contract_version: Literal["outline-v1"]
    extractor_version: str = Field(min_length=1, max_length=64)
    generation_mode: Literal["deterministic", "model"]
    items: list[OutlineItemV1] = Field(min_length=1, max_length=256)


class CatalogEvidenceLocatorRead(StrictBaseModel):
    evidence_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)


class CatalogEntityKnowledgeUnitRead(StrictBaseModel):
    publication_id: uuid.UUID
    ontology_version_id: uuid.UUID
    item_id: uuid.UUID
    item_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entity_id: uuid.UUID
    entity_type_id: uuid.UUID
    entity_type_key: str = Field(min_length=1, max_length=128)
    entity_type_label: str = Field(min_length=1, max_length=255)
    canonical_name: str = Field(min_length=1, max_length=512)
    source_type: Literal["manual", "imported", "extracted"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[CatalogEvidenceLocatorRead] = Field(min_length=1, max_length=20)


class CatalogRelationKnowledgeUnitRead(StrictBaseModel):
    publication_id: uuid.UUID
    ontology_version_id: uuid.UUID
    item_id: uuid.UUID
    item_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation_id: uuid.UUID
    relation_type_id: uuid.UUID
    relation_type_key: str = Field(min_length=1, max_length=128)
    relation_type_label: str = Field(min_length=1, max_length=255)
    direction: Literal["directed", "undirected"]
    source_entity_id: uuid.UUID
    source_entity_name: str = Field(min_length=1, max_length=512)
    target_entity_id: uuid.UUID
    target_entity_name: str = Field(min_length=1, max_length=512)
    source_type: Literal["manual", "imported", "extracted"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    review_status: Literal["approved", "not_required"]
    evidence: list[CatalogEvidenceLocatorRead] = Field(min_length=1, max_length=20)


class CatalogGraphRead(StrictBaseModel):
    entities: list[CatalogEntityKnowledgeUnitRead] = Field(max_length=100)
    relations: list[CatalogRelationKnowledgeUnitRead] = Field(max_length=100)
    counts: CatalogGraphCountsRead
    entities_truncated: bool
    relations_truncated: bool

    @model_validator(mode="after")
    def validate_truncation(self):
        if len(self.entities) > self.counts.entities or len(self.relations) > self.counts.relations:
            raise ValueError("Catalog graph counts cannot be below returned rows")
        if self.entities_truncated != (len(self.entities) < self.counts.entities):
            raise ValueError("entity truncation must match count")
        if self.relations_truncated != (len(self.relations) < self.counts.relations):
            raise ValueError("relation truncation must match count")
        return self


class CatalogPdfPageAssessmentRead(StrictBaseModel):
    page: StrictInt = Field(ge=1, le=1_000_000)
    status: Literal["blank", "no_effective_content", "uncertain", "failed"]
    reason: Literal[
        "uniform_source_and_render", "visible_marks", "inspection_unavailable",
        "native_text_failed", "render_failed", "ocr_failed", "resource_limit",
        "human_review_no_effective_content",
    ]
    render_dpi: StrictInt = Field(ge=1, le=10_000)
    render_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    source_images_checked: StrictInt = Field(ge=0, le=32)
    checks: list[Literal[
        "native_text_empty", "ocr_empty", "full_page_uniform",
        "source_images_uniform", "paint_operations_safe",
    ]] = Field(max_length=5)
    classification_source: Literal["automatic", "human_review"] = "automatic"
    review_evidence_ref: str | None = Field(default=None, max_length=256)


class CatalogParsingCoverageRead(StrictBaseModel):
    contract_version: Literal["pdf-coverage-v1"] = "pdf-coverage-v1"
    status: Literal["complete", "partial", "unknown"] = "unknown"
    total_pages: StrictInt | None = Field(default=None, ge=1, le=1_000_000)
    processed_pages: list[StrictInt] = Field(default_factory=list, max_length=5000)
    unprocessed_visual_pages: list[StrictInt] = Field(default_factory=list, max_length=5000)
    skipped_visual_block_count: StrictInt = Field(default=0, ge=0, le=5000)
    reasons: list[Literal["visual_content_without_ocr", "visual_content_not_ingested"]] = Field(
        default_factory=list, max_length=8
    )
    confirmed_blank_pages: list[StrictInt] = Field(default_factory=list, max_length=5000)
    no_effective_content_pages: list[StrictInt] = Field(default_factory=list, max_length=5000)
    page_assessments: list[CatalogPdfPageAssessmentRead] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def validate_coverage(self):
        from app.services.pdf_coverage import validate_pdf_coverage_report

        validate_pdf_coverage_report(self.model_dump())
        return self


class CatalogPdfPageReviewInput(StrictBaseModel):
    page: StrictInt = Field(ge=1, le=1_000_000)
    render_dpi: StrictInt = Field(ge=1, le=10_000)
    render_sha256: StrictStr = Field(pattern=r"^[0-9a-fA-F]{64}$")
    review_evidence_ref: StrictStr = Field(min_length=1, max_length=256)

    @field_validator("review_evidence_ref")
    @classmethod
    def validate_evidence_reference(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("page review evidence reference is invalid")
        return value

    @model_validator(mode="after")
    def validate_page_binding(self):
        reference = self.review_evidence_ref
        parts = reference.split("/")
        if (
            not reference.startswith(("evidence:", "review:"))
            or "\\" in reference
            or ".." in parts
            or not reference.endswith(f"/page-{self.page}")
        ):
            raise ValueError("page review evidence reference must identify this page")
        return self


class CatalogPdfCoverageReviewApplyRequest(StrictBaseModel):
    revision_id: uuid.UUID
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-fA-F]{64}$")
    idempotency_key: uuid.UUID
    reviews: list[CatalogPdfPageReviewInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_pages(self):
        pages = [item.page for item in self.reviews]
        if len(pages) != len(set(pages)):
            raise ValueError("PDF page reviews must not repeat a page")
        return self


class CatalogPdfCoverageReviewRollbackRequest(StrictBaseModel):
    revision_id: uuid.UUID
    source_sha256: StrictStr = Field(pattern=r"^[0-9a-fA-F]{64}$")
    idempotency_key: uuid.UUID
    apply_idempotency_key: uuid.UUID


class CatalogPdfCoverageReviewMutationRead(StrictBaseModel):
    operation_key: uuid.UUID
    changed: bool
    parsing_coverage: CatalogParsingCoverageRead


class CatalogDocumentDetailRead(CatalogDocumentListItemRead):
    contract_version: Literal["catalog-document-detail-v1"] = "catalog-document-detail-v1"
    file: CatalogRevisionFileRead | None
    summary: CatalogSummaryRead | None
    outline: CatalogOutlineRead | None
    graph: CatalogGraphRead
    parsing_coverage: CatalogParsingCoverageRead = Field(default_factory=CatalogParsingCoverageRead)


class CatalogEvidenceFactRefRead(StrictBaseModel):
    publication_id: uuid.UUID
    ontology_version_id: uuid.UUID
    item_kind: Literal["entity", "relation"]
    item_id: uuid.UUID
    fact_id: uuid.UUID
    chunk_id: uuid.UUID | None
    item_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CatalogEvidenceDetailRead(StrictBaseModel):
    contract_version: Literal["catalog-evidence-v1"] = "catalog-evidence-v1"
    evidence_id: uuid.UUID
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    revision_file_id: uuid.UUID | None
    evidence_kind: str = Field(min_length=1, max_length=32)
    text_quote: str | None = Field(default=None, max_length=8000)
    text_window: str | None = Field(default=None, max_length=16000)
    window_start: int | None = Field(default=None, ge=0)
    window_end: int | None = Field(default=None, ge=0)
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    title_path: list[str] | None = Field(default=None, max_length=16)
    fact_refs: list[CatalogEvidenceFactRefRead] = Field(min_length=1, max_length=100)
    fact_refs_truncated: bool


class CatalogFileAccessRead(StrictBaseModel):
    contract_version: Literal["catalog-file-access-v1"] = "catalog-file-access-v1"
    revision_file_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    file_name: str = Field(min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    access_mode: Literal["signed_url", "proxy"]
    url: str = Field(min_length=1, max_length=4096)
    expires_at: datetime | None
