from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.schemas.evidence_locator import (
    EvidenceLocatorV1,
    SourceLocatorV1,
    legacy_evidence_fallback,
    normalize_evidence_locator_payload,
    sha256_text,
    validate_parent_relationship,
)


DOC_ID = UUID("00000000-0000-0000-0000-000000000001")
REV_ID = UUID("00000000-0000-0000-0000-000000000002")
FILE_ID = UUID("00000000-0000-0000-0000-000000000003")
RAW_HASH = "a" * 64
CONTENT_HASH = "b" * 64


def _locator(**source_overrides) -> EvidenceLocatorV1:
    text = "evidence text"
    source = {"kind": "text", "text": {"start": 0, "end": len(text)}}
    source_keys = {
        "kind",
        "file_name",
        "page",
        "text",
        "heading_path",
        "table",
        "sheet",
        "row",
        "column",
        "cell",
        "json_pointer",
        "bbox",
    }
    source.update({key: source_overrides.pop(key) for key in source_keys if key in source_overrides})
    values = {
        "document_id": DOC_ID,
        "document_revision_id": REV_ID,
        "revision_no": 1,
        "document_revision_file_id": FILE_ID,
        "raw_file_sha256": RAW_HASH,
        "normalized_content_hash": CONTENT_HASH,
        "unit_id": uuid4(),
        "unit_kind": "chunk",
        "ordinal": 1,
        "parser": {"name": "fixture", "version": "v1"},
        "source": source,
        "unit_text_sha256": sha256_text(text),
        "quote_sha256": sha256_text(text),
    }
    values.update(source_overrides)
    return EvidenceLocatorV1(**values)


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "text", "text": {"start": 0, "end": 4}},
        {"kind": "pdf", "page": {"start": 2, "end": 2}, "bbox": {"x_min": 1, "y_min": 2, "x_max": 5, "y_max": 8, "coordinate_system": "pixels"}},
        {"kind": "docx", "heading_path": ["Heading 1"], "table": {"index": 0}},
        {"kind": "xlsx", "sheet": {"name": "Sheet1"}, "row": {"start": 4, "end": 4}, "column": {"start": "a", "end": "c"}, "cell": {"start": "a4", "end": "c4"}},
        {"kind": "xls", "sheet": {"name": "Legacy"}, "row": {"start": 1, "end": 2}},
        {"kind": "csv", "row": {"start": 3, "end": 3}, "column": {"start": 1, "end": 2}, "cell": {"start": "A3", "end": "B3"}},
        {"kind": "json", "json_pointer": "/records/0/name"},
        {"kind": "image", "bbox": {"x_min": 0, "y_min": 0, "x_max": 10, "y_max": 20, "coordinate_system": "pixels"}},
    ],
)
def test_all_planned_source_locator_shapes_are_typed(source):
    locator = _locator(**source)

    assert locator.source.kind == source["kind"]
    assert locator.model_dump(mode="json")["locator_version"] == "v1"


def test_docx_row_column_cell_locators_require_a_table():
    with pytest.raises(ValidationError, match="DOCX table"):
        _locator(kind="docx", row={"start": 1, "end": 1})
    locator = _locator(
        kind="docx",
        table={"index": 0},
        row={"start": 1, "end": 1},
        column={"start": 1, "end": 2},
        cell={"start": "A1", "end": "B1"},
    )
    assert locator.source.table is not None


@pytest.mark.parametrize("kind", ["pdf", "text", "json", "image"])
def test_non_tabular_sources_reject_row_column_and_cell_locators(kind):
    with pytest.raises(ValidationError, match="tabular source"):
        _locator(kind=kind, row={"start": 1, "end": 1})


def test_normalization_is_json_safe_and_canonicalizes_coordinates():
    locator = _locator(
        kind="xlsx",
        sheet={"name": " Sheet1 "},
        column={"start": " a ", "end": "c"},
        cell={"start": "a4", "end": "c4"},
    )

    payload = normalize_evidence_locator_payload(locator)

    assert payload["document_id"] == str(DOC_ID)
    assert payload["source"]["sheet"]["name"] == "Sheet1"
    assert payload["source"]["column"] == {"start": "A", "end": "C"}
    assert "document_revision_file_id" in payload


def test_ocr_quality_requires_ocr_mode_and_stays_bounded():
    locator = _locator(
        kind="pdf",
        page={"start": 1, "end": 1},
        quality={
            "extraction_mode": "ocr",
            "ocr_engine": "rapidocr",
            "ocr_engine_version": "1.0",
            "ocr_confidence": 0.93,
        },
    )

    assert locator.quality.ocr_confidence == 0.93

    with pytest.raises(ValidationError, match="OCR confidence"):
        _locator(quality={"extraction_mode": "native", "ocr_confidence": 0.93})


def test_invalid_hash_ranges_json_pointer_and_bbox_are_rejected():
    with pytest.raises(ValidationError, match="SHA-256"):
        _locator(raw_file_sha256="not-a-hash")
    with pytest.raises(ValidationError, match="text span"):
        _locator(kind="text", text={"start": 5, "end": 2})
    with pytest.raises(ValidationError, match="JSON Pointer"):
        _locator(kind="json", json_pointer="records/0")
    with pytest.raises(ValidationError, match="bounding box"):
        _locator(kind="image", bbox={"x_min": 5, "y_min": 0, "x_max": 1, "y_max": 2, "coordinate_system": "pixels"})


