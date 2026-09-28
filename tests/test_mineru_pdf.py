from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.api import documents
from app.config import settings
from app.services import import_parsing, mineru_pdf
from app.services.evidence_write_path import validate_parser_segments


def _item(kind, text=None, *, page_idx=0, bbox=None, **other):
    return {
        "type": kind,
        "page_idx": page_idx,
        "bbox": bbox or [1, 2, 30, 40],
        **({"text": text} if text is not None else {}),
        **other,
    }


def _map(items, size=200):
    return mineru_pdf.map_mineru_content_list(items, chunk_size=size, chunk_overlap=0)


def _payload(items):
    return {"version": "3.4.4", "results": {"document.pdf": {
        "content_list": json.dumps(items), "middle_json": json.dumps({"pdf_info": [{}]}),
    }}}


def _remote(data=b"%PDF", **overrides):
    options = {
        "base_url": "https://mineru.internal",
        "expected_version": "3.4.4",
        "timeout_seconds": 4,
        "max_response_bytes": 4096,
        "chunk_size": 200,
        "chunk_overlap": 0,
    }
    options.update(overrides)
    return mineru_pdf.parse_pdf_remote(data, **options)


def test_ignores_all_image_descriptions_page_numbers_and_unknown_types():
    source = _map([
        _item("header", "Verified title", page_idx=0),
        *(
            _item(kind, "wrong content", content="wrong content", description="wrong description")
            for kind in ("image", "natural_image", "flowchart", "text_image", "diagram")
        ),
        _item("page_number", "416", page_idx=415),
        _item("footer", "private footer", page_idx=415),
        _item("text", "Visible text", page_idx=415),
    ])
    assert source["normalized_text"] == "Verified title\n\nVisible text"
    assert "wrong" not in str(source)
    assert "416" not in source["normalized_text"]
    assert source["segments"][-1]["parser_unit"]["source"]["page"] == {"start": 416, "end": 416}
    assert source["segments"][-1]["parser_unit"]["source"]["bbox"]["coordinate_system"] == (
        "mineru_output_coordinates"
    )
    validate_parser_segments(source["segments"])
    assert source["coverage"] == {
        "contract_version": "pdf-coverage-v1",
        "status": "partial",
        "total_pages": None,
        "processed_pages": [1, 416],
        "unprocessed_visual_pages": [1],
        "skipped_visual_block_count": 5,
        "reasons": ["visual_content_not_ingested"],
    }
    coverage_unit = source["segments"][0]["structured_units"][-1]
    assert coverage_unit["unit_key"] == "pdf:coverage:v1"
    assert coverage_unit["text"] == ""
    assert coverage_unit["value"] == source["coverage"]


def test_small_table_stays_together_with_numbered_question_and_valid_locator_tree():
    source = _map([
        _item("text", "1. Find the ratio"),
        _item("table", table_body="<table><tr><th>Rule</th><th>Result</th></tr>"
              "<tr><td>$50=1</td><td>yes</td></tr></table>"),
    ])
    assert len(source["chunks"]) == 1
    assert "1. Find the ratio" in source["chunks"][0]["text"]
    assert "$50=1" in source["chunks"][0]["text"]
    question = source["segments"][0]
    assert question["kind"] == "question"
    content_units = [
        unit for unit in question["structured_units"]
        if unit["unit_key"] != "pdf:coverage:v1"
    ]
    assert [u["unit_kind"] for u in content_units] == [
        "structured_unit", "table", "row", "cell", "cell", "row", "cell", "cell",
    ]
    table = next(unit for unit in question["structured_units"] if unit["unit_kind"] == "table")
    assert table["source"]["table"] == {"index": 0}
    for unit in content_units[1:]:
        assert set(unit["source"]) == {"page", "table", "bbox"}
        assert unit["source"]["page"] == {"start": 1, "end": 1}
    validate_parser_segments(source["segments"])


def test_known_page_with_unmappable_structure_is_not_marked_complete():
    source = mineru_pdf.map_mineru_response(
        _payload([
            _item("text", "readable body"),
            {"type": "table", "page_idx": 0, "bbox": None, "table_body": "not a table"},
        ]),
        expected_version="3.4.4",
        chunk_size=200,
        chunk_overlap=0,
    )
    assert source["normalized_text"] == "readable body"
    assert source["coverage"]["status"] == "unknown"
    assert source["coverage"]["processed_pages"] == [1]


