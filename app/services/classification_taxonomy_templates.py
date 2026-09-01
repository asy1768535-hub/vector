from __future__ import annotations

from dataclasses import dataclass

from app.services.classification_runtime_contracts import canonical_sha256
from app.services.classification_taxonomy_bootstrap_contracts import (
    DraftLabelInput,
    NormalizedTaxonomyDraft,
    TaxonomyBootstrapError,
    normalize_taxonomy_draft,
)


@dataclass(frozen=True, slots=True)
class TaxonomyBootstrapTemplate:
    key: str
    version: str
    description: str
    draft: NormalizedTaxonomyDraft
    template_hash: str


def _template(
    *,
    key: str,
    version: str,
    description: str,
    labels: tuple[DraftLabelInput, ...],
) -> TaxonomyBootstrapTemplate:
    draft = normalize_taxonomy_draft(labels)
    template_hash = canonical_sha256(
        {
            "key": key,
            "version": version,
            "description": description,
            "labels_hash": draft.payload_hash,
        }
    )
    return TaxonomyBootstrapTemplate(
        key=key,
        version=version,
        description=description,
        draft=draft,
        template_hash=template_hash,
    )


GENERAL_ENTERPRISE_V1 = _template(
    key="general-enterprise",
    version="1.0.0",
    description="General starter categories for internal enterprise documents.",
    labels=(
        DraftLabelInput(
            "governance-policy",
            "\u5236\u5ea6\u4e0e\u6cbb\u7406",
            "Policies, rules, procedures, and internal governance materials.",
            sort_order=10,
        ),
        DraftLabelInput(
            "contracts-legal",
            "\u5408\u540c\u4e0e\u6cd5\u52a1",
            "Contracts, legal opinions, agreements, and dispute materials.",
            sort_order=20,
        ),
        DraftLabelInput(
            "finance-tax",
            "\u8d22\u52a1\u4e0e\u7a0e\u52a1",
            "Financial statements, invoices, tax, payments, and accounting materials.",
            sort_order=30,
        ),
        DraftLabelInput(
            "human-resources",
            "\u4eba\u529b\u8d44\u6e90",
            "Employment, payroll, attendance, benefits, and personnel materials.",
            sort_order=40,
        ),
        DraftLabelInput(
            "projects-operations",
            "\u9879\u76ee\u4e0e\u8fd0\u8425",
            "Project delivery, operational records, procurement, and work reports.",
            sort_order=50,
        ),
        DraftLabelInput(
            "safety-compliance",
            "\u5b89\u5168\u4e0e\u5408\u89c4",
            "Safety controls, inspections, compliance evidence, and risk materials.",
            sort_order=60,
        ),
        DraftLabelInput(
            "technology-product",
            "\u6280\u672f\u4e0e\u4ea7\u54c1",
            "Technical designs, systems, products, data, and engineering materials.",
            sort_order=70,
        ),
        DraftLabelInput(
            "reference-other",
            "\u53c2\u8003\u4e0e\u5176\u4ed6",
            "Reference information that does not yet fit another reviewed category.",
            sort_order=80,
        ),
    ),
)


_TEMPLATES = {GENERAL_ENTERPRISE_V1.key: GENERAL_ENTERPRISE_V1}


def list_taxonomy_bootstrap_templates() -> tuple[TaxonomyBootstrapTemplate, ...]:
    return tuple(_TEMPLATES[key] for key in sorted(_TEMPLATES))


def get_taxonomy_bootstrap_template(key: str) -> TaxonomyBootstrapTemplate:
    template = _TEMPLATES.get(key)
    if template is None:
        raise TaxonomyBootstrapError(
            "bootstrap_template_not_found",
            "taxonomy bootstrap template was not found",
        )
    return template
