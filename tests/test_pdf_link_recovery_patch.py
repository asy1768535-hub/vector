"""Synthetic link metadata regression; no business PDFs or OCR results."""
import inspect
from io import BytesIO

import pypdf
import pypdf.generic._link as links
import pytest
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NullObject, NumberObject, TextStringObject

from deploy.patch_pdf_link_actions import guard_invalid_pdf_link_actions


@pytest.mark.parametrize("null_action", [True, False])
def test_pdf_null_link_action_preserves_annotation(monkeypatch, null_action):
    namespace = dict(vars(links))
    source = inspect.getsource(links._build_link)
    exec(compile(guard_invalid_pdf_link_actions(source), "<link-guard>", "exec"), namespace)
    monkeypatch.setattr(links, "_build_link", namespace["_build_link"])
    action = NullObject() if null_action else DictionaryObject({
        NameObject("/S"): NameObject("/URI"),
        NameObject("/URI"): TextStringObject("https://example.invalid/"),
    })
    annotation = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Link"),
        NameObject("/Rect"): ArrayObject([NumberObject(n) for n in (0, 0, 20, 20)]),
        NameObject("/Contents"): TextStringObject("synthetic annotation"),
        NameObject("/A"): action,
    })
    original = BytesIO()
    with pypdf.PdfWriter() as creator:
        page = creator.add_blank_page(width=72, height=72)
        page[NameObject("/Annots")] = ArrayObject([creator._add_object(annotation)])
        creator.write(original)
    original_bytes = original.getvalue()
    reader = pypdf.PdfReader(BytesIO(original_bytes))
    with pypdf.PdfWriter() as writer:
        writer.add_page(reader.pages[0])
        copied = BytesIO()
        writer.write(copied)
    output = pypdf.PdfReader(BytesIO(copied.getvalue()))
    assert len(output.pages) == 1
    preserved = output.pages[0]["/Annots"][0].get_object()
    assert preserved["/Contents"] == "synthetic annotation"
    assert list(preserved["/Rect"]) == [0, 0, 20, 20]
    if null_action:
        assert isinstance(preserved["/A"], NullObject)
        assert isinstance(reader.pages[0]["/Annots"][0].get_object()["/A"], NullObject)
    else:
        assert preserved["/A"] == action
    assert original.getvalue() == original_bytes


def test_pdf_link_patch_rejects_source_drift():
    with pytest.raises(RuntimeError, match="Unexpected pypdf link builder"):
        guard_invalid_pdf_link_actions("unsupported source")