def test_standalone_long_table_keeps_table_aware_slicing():
    rows = "".join(f"<tr><td>R{i}</td><td>{i}</td></tr>" for i in range(8))
    source = _map([
        _item("table", table_body=f"<table><tr><th>Name</th><th>Value</th></tr>{rows}</table>"),
    ], size=55)
    assert source["segments"][0]["kind"] == "table"
    assert len(source["chunks"]) > 1
    assert all("Name | Value" in chunk["text"] for chunk in source["chunks"])
    assert "R7 | 7" in source["normalized_text"]


def test_question_options_and_raw_formula_stay_in_one_segment_and_chunk():
    source = _map([
        _item("text", "1. Compute the value"),
        _item("formula", r"C/c=5/17"),
        _item("list", list_items=["A. 1", "C. 2"]),
    ])
    assert len(source["segments"]) == len(source["chunks"]) == 1
    question = source["segments"][0]
    assert question["kind"] == "question"
    assert question["structure"]["formulae"] == ["C/c=5/17"]
    assert "B." not in source["normalized_text"]
    assert "C/c=5/17" in source["chunks"][0]["text"]
    assert question["structured_units"][1]["latex"] == "C/c=5/17"
    validate_parser_segments(source["segments"])


def test_question_keeps_image_table_formula_and_options_in_one_segment():
    source = _map([
        _item("text", "1. Complete the result"),
        _item("image", content="ignored image text", description="ignored description"),
        _item("table", table_body=(
            "<table><tr><th>Item</th><th>Value</th></tr>"
            "<tr><td>H</td><td>5/11</td></tr></table>"
        )),
        _item("formula", r"H=5/11"),
        _item("list", list_items=["A. 5/11", "B. 11/5"]),
    ], size=500)

    assert len(source["segments"]) == len(source["chunks"]) == 1
    question = source["segments"][0]
    assert question["kind"] == "question"
    assert question["structure"]["item_types"] == ["text", "table", "formula", "list"]
    for expected in ("1. Complete the result", "Item | Value", "H | 5/11", "H=5/11", "A. 5/11"):
        assert expected in question["text"]
        assert expected in source["chunks"][0]["text"]
    assert "ignored" not in source["normalized_text"]
    table = next(unit for unit in question["structured_units"] if unit["unit_kind"] == "table")
    assert table["parent_key"] == question["unit_key"]
    formula = next(unit for unit in question["structured_units"] if unit.get("latex") == "H=5/11")
    assert formula["parent_key"] == question["unit_key"]
    validate_parser_segments(source["segments"])
    assert source["coverage"]["status"] == "partial"
    assert source["coverage"]["unprocessed_visual_pages"] == [1]
    assert source["coverage"]["skipped_visual_block_count"] == 1


def test_ignored_image_respects_page_and_next_question_boundaries():
    source = _map([
        _item("text", "1. First", page_idx=0),
        _item("flowchart", content="ignored", page_idx=0),
        _item("list", list_items=["A. one"], page_idx=0),
        _item("natural_image", content="ignored", page_idx=1),
        _item("text", "2. Second", page_idx=1),
        _item("text", "answer", page_idx=1),
        _item("text", "3. Third", page_idx=1),
    ])
    assert [segment["text"] for segment in source["segments"]] == [
        "1. First\nA. one",
        "2. Second\nanswer",
        "3. Third",
    ]


def test_rowspan_expands_semantics_and_preserves_cell_span_metadata():
    source = _map([_item("table", table_body=(
        "<table><tr><th>Region</th><th>Item</th><th>Value</th></tr>"
        "<tr><td rowspan='2'>B</td><td>X</td><td>1</td></tr>"
        "<tr><td>Y</td><td>2</td></tr></table>"
    ))])
    table = source["segments"][0]
    assert table["rows"] == ["Region | Item | Value", "B | X | 1", "B | Y | 2"]
    assert all(len(row.split(" | ")) == 3 for row in table["rows"])
    copied = next(
        unit for unit in table["structured_units"]
        if unit["unit_kind"] == "cell" and unit["row_ordinal"] == 2 and unit["cell_ordinal"] == 0
    )
    assert copied["text"] == "B"
    assert copied["rowspan"] == 2 and copied["colspan"] == 1
    assert copied["span_origin_row"] == 1 and copied["span_origin_column"] == 0
    assert copied["is_span_copy"] is True
    validate_parser_segments(source["segments"])