def test_text_ranges_validate_every_item_for_containment_order_and_overlap():
    with pytest.raises(ValidationError, match="contained"):
        _locator(
            kind="text",
            text={
                "start": 0,
                "end": 10,
                "ranges": [{"start": 1, "end": 2}, {"start": 11, "end": 12}],
            },
        )
    with pytest.raises(ValidationError, match="ordered and non-overlapping"):
        _locator(
            kind="text",
            text={
                "start": 0,
                "end": 10,
                "ranges": [{"start": 1, "end": 5}, {"start": 4, "end": 7}],
            },
        )
    with pytest.raises(ValidationError, match="ordered and non-overlapping"):
        _locator(
            kind="text",
            text={
                "start": 0,
                "end": 10,
                "ranges": [{"start": 4, "end": 5}, {"start": 1, "end": 2}],
            },
        )


def test_excel_column_ranges_use_column_numbers_not_string_order():
    with pytest.raises(ValidationError, match="column end"):
        _locator(kind="xlsx", column={"start": "AA", "end": "Z"})
    locator = _locator(kind="xlsx", column={"start": "Z", "end": "AA"})
    assert locator.source.column is not None
    assert locator.source.column.start == "Z"
    assert locator.source.column.end == "AA"


def test_a1_cell_ranges_validate_column_and_row_direction():
    with pytest.raises(ValidationError, match="cell end"):
        _locator(kind="xlsx", cell={"start": "C4", "end": "A1"})
    with pytest.raises(ValidationError, match="cell end"):
        _locator(kind="xlsx", cell={"start": "A4", "end": "C1"})
    locator = _locator(kind="xlsx", cell={"start": "Z4", "end": "AA5"})
    assert locator.source.cell is not None
    assert locator.source.cell.start == "Z4"
    assert locator.source.cell.end == "AA5"


def test_verified_locator_requires_provenance_hashes_and_file_identity_pair():
    with pytest.raises(ValidationError, match="verified locator"):
        _locator(unit_text_sha256=None)
    with pytest.raises(ValidationError, match="provided together"):
        _locator(document_revision_file_id=None)


def test_parent_relationship_is_revision_scoped_and_ordered():
    parent = _locator(ordinal=1)
    child = _locator(ordinal=2, parent_unit_id=parent.unit_id)
    validate_parent_relationship(parent, child)

    with pytest.raises(ValueError, match="parent_unit_id"):
        validate_parent_relationship(parent, child.model_copy(update={"parent_unit_id": uuid4()}))
    with pytest.raises(ValueError, match="same document revision"):
        validate_parent_relationship(parent, child.model_copy(update={"document_revision_id": uuid4()}))
    with pytest.raises(ValueError, match="same revision number"):
        validate_parent_relationship(parent, child.model_copy(update={"revision_no": 2}))
    with pytest.raises(ValueError, match="precede"):
        validate_parent_relationship(parent, child.model_copy(update={"ordinal": 1}))


def test_legacy_fallback_preserves_known_scalars_without_fabricating_file_or_hashes():
    locator = legacy_evidence_fallback(
        {
            "document_id": DOC_ID,
            "document_revision_id": REV_ID,
            "revision_no": 2,
            "evidence_id": uuid4(),
            "document_block_id": uuid4(),
            "evidence_kind": "chunk",
            "source_start": 8,
            "source_end": 19,
            "page_start": 3,
            "page_end": 3,
            "title_path": ["Section 1"],
            "position": {"type": "page", "page": 3},
            "text_quote": "legacy quote",
            "text_quote_hash": sha256_text("legacy quote"),
            "evidence_metadata": {"location": {"type": "page", "page": 3}},
        }
    )

    assert locator.provenance_status == "legacy_unverified"
    assert locator.document_revision_file_id is None
    assert locator.raw_file_sha256 is None
    assert locator.source.page is not None
    assert locator.source.text is not None
    assert locator.parent_unit_id is not None
    assert locator.source.heading_path == ["Section 1"]
    assert locator.unit_text_sha256 is None
    assert locator.quote_sha256 == sha256_text("legacy quote")


def test_legacy_fallback_rejects_unknown_source_kind_instead_of_guessing():
    with pytest.raises(ValidationError, match="kind"):
        legacy_evidence_fallback(
            {
                "document_id": DOC_ID,
                "document_revision_id": REV_ID,
                "revision_no": 1,
                "evidence_id": uuid4(),
                "source_kind": "invented-domain-format",
            }
        )


def test_doc_source_supports_logical_table_and_image_coordinates():
    table = SourceLocatorV1.model_validate({
        "kind": "doc",
        "table": {"index": 0},
        "row": {"start": 1, "end": 1},
    })
    image = SourceLocatorV1.model_validate({
        "kind": "doc",
        "bbox": {
            "x_min": 0,
            "y_min": 0,
            "x_max": 10,
            "y_max": 10,
            "coordinate_system": "image_pixels",
        },
    })

    assert table.kind == "doc"
    assert image.kind == "doc"
