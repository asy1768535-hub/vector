"""Apply only the recovery delta while building an isolated production-base image.

Run in a Docker build layer, never against a running service. Existing unrelated
source changes are retained. A missing/ambiguous anchor fails the build.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError('Recovery patch anchor is missing or ambiguous')
    return source.replace(old, new, 1)


def function_delta(source, name, changes):
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    lines = source.splitlines(keepends=True)
    body = ''.join(lines[node.lineno - 1:node.end_lineno])
    for old, new in changes:
        body = replace_once(body, old, new)
    return ''.join(lines[:node.lineno - 1]) + body + ''.join(lines[node.end_lineno:])


def main(root):
    root = Path(root)
    outputs = {}
    path = root / 'app/config.py'
    outputs[path] = replace_once(path.read_text(), '    pdf_ocr_max_pages: int = 50\n',
        '    pdf_ocr_max_pages: int = 50\n    # Isolated recovery workers only: bounded page batches and adaptive rendering.\n    pdf_resource_recovery_enabled: bool = False\n')
    path = root / 'app/services/import_parsing.py'
    source = path.read_text()
    import re
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)
             and getattr(node.func, 'attr', '') == 'build_pdf_source']
    if len(calls) not in (1, 3):
        raise RuntimeError('Unrecognized PDF parser call topology')
    source, count = re.subn(r'(?m)^([ \t]*)max_ocr_pages=settings.pdf_ocr_max_pages,$',
        r'\1max_ocr_pages=settings.pdf_ocr_max_pages,\n\1resource_recovery=settings.pdf_resource_recovery_enabled,', source)
    if count != len(calls):
        raise RuntimeError('Unexpected PDF parser call count')
    outputs[path] = source
    path = root / 'app/services/pdf_extract.py'
    source = function_delta(path.read_text(), '_extract_pdf_parts', [
        ('    coverage_state: dict[str, Any] | None = None,\n',
         '    coverage_state: dict[str, Any] | None = None,\n    adaptive_render: bool = False,\n    allow_empty: bool = False,\n'),
        ('        meaningful_chars = _meaningful_char_count(text)\n',
         '        if adaptive_render:\n'
         '            try:\n'
         '                text.encode("utf-8")\n'
         '            except UnicodeEncodeError:\n'
         '                if not ocr_enabled or ocr is None:\n'
         '                    raise PdfExtractError("PDF native text encoding requires OCR recovery") from None\n'
         '                # Treat invalid native decoding as a page extraction failure.\n'
         '                # Recognize the original rendered page; never drop/replace glyphs.\n'
         '                text = ""\n'
         '        meaningful_chars = _meaningful_char_count(text)\n'),
        ('        effective_render_dpi = _resolve_page_render_dpi(page, render_dpi)\n',
         '        effective_render_dpi = _resolve_page_render_dpi(page, render_dpi)\n'
         '        if adaptive_render and page_dimensions is not None:\n'
         '            from app.services.pdf_resource_recovery import fit_render_dpi\n\n'
         '            effective_render_dpi = fit_render_dpi(page_dimensions, effective_render_dpi)\n'
         '            if coverage_state is not None:\n'
         '                coverage_state.setdefault("render_dpi_by_page", {})[idx + 1] = effective_render_dpi\n'),
        ('    if not parts:\n', '    if not parts and not allow_empty:\n'),
        ('        png = _render_page_png(data, idx, effective_render_dpi)\n',
         '        while True:\n'
         '            try:\n'
         '                png = _render_page_png(data, idx, effective_render_dpi)\n'
         '                break\n'
         '            except PdfResourceLimitError as exc:\n'
         '                if not adaptive_render or str(exc) != "PDF resource limit exceeded: rendered image bytes":\n'
         '                    raise\n'
         '                from app.services.pdf_resource_recovery import MIN_RENDER_DPI\n\n'
         '                if effective_render_dpi <= MIN_RENDER_DPI:\n'
         '                    raise\n'
         '                # High-entropy images can meet the pixel budget but exceed PNG bytes.\n'
         '                effective_render_dpi = max(MIN_RENDER_DPI, int(effective_render_dpi * 0.8))\n'
         '        if adaptive_render and coverage_state is not None:\n'
         '            coverage_state.setdefault("render_dpi_by_page", {})[idx + 1] = effective_render_dpi\n'),
    ])
    source = function_delta(source, 'build_pdf_source', [
        ('    preflight_report: Mapping[str, Any] | None = None,\n',
         '    preflight_report: Mapping[str, Any] | None = None,\n    resource_recovery: bool = False,\n'),
        ('    parts = _extract_pdf_parts(\n',
         '    extract_parts = _extract_pdf_parts\n    if resource_recovery:\n'
         '        from app.services.pdf_resource_recovery import extract_pdf_parts_batched\n\n'
         '        extract_parts = extract_pdf_parts_batched\n    parts = extract_parts(\n'),
        ('            "max_ocr_pages": max_ocr_pages,\n',
         '            "max_ocr_pages": max_ocr_pages,\n'
         '            **({"resource_recovery": True, "page_batch_size": min(25, max_ocr_pages)} if resource_recovery else {}),\n'),
        ('        if extraction_mode == "native_visual_unparsed":\n',
         '        effective_dpi = coverage_state.get("render_dpi_by_page", {}).get(page_index + 1)\n'
         '        if effective_dpi is not None:\n            quality["render_dpi"] = effective_dpi\n'
         '        if extraction_mode == "native_visual_unparsed":\n'),
    ])
    outputs[path] = source
    for path, source in outputs.items():
        ast.parse(source)
    for path, source in outputs.items():
        path.write_text(source)
    print(json.dumps({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in outputs}))


if __name__ == '__main__':
    import sys
    main(sys.argv[1])