def test_colspan_expands_semantics_and_invalid_span_defaults_to_one():
    source = _map([_item("table", table_body=(
        "<table><tr><th colspan='2'>Metric</th><th>Value</th></tr>"
        "<tr><td colspan='invalid'>A</td><td>B</td><td>1</td></tr></table>"
    ))])
    table = source["segments"][0]
    assert table["rows"] == ["Metric | Metric | Value", "A | B | 1"]
    cells = [unit for unit in table["structured_units"] if unit["unit_kind"] == "cell"]
    origin = next(unit for unit in cells if unit["row_ordinal"] == 0 and unit["cell_ordinal"] == 0)
    copied = next(unit for unit in cells if unit["row_ordinal"] == 0 and unit["cell_ordinal"] == 1)
    invalid = next(unit for unit in cells if unit["row_ordinal"] == 1 and unit["cell_ordinal"] == 0)
    assert origin["colspan"] == copied["colspan"] == 2
    assert origin["span_origin_column"] == copied["span_origin_column"] == 0
    assert origin["is_span_copy"] is False and copied["is_span_copy"] is True
    assert invalid["colspan"] == 1 and invalid["is_span_copy"] is False
    validate_parser_segments(source["segments"])
def test_mineru_content_limits_fail_closed(monkeypatch):
    monkeypatch.setattr(mineru_pdf, "_MAX_CONTENT_ITEMS", 1)
    with pytest.raises(mineru_pdf.MineruPdfError, match="content limits"):
        _map([_item("text", "one"), _item("text", "two")])

    monkeypatch.setattr(mineru_pdf, "_MAX_CONTENT_ITEMS", 10)
    monkeypatch.setattr(mineru_pdf, "_MAX_CONTENT_STRING_CHARS", 4)
    with pytest.raises(mineru_pdf.MineruPdfError, match="content limits"):
        _map([_item("text", "12345")])

    monkeypatch.setattr(mineru_pdf, "_MAX_CONTENT_STRING_CHARS", 10_000)
    monkeypatch.setattr(mineru_pdf, "_MAX_STRUCTURED_UNITS", 2)
    with pytest.raises(mineru_pdf.MineruPdfError, match="content limits"):
        _map([_item("table", table_body=(
            "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
        ))])


def test_remote_response_hard_cap_cannot_be_relaxed(monkeypatch):
    original_client = httpx.Client
    monkeypatch.setattr(mineru_pdf, "_MAX_RESPONSE_BYTES", 4)
    monkeypatch.setattr(
        mineru_pdf.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=b"12345")
            ),
            **kwargs,
        ),
    )
    with pytest.raises(mineru_pdf.MineruPdfError, match="limit"):
        _remote(max_response_bytes=4096)




def test_newspaper_preserves_three_column_input_order_and_existing_budget():
    source = _map([
        _item("text", "left column"),
        _item("text", "middle column"),
        _item("text", "right column"),
    ], size=10)
    assert [s["text"] for s in source["segments"]] == [
        "left column", "middle column", "right column",
    ]
    assert source["normalized_text"].index("left") < source["normalized_text"].index("middle")
    assert source["normalized_text"].index("middle") < source["normalized_text"].index("right")
    assert len(source["chunks"]) >= 3


def test_image_only_result_fails_explicitly():
    with pytest.raises(mineru_pdf.MineruPdfError, match="no importable structure"):
        _map([_item("image", content="bad", description="bad")])


@pytest.mark.parametrize("payload", [
    {"version": "3.4.3", "content_list": [], "middle_json": {"pdf_info": []}},
    {"version": "3.4.4", "results": {"x": {"content_list": []}}},
    {"version": "3.4.4", "content_list": "invalid", "middle_json": {"pdf_info": []}},
])
def test_wrong_version_or_bad_shape_fails_without_echoing_content(payload):
    payload["secret"] = "private body"
    with pytest.raises(mineru_pdf.MineruPdfError) as exc:
        mineru_pdf.map_mineru_response(
            payload, expected_version="3.4.4", chunk_size=200, chunk_overlap=0,
        )
    assert "private body" not in str(exc.value)


