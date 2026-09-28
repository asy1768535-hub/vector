"""Opt-in resource recovery, retaining one original document and page numbering."""
from __future__ import annotations

import math
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory

import pypdf

from app.services import pdf_extract

MAX_RECOVERY_PAGES = 2000
PAGE_BATCH_SIZE = 25
MIN_RENDER_DPI = 72


def fit_render_dpi(dimensions: tuple[float, float], requested: int) -> int:
    """Bound rendered pixels before allocation; refuse unreadably small scales."""
    width, height = dimensions
    if not all(math.isfinite(v) and v > 0 for v in (width, height, requested)):
        pdf_extract._resource_limit("page_dimensions")
    dpi = min(int(requested), int(72 * math.sqrt(pdf_extract.PDF_MAX_RENDER_PIXELS / (width * height))))
    while dpi >= MIN_RENDER_DPI:
        if math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72) <= pdf_extract.PDF_MAX_RENDER_PIXELS:
            return dpi
        dpi -= 1
    pdf_extract._resource_limit("render_pixels")


def extract_pdf_parts_batched(
    data, *, ocr_enabled, ocr, min_text_chars, render_dpi, max_ocr_pages,
    coverage_state=None,
):
    """Parse temporary page batches; publish no source until all batches succeed.

    Source bytes are never overwritten. Per-batch image/OCR budgets remain in
    the existing extractor; output character and block budgets span the entire
    original document. This is bounded batching, not resumable checkpoints.
    """
    if max_ocr_pages < 1:
        raise pdf_extract.PdfExtractError("PDF recovery requires a positive OCR page budget")
    reader = None
    parts = []
    total_chars = total_blocks = 0
    unprocessed = []
    render_dpis = {}
    visited = 0
    try:
        reader = pdf_extract._open_reader(data)
        # Validate the page-tree count before flattening a potentially huge tree.
        page_tree = reader.root_object['/Pages'].get_object()
        count = int(page_tree['/Count'])
        if count < 1 or count > MAX_RECOVERY_PAGES:
            pdf_extract._resource_limit("page_count")
        if len(reader.pages) != count:
            raise pdf_extract.PdfExtractError("PDF page count mismatch")
        page_items = iter(enumerate(reader.pages))
        batch_size = min(PAGE_BATCH_SIZE, max_ocr_pages)
        with TemporaryDirectory(prefix='pdf-resource-recovery-') as directory:
            while batch := list(islice(page_items, batch_size)):
                start = batch[0][0]
                path = Path(directory) / f'pages-{start + 1}.pdf'
                writer = pypdf.PdfWriter()
                try:
                    for _, page in batch:
                        writer.add_page(page)
                    with path.open('wb') as stream:
                        writer.write(stream)
                finally:
                    writer.close()
                state = {}
                batch_parts = pdf_extract._extract_pdf_parts(
                    path, ocr_enabled=ocr_enabled, ocr=ocr,
                    min_text_chars=min_text_chars, render_dpi=render_dpi,
                    max_ocr_pages=max_ocr_pages, coverage_state=state,
                    adaptive_render=True, allow_empty=True,
                )
                path.unlink()
                if state.get('total_pages') != len(batch):
                    raise pdf_extract.PdfExtractError("PDF batch page coverage mismatch")
                visited += len(batch)
                for index, value, mode, blocks in batch_parts:
                    total_chars += len(value)
                    total_blocks += len(blocks)
                    if total_chars > pdf_extract.PDF_MAX_NORMALIZED_TEXT_CHARS:
                        pdf_extract._resource_limit('normalized_text')
                    if total_blocks > pdf_extract.PDF_MAX_OCR_BLOCKS_TOTAL:
                        pdf_extract._resource_limit('ocr_blocks')
                    parts.append((index + start, value, mode, blocks))
                unprocessed.extend(start + p for p in state.get('unprocessed_visual_pages', []))
                render_dpis.update({start + p: dpi for p, dpi in state.get('render_dpi_by_page', {}).items()})
        if visited != count:
            raise pdf_extract.PdfExtractError("PDF document page coverage mismatch")
        if not parts:
            raise pdf_extract.PdfExtractError("PDF recovery produced no extractable text")
        if coverage_state is not None:
            coverage_state.update(total_pages=visited, unprocessed_visual_pages=sorted(set(unprocessed)),
                                  render_dpi_by_page=render_dpis)
        return parts
    except pdf_extract.PdfExtractError:
        raise
    except (MemoryError, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise pdf_extract.PdfExtractError("PDF resource recovery failed") from None
    finally:
        if reader is not None:
            reader.close()
