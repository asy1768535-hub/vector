import sys
import types

from app.services import pdf_extract


def test_pdf_renderer_retries_one_transient_render_failure(monkeypatch) -> None:
    calls = []

    class Image:
        def save(self, output, *, format):
            assert format == "PNG"
            output.write(b"png")

    class Bitmap:
        def to_pil(self):
            return Image()

    class Page:
        def get_size(self):
            return 72, 72

        def render(self, *, scale):
            assert scale == 1
            return Bitmap()

    class Document:
        def __getitem__(self, index):
            assert index == 0
            return Page()

        def close(self):
            return None

    def open_document(_data):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient renderer failure")
        return Document()

    monkeypatch.setitem(sys.modules, "pypdfium2", types.SimpleNamespace(PdfDocument=open_document))

    assert pdf_extract._render_page_png(b"pdf", 0, 72) == b"png"
    assert len(calls) == 2