def test_explicit_bounded_page_count_can_mark_complete():
    source = mineru_pdf.map_mineru_content_list(
        [_item("text", "body")],
        chunk_size=200,
        chunk_overlap=0,
        total_pages=1,
    )
    assert source["coverage"]["status"] == "complete"
    assert source["coverage"]["total_pages"] == 1


def test_embedded_content_list_and_page_limits_fail_closed(monkeypatch):
    monkeypatch.setattr(mineru_pdf, "_MAX_CONTENT_ITEMS", 1)
    with pytest.raises(mineru_pdf.MineruPdfError, match="content limits"):
        mineru_pdf.map_mineru_response(
            _payload([_item("text", "one"), _item("text", "two")]),
            expected_version="3.4.4",
            chunk_size=200,
            chunk_overlap=0,
        )
    monkeypatch.setattr(mineru_pdf, "_MAX_CONTENT_ITEMS", 10)
    with pytest.raises(mineru_pdf.MineruPdfError, match="page limits"):
        mineru_pdf.map_mineru_content_list(
            [_item("text", "body", page_idx=500)],
            chunk_size=200,
            chunk_overlap=0,
        )


def test_embedded_content_list_depth_limit_is_checked_before_decode(monkeypatch):
    monkeypatch.setattr(mineru_pdf, "_MAX_EMBEDDED_JSON_DEPTH", 4)
    nested_item = "[" * 4 + "]" * 4
    payload = {
        "version": "3.4.4",
        "content_list": f"[{nested_item}]",
        "middle_json": "{}",
    }

    with pytest.raises(mineru_pdf.MineruPdfError, match="content limits"):
        mineru_pdf.map_mineru_response(
            payload, expected_version="3.4.4", chunk_size=200, chunk_overlap=0,
        )


def test_embedded_content_list_node_limit_rejects_many_empty_containers(monkeypatch):
    monkeypatch.setattr(mineru_pdf, "_MAX_EMBEDDED_JSON_NODES", 5)
    payload = {
        "version": "3.4.4",
        "content_list": "[[{}, {}, {}]]",
        "middle_json": "{}",
    }

    with pytest.raises(mineru_pdf.MineruPdfError, match="content limits"):
        mineru_pdf.map_mineru_response(
            payload, expected_version="3.4.4", chunk_size=200, chunk_overlap=0,
        )



def test_embedded_content_list_string_budget_counts_keys_and_values(monkeypatch):
    monkeypatch.setattr(mineru_pdf, "_MAX_CONTENT_STRING_CHARS", 10)
    for content_list in ('[{"12345678901":"x"}]', '["12345678901"]'):
        payload = {
            "version": "3.4.4",
            "content_list": content_list,
            "middle_json": "{}",
        }
        with pytest.raises(mineru_pdf.MineruPdfError, match="content limits"):
            mineru_pdf.map_mineru_response(
                payload, expected_version="3.4.4", chunk_size=200, chunk_overlap=0,
            )


def test_embedded_content_list_huge_integer_fails_with_safe_error():
    payload = {
        "version": "3.4.4",
        "content_list": "[" + "9" * 4301 + "]",
        "middle_json": "{}",
    }

    with pytest.raises(mineru_pdf.MineruPdfError, match="response is invalid"):
        mineru_pdf.map_mineru_response(
            payload, expected_version="3.4.4", chunk_size=200, chunk_overlap=0,
        )

def test_table_span_expansion_checks_text_budget_before_row_render(monkeypatch):
    monkeypatch.setattr(mineru_pdf, "_MAX_EXPANDED_TABLE_TEXT_CHARS", 10)
    with pytest.raises(mineru_pdf.MineruPdfError, match="table exceeds content limits"):
        _map([_item(
            "table",
            table_body="<table><tr><td colspan='2'>123456</td></tr></table>",
        )])


