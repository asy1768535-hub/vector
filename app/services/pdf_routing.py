"""Deterministic PDF parser routing and non-text provenance."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypedDict

from app.services.parser_units import build_parser_unit
from app.services.pdf_preflight import validate_pdf_preflight_report

PDF_ROUTING_CONTRACT_VERSION = "pdf-routing-v1"
PDF_ROUTING_UNIT_KEY = "pdf:routing:v1"
PDF_ROUTING_MAX_REASONS = 8

PdfRoutingSelection = Literal["native_or_rapidocr", "mineru"]
PdfRoutingReason = Literal[
    "mineru_not_authorized",
    "mixed_content_detected",
    "multiple_ocr_pages",
    "native_text_sufficient",
    "no_enhancement_signal",
    "preflight_unknown",
    "resource_limit",
    "scan_page_detected",
]
PDF_ROUTING_REASONS = frozenset(
    {
        "mineru_not_authorized",
        "mixed_content_detected",
        "multiple_ocr_pages",
        "native_text_sufficient",
        "no_enhancement_signal",
        "preflight_unknown",
        "resource_limit",
        "scan_page_detected",
    }
)


class PdfRoutingDecisionV1(TypedDict):
    contract_version: Literal["pdf-routing-v1"]
    selection: PdfRoutingSelection
    needs_review: bool
    reasons: list[PdfRoutingReason]


def validate_pdf_routing_decision(value: object) -> PdfRoutingDecisionV1:
    """Validate and copy a routing decision without retaining extra fields."""
    if not isinstance(value, Mapping):
        raise ValueError("PDF routing decision must be an object")
    if set(value) != {"contract_version", "selection", "needs_review", "reasons"}:
        raise ValueError("PDF routing decision fields are invalid")
    if value.get("contract_version") != PDF_ROUTING_CONTRACT_VERSION:
        raise ValueError("PDF routing decision version is invalid")
    selection = value.get("selection")
    if selection not in {"native_or_rapidocr", "mineru"}:
        raise ValueError("PDF routing selection is invalid")
    needs_review = value.get("needs_review")
    if not isinstance(needs_review, bool):
        raise ValueError("PDF routing review flag is invalid")
    raw_reasons = value.get("reasons")
    if (
        not isinstance(raw_reasons, list)
        or not raw_reasons
        or len(raw_reasons) > PDF_ROUTING_MAX_REASONS
        or raw_reasons != sorted(set(raw_reasons))
        or any(
            not isinstance(reason, str) or reason not in PDF_ROUTING_REASONS
            for reason in raw_reasons
        )
    ):
        raise ValueError("PDF routing reasons are invalid")
    reasons = list(raw_reasons)
    if selection == "mineru" and "mineru_not_authorized" in reasons:
        raise ValueError("unauthorized PDF routing cannot select MinerU")
    if selection == "mineru" and (
        "preflight_unknown" in reasons or "resource_limit" in reasons
    ):
        raise ValueError("uncertain PDF routing cannot select MinerU")
    return {
        "contract_version": PDF_ROUTING_CONTRACT_VERSION,
        "selection": selection,
        "needs_review": needs_review,
        "reasons": reasons,
    }


def _decision(
    selection: PdfRoutingSelection,
    *,
    needs_review: bool,
    reasons: Iterable[PdfRoutingReason],
) -> PdfRoutingDecisionV1:
    return validate_pdf_routing_decision(
        {
            "contract_version": PDF_ROUTING_CONTRACT_VERSION,
            "selection": selection,
            "needs_review": needs_review,
            "reasons": sorted(set(reasons)),
        }
    )


def choose_pdf_route(
    preflight: object,
    *,
    mineru_authorized: bool,
) -> PdfRoutingDecisionV1:
    """Choose one document-level parser from bounded preflight facts."""
    if not isinstance(mineru_authorized, bool):
        raise ValueError("mineru_authorized must be a boolean")
    report = validate_pdf_preflight_report(preflight)
    authorization_reasons: list[PdfRoutingReason] = (
        [] if mineru_authorized else ["mineru_not_authorized"]
    )
    if report["status"] != "complete":
        reasons: list[PdfRoutingReason] = [
            *authorization_reasons,
            "preflight_unknown",
        ]
        if (
            report["unknown_reason"] == "resource_limit"
            or report["page_limit_exceeded"]
            or report["image_limit_exceeded"]
        ):
            reasons.append("resource_limit")
        return _decision(
            "native_or_rapidocr",
            needs_review=True,
            reasons=reasons,
        )

    scan_pages = [
        page
        for page in report["pages"]
        if page["low_text"] and page["has_visual_content"]
    ]
    enhancement_reasons: list[PdfRoutingReason] = []
    if scan_pages:
        enhancement_reasons.append("scan_page_detected")
    if len(scan_pages) > 1:
        enhancement_reasons.append("multiple_ocr_pages")
    if report["has_mixed_content"]:
        enhancement_reasons.append("mixed_content_detected")

    if enhancement_reasons:
        if mineru_authorized:
            return _decision(
                "mineru",
                needs_review=False,
                reasons=enhancement_reasons,
            )
        return _decision(
            "native_or_rapidocr",
            needs_review=True,
            reasons=[*authorization_reasons, *enhancement_reasons],
        )

    all_native = bool(report["pages"]) and all(
        not page["low_text"] and not page["has_visual_content"]
        for page in report["pages"]
    )
    return _decision(
        "native_or_rapidocr",
        needs_review=False,
        reasons=[
            *authorization_reasons,
            "native_text_sufficient" if all_native else "no_enhancement_signal",
        ],
    )


def attach_pdf_routing_unit(
    segments: list[dict[str, Any]],
    decision: object,
) -> PdfRoutingDecisionV1:
    """Attach one empty-text routing unit below the first PDF parser root."""
    validated = validate_pdf_routing_decision(decision)
    if not segments:
        raise ValueError("PDF routing requires at least one parser segment")
    first = segments[0]
    root = first.get("parser_unit")
    if not isinstance(root, Mapping):
        raise ValueError("PDF routing requires a root parser unit")
    parser = root.get("parser")
    parent_key = root.get("unit_key")
    if not isinstance(parser, Mapping) or not isinstance(parent_key, str):
        raise ValueError("PDF routing root parser unit is invalid")

    unit = build_parser_unit(
        source_kind="pdf",
        unit_kind="structured_unit",
        ordinal=1,
        unit_key=PDF_ROUTING_UNIT_KEY,
        parser=parser,
        parent_key=parent_key,
    )
    unit["value"] = validated  # type: ignore[typeddict-unknown-key]
    unit["text"] = ""
    unit["structure_type"] = "pdf_routing"  # type: ignore[typeddict-unknown-key]
    structured_units = first.setdefault("structured_units", [])
    if not isinstance(structured_units, list):
        raise ValueError("parser structured_units must be a list")
    structured_units[:] = [
        existing
        for existing in structured_units
        if not (
            isinstance(existing, Mapping)
            and existing.get("unit_key") == PDF_ROUTING_UNIT_KEY
        )
    ]
    structured_units.append(unit)
    return validated
