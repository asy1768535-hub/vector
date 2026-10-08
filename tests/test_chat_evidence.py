from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.schemas.dify import DifyRecord
from app.services.chat_evidence import pack_required_evidence, resolve_file_mentions


def _document(title):
    return SimpleNamespace(id=uuid.uuid4(), title=title, display_name=None)


def _record(text, title="file"):
    return DifyRecord(content=text, title=title, score=0.9, metadata={"chunk_id": str(uuid.uuid4())})


def test_full_filename_keeps_extension_identity_and_requested_order():
    first, second, same_stem = [_document(title) for title in ("Alpha年度清单.xlsx", "Beta情况说明.pdf", "Beta情况说明.docx")]
    selected = resolve_file_mentions("对比《Beta情况说明.pdf》和《Alpha年度清单.xlsx》", [first, second, same_stem])
    assert selected.document_ids == [second.id, first.id]
    assert selected.explicit and not selected.notices


def test_unique_abbreviation_and_reordered_title_tokens():
    doc = _document("Alpha年度清单完整版.xlsx")
    selected = resolve_file_mentions("比较《年度 Alpha 清单》", [doc, _document("Beta其他台账.pdf")])
    assert selected.document_ids == [doc.id]
    assert selected.explicit and not selected.notices


def test_ambiguous_abbreviation_requires_clarification():
    selected = resolve_file_mentions("对比《年度清单》", [_document("Alpha年度清单.xlsx"), _document("Beta年度清单.xlsx")])
    assert selected.explicit and selected.notices
    assert selected.document_ids == []


def test_plain_missing_filename_is_not_replaced_with_topic_search():
    selected = resolve_file_mentions("请核对 missing.pdf", [_document("Other说明.pdf")])
    assert selected.explicit and selected.notices
    assert selected.document_ids == []


def test_plain_two_filenames_are_separately_resolved():
    first, second = _document("Alpha记录.pdf"), _document("Beta台账.xlsx")
    selected = resolve_file_mentions("对比Alpha记录.pdf和Beta台账.xlsx", [first, second])
    assert selected.document_ids == [first.id, second.id]
    assert not selected.notices


def test_missing_plain_file_is_not_ignored_alongside_quoted_file():
    first = _document("Alpha记录.pdf")
    selected = resolve_file_mentions("对比《Alpha记录.pdf》与missing.pdf", [first])
    assert selected.document_ids == [first.id]
    assert selected.notices


def test_unquoted_full_filename_with_spaces_is_not_reduced_to_ambiguous_suffix():
    first, second = _document("Alpha Annual Report.pdf"), _document("Beta Annual Report.pdf")
    selected = resolve_file_mentions("Read Alpha Annual Report.pdf", [first, second])
    assert selected.document_ids == [first.id]
    assert not selected.notices


def test_quoted_fact_is_not_a_filename():
    selected = resolve_file_mentions("材料中的“可能发生”代表什么", [_document("Alpha记录.pdf")])
    assert not selected.explicit


def test_required_groups_are_reserved_before_optional_ranked_text():
    first, second, noise = _record("必要甲"), _record("必要乙"), _record("噪声" * 100)
    packed, notices = pack_required_evidence([[first], [second]], [noise, first], 30)
    assert packed == [first, second]
    assert not notices


def test_required_group_is_not_claimed_complete_after_truncation():
    group = [_record("一" * 10), _record("二" * 10)]
    packed, notices = pack_required_evidence([group], [], 20)
    assert not packed and notices


def test_stricter_configuration_and_twenty_source_limit_are_respected():
    packed, notices = pack_required_evidence([[_record("x") for _ in range(21)]], [], 12000)
    assert not packed and notices
    packed, notices = pack_required_evidence([[_record("x")]], [], 1)
    assert not packed and notices