def test_remote_multipart_contract_and_no_real_network(monkeypatch):
    captured = {}
    original_client = httpx.Client

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, json=_payload([_item("text", "body")]))

    def mock_client(**kwargs):
        captured["timeout"] = kwargs["timeout"]
        captured["follow_redirects"] = kwargs["follow_redirects"]
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(mineru_pdf.httpx, "Client", mock_client)
    source = _remote()
    assert source["normalized_text"] == "body"
    assert source["coverage"]["status"] == "unknown"
    assert source["coverage"]["total_pages"] is None
    assert source["coverage"]["processed_pages"] == [1]
    request = captured["request"]
    assert request.url.path == "/file_parse"
    assert request.method == "POST"
    body = request.content
    assert b'name="files"' in body
    assert b'name="backend"' in body and b"hybrid-engine" in body
    assert b'name="effort"' in body and b"high" in body
    assert b'name="parse_method"' in body and b"ocr" in body
    for name in ("formula_enable", "table_enable", "image_analysis", "return_content_list", "return_middle_json"):
        assert f'name="{name}"'.encode() in body
    for name in ("return_md", "return_model_output", "return_images"):
        assert f'name="{name}"'.encode() in body
    assert captured["follow_redirects"] is False
    assert captured["timeout"].read == 4


@pytest.mark.parametrize("mode", ["body", "header", "timeout", "bad_json", "status"])
def test_remote_limits_timeout_and_bad_responses_do_not_leak(monkeypatch, mode):
    original_client = httpx.Client

    def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("secret /private/path", request=request)
        if mode == "status":
            return httpx.Response(500, text="secret /private/path")
        if mode == "header":
            return httpx.Response(200, content=b"{}", headers={"content-length": "9000"})
        if mode == "body":
            return httpx.Response(200, content=b"x" * 4100)
        return httpx.Response(200, content=b"secret /private/path")

    monkeypatch.setattr(
        mineru_pdf.httpx, "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(mineru_pdf.MineruPdfError) as exc:
        _remote()
    assert "secret" not in str(exc.value) and "/private/path" not in str(exc.value)
    if mode in {"body", "header"}:
        assert "limit" in str(exc.value)
    if mode == "timeout":
        assert "timed out" in str(exc.value)


def test_library_allowlist_selects_identical_parser_for_sync_bytes_and_worker_path(monkeypatch, tmp_path):
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=False)
    path = tmp_path / "pdf.upload"
    path.write_bytes(b"%PDF")
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "other, target")
    monkeypatch.setattr(
        import_parsing.pdf_preflight,
        "preflight_pdf",
        lambda *_args, **_kwargs: {
            "contract_version": "pdf-preflight-v1",
            "status": "complete",
            "total_pages": 1,
            "page_count_known": True,
            "page_limit_exceeded": False,
            "image_limit_exceeded": False,
            "has_mixed_content": False,
            "pages": [{
                "page": 1,
                "native_text_chars": 0,
                "embedded_image_count": 1,
                "has_visual_content": True,
                "low_text": True,
            }],
            "unknown_reason": None,
        },
    )
    calls = []

    def parse(data, **kwargs):
        calls.append((data, kwargs))
        return _map([_item("text", "selected")])

    monkeypatch.setattr(mineru_pdf, "parse_pdf_remote", parse)
    monkeypatch.setattr(import_parsing.pdf_extract, "build_pdf_source", lambda *_args, **_kwargs: pytest.fail("unexpected fallback"))
    sync_source = import_parsing.build_pdf_import_source(b"%PDF", library)
    parsed = import_parsing.parse_import_file(path, library, file_name="sample.pdf")
    assert parsed.normalized_text == sync_source["normalized_text"] == "selected"
    assert parsed.chunks == sync_source["chunks"]
    assert parsed.segments == sync_source["segments"]
    assert [call[0] for call in calls] == [b"%PDF", path]
    assert calls[0][1] == calls[1][1]
    assert calls[0][1]["total_pages"] == 1

    def fail(*_args, **_kwargs):
        raise mineru_pdf.MineruPdfError("MinerU request failed")

    monkeypatch.setattr(mineru_pdf, "parse_pdf_remote", fail)
    with pytest.raises(mineru_pdf.MineruPdfError):
        import_parsing.build_pdf_import_source(b"%PDF", library)

def test_default_disabled_and_slug_match_exactly(monkeypatch):
    library = SimpleNamespace(slug="target")
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    assert not import_parsing.mineru_pdf_enabled(library)
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    assert import_parsing.mineru_pdf_enabled(library)
    library.slug = "target-extra"
    assert not import_parsing.mineru_pdf_enabled(library)


def test_prose_and_list_retain_shared_context_and_individual_evidence():
    source = _map([
        _item("header", "Team requirements", bbox=[0, 0, 50, 10]),
        _item("text", "Key traits required for an agile team:", bbox=[0, 12, 50, 20]),
        _item("list", list_items=["Competence", "Common focus", "Collaboration"], bbox=[0, 22, 50, 50]),
    ], size=500)
    assert len(source["chunks"]) == 1
    assert "agile team" in source["chunks"][0]["text"]
    assert "Collaboration" in source["chunks"][0]["text"]
    segment = source["segments"][0]
    assert len([
        unit for unit in segment["structured_units"]
        if unit["unit_key"] != "pdf:coverage:v1"
    ]) == 3
    assert segment["structured_units"][-1]["value"] == source["coverage"]
    assert segment["parser_unit"]["source"]["bbox"]["y_max"] == 50
    assert segment["structured_units"][1]["source"]["bbox"]["y_max"] == 20
    validate_parser_segments(source["segments"])


def test_prose_packing_respects_pages_headings_images_and_budget():
    source = _map([
        _item("text", "a" * 30),
        _item("text", "b" * 30),
        _item("text", "next page", page_idx=1),
        _item("header", "New section", page_idx=1),
        _item("image", "untrusted", page_idx=1),
        _item("list", list_items=["Independent list"], page_idx=1),
    ], size=50)
    assert len(source["segments"]) == 5
    assert "untrusted" not in source["normalized_text"]
    assert all(len(chunk["text"]) <= 50 for chunk in source["chunks"])
    validate_parser_segments(source["segments"])


def test_document_ingest_and_replace_forward_segments(monkeypatch):
    segments = [{"kind": "question"}]
    library = SimpleNamespace(id=uuid.uuid4())
    document = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library.id,
        deleted_at=None,
        doc_metadata=None,
        external_id=None,
        status="pending",
        title="doc.pdf",
        current_revision=2,
        latest_revision_id=None,
        current_revision_id=None,
    )
    job = SimpleNamespace(id=uuid.uuid4(), document_revision_id=None)
    doc_data = {
        "text": "body",
        "title": "doc.pdf",
        "external_id": None,
        "metadata": None,
        "splitter": "text",
        "chunks": [{"text": "body"}],
        "segments": segments,
    }

    ingest = AsyncMock(return_value=(document, job, 1, True))
    monkeypatch.setattr(documents.ingest_service, "ingest_text", ingest)
    asyncio.run(documents._ingest_or_upsert(AsyncMock(), library, uuid.uuid4(), doc_data))
    assert ingest.await_args.kwargs["segments"] is segments

    result = MagicMock()
    result.scalars.return_value.first.return_value = document
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=None)
    db.add = MagicMock()
    reingest = AsyncMock(return_value=(job, 1, True))
    monkeypatch.setattr(documents.ingest_service, "reingest_document", reingest)
    asyncio.run(documents._replace_document(db, library, document.id, doc_data))
    assert reingest.await_args.kwargs["segments"] is segments

def test_map_mineru_response_with_total_pages_reports_complete_coverage():
    source = mineru_pdf.map_mineru_response(
        _payload([
            _item("text", "page one text", page_idx=0),
            _item("text", "page two text", page_idx=1),
        ]),
        expected_version="3.4.4",
        chunk_size=200,
        chunk_overlap=0,
        total_pages=2,
    )
    assert source["coverage"]["status"] == "complete"
    assert source["coverage"]["total_pages"] == 2
    assert source["coverage"]["processed_pages"] == [1, 2]
    assert source["coverage"]["reasons"] == []


def test_remote_passes_total_pages_through(monkeypatch):
    original_client = httpx.Client
    monkeypatch.setattr(
        mineru_pdf.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    content=json.dumps(_payload([_item("text", "single page", page_idx=0)])).encode(),
                )
            ),
            **kwargs,
        ),
    )
    source = _remote(total_pages=1)
    assert source["coverage"]["status"] == "complete"
    assert source["coverage"]["total_pages"] == 1


def _mock_single_page_scan_preflight(monkeypatch, *, total_pages: int = 1):
    monkeypatch.setattr(
        import_parsing.pdf_preflight,
        "preflight_pdf",
        lambda _data, **_kwargs: {
            "contract_version": "pdf-preflight-v1",
            "status": "complete",
            "total_pages": total_pages,
            "page_count_known": True,
            "has_mixed_content": False,
            "page_limit_exceeded": False,
            "image_limit_exceeded": False,
            "pages": [
                {
                    "page": idx + 1,
                    "native_text_chars": 0,
                    "embedded_image_count": 1,
                    "has_visual_content": True,
                    "low_text": True,
                }
                for idx in range(total_pages)
            ],
            "unknown_reason": None,
        },
    )


def test_pdf_quality_cascade_disabled_keeps_v1_mineru_selection(monkeypatch):
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", False)
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=True)

    _mock_single_page_scan_preflight(monkeypatch)

    mineru_calls = []
    def parse_remote(data, **kwargs):
        mineru_calls.append((data, kwargs))
        return _map([_item("text", "from mineru")])

    monkeypatch.setattr(mineru_pdf, "parse_pdf_remote", parse_remote)
    monkeypatch.setattr(
        import_parsing.pdf_extract,
        "build_pdf_source",
        lambda *_args, **_kwargs: pytest.fail("local build_pdf_source should not be called when gate disabled"),
    )

    source = import_parsing.build_pdf_import_source(b"%PDF-test", library)
    assert len(mineru_calls) == 1
    assert source["normalized_text"] == "from mineru"
    assert source["routing"]["selection"] == "mineru"


def test_pdf_quality_cascade_enabled_accepts_good_local_candidate(monkeypatch):
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=True)

    _mock_single_page_scan_preflight(monkeypatch)

    local_source = _map([_item("text", "本地高质量解析正文内容")])
    from tests.test_pdf_quality_inspector import _build_valid_ocr_blocks
    valid_blocks = _build_valid_ocr_blocks(10)
    page_text = "\n".join(b["text"] for b in valid_blocks)
    local_source["normalized_text"] = page_text
    local_source["chunks"] = [{"text": page_text, "source_start": 0, "source_end": len(page_text)}]
    local_source["coverage"] = {"status": "complete", "total_pages": 1, "processed_pages": [1]}
    local_source["segments"][0]["text"] = page_text
    local_source["segments"][0]["quality"] = {
        "extraction_mode": "ocr",
        "visual_content_unparsed": False,
        "ocr_blocks": valid_blocks,
    }

    monkeypatch.setattr(
        import_parsing.pdf_extract,
        "build_pdf_source",
        lambda *_args, **_kwargs: local_source,
    )
    monkeypatch.setattr(
        mineru_pdf,
        "parse_pdf_remote",
        lambda *_args, **_kwargs: pytest.fail("remote MinerU should not be called when candidate accepted"),
    )

    source = import_parsing.build_pdf_import_source(b"%PDF-scan", library)
    assert source["routing"]["selection"] == "native_or_rapidocr"
    assert source["routing"]["needs_review"] is False
    assert "scan_page_detected" in source["routing"]["reasons"]
    assert source["normalized_text"] == page_text


def test_pdf_quality_cascade_enabled_rejects_and_escalates_to_mineru(monkeypatch):
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=True)

    _mock_single_page_scan_preflight(monkeypatch)

    bad_local = _map([_item("text", "短")])
    bad_local["segments"][0]["quality"] = {
        "extraction_mode": "ocr",
        "visual_content_unparsed": False,
        "ocr_blocks": [],
    }

    monkeypatch.setattr(
        import_parsing.pdf_extract,
        "build_pdf_source",
        lambda *_args, **_kwargs: bad_local,
    )

    mineru_called = []
    def parse_remote(data, **kwargs):
        mineru_called.append(True)
        return _map([_item("text", "来自 MinerU 的完整表格文本")])

    monkeypatch.setattr(mineru_pdf, "parse_pdf_remote", parse_remote)

    source = import_parsing.build_pdf_import_source(b"%PDF-scan", library)
    assert len(mineru_called) == 1
    assert source["routing"]["selection"] == "mineru"
    assert "来自 MinerU" in source["normalized_text"]


def test_pdf_quality_cascade_local_exception_escalates_to_mineru(monkeypatch):
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=True)

    _mock_single_page_scan_preflight(monkeypatch)

    def fail_local(*_args, **_kwargs):
        raise import_parsing.pdf_extract.PdfExtractError("Simulated OCR failure")

    monkeypatch.setattr(import_parsing.pdf_extract, "build_pdf_source", fail_local)

    mineru_called = []
    def parse_remote(data, **kwargs):
        mineru_called.append(True)
        return _map([_item("text", "MinerU 成功承接")])

    monkeypatch.setattr(mineru_pdf, "parse_pdf_remote", parse_remote)

    source = import_parsing.build_pdf_import_source(b"%PDF-scan", library)
    assert len(mineru_called) == 1
    assert source["routing"]["selection"] == "mineru"
    assert "MinerU 成功承接" in source["normalized_text"]


def test_pdf_quality_cascade_mineru_failure_raises_explicitly(monkeypatch):
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=True)

    _mock_single_page_scan_preflight(monkeypatch)

    bad_local = _map([_item("text", "短")])
    bad_local["segments"][0]["quality"] = {
        "extraction_mode": "ocr",
        "visual_content_unparsed": False,
        "ocr_blocks": [],
    }
    monkeypatch.setattr(import_parsing.pdf_extract, "build_pdf_source", lambda *_a, **_kw: bad_local)

    def fail_remote(*_args, **_kwargs):
        raise mineru_pdf.MineruPdfError("MinerU 远端服务超时")

    monkeypatch.setattr(mineru_pdf, "parse_pdf_remote", fail_remote)

    with pytest.raises(mineru_pdf.MineruPdfError, match="MinerU 远端服务超时"):
        import_parsing.build_pdf_import_source(b"%PDF-scan", library)


def test_pdf_quality_cascade_multi_page_scan_skips_candidate(monkeypatch):
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=True)

    _mock_single_page_scan_preflight(monkeypatch, total_pages=2)

    monkeypatch.setattr(
        import_parsing.pdf_extract,
        "build_pdf_source",
        lambda *_args, **_kwargs: pytest.fail("Multi-page scan must not trigger local candidate"),
    )

    mineru_called = []
    def parse_remote(data, **kwargs):
        mineru_called.append(kwargs.get("total_pages"))
        return _map([_item("text", "p1"), _item("text", "p2", page_idx=1)])

    monkeypatch.setattr(mineru_pdf, "parse_pdf_remote", parse_remote)

    source = import_parsing.build_pdf_import_source(b"%PDF-multi", library)
    assert source["routing"]["selection"] == "mineru"


def test_pdf_quality_cascade_resource_limit_raises_without_calling_mineru(monkeypatch):
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=True)

    _mock_single_page_scan_preflight(monkeypatch)

    def raise_resource_limit(*_args, **_kwargs):
        raise import_parsing.pdf_extract.PdfResourceLimitError("PDF 像素超限")

    monkeypatch.setattr(import_parsing.pdf_extract, "build_pdf_source", raise_resource_limit)
    monkeypatch.setattr(
        mineru_pdf,
        "parse_pdf_remote",
        lambda *_a, **_kw: pytest.fail("MinerU must not be called on resource limit error"),
    )

    with pytest.raises(import_parsing.pdf_extract.PdfResourceLimitError, match="PDF 像素超限"):
        import_parsing.build_pdf_import_source(b"%PDF-oversized", library)


def test_pdf_quality_cascade_unexpected_error_raises_without_calling_mineru(monkeypatch):
    monkeypatch.setattr(settings, "mineru_pdf_base_url", "https://mineru.internal")
    monkeypatch.setattr(settings, "mineru_pdf_library_slugs", "target")
    monkeypatch.setattr(settings, "pdf_quality_cascade_enabled", True)
    library = SimpleNamespace(slug="target", chunk_size=200, chunk_overlap=0, ocr_enabled=True)

    _mock_single_page_scan_preflight(monkeypatch)

    def raise_unexpected_bug(*_args, **_kwargs):
        raise TypeError("Unexpected code bug in candidate pipeline")

    monkeypatch.setattr(import_parsing.pdf_extract, "build_pdf_source", raise_unexpected_bug)
    monkeypatch.setattr(
        mineru_pdf,
        "parse_pdf_remote",
        lambda *_a, **_kw: pytest.fail("MinerU must not be called on unexpected code bug"),
    )

    with pytest.raises(TypeError, match="Unexpected code bug in candidate pipeline"):
        import_parsing.build_pdf_import_source(b"%PDF-bug", library)
