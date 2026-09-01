from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.services.classification_decision_contracts import (
    CLASSIFICATION_MAX_SECONDARY_LABELS,
    CLASSIFICATION_MIN_CONFIDENCE_MICROS,
    CLASSIFICATION_MIN_MARGIN_MICROS,
    CLASSIFICATION_REASON_ORDER,
    ClassifierProposalInput,
)


@dataclass(frozen=True, slots=True)
class KnownLabelPolicyState:
    label_id: uuid.UUID
    active: bool
    enabled: bool


@dataclass(frozen=True, slots=True)
class ClassificationPolicyEvaluation:
    run_status: str
    run_reason_codes: tuple[str, ...]
    proposal_statuses: tuple[str, ...]
    proposal_reason_codes: tuple[tuple[str, ...], ...]
    selected_primary_index: int | None
    selected_secondary_indices: tuple[int, ...]


def _ordered_reasons(values: set[str]) -> tuple[str, ...]:
    return tuple(code for code in CLASSIFICATION_REASON_ORDER if code in values)


def evaluate_classification_policy(
    proposals: tuple[ClassifierProposalInput, ...],
    *,
    known_labels: dict[uuid.UUID, KnownLabelPolicyState],
    has_manual_decision_protection: bool,
) -> ClassificationPolicyEvaluation:
    per_item: list[set[str]] = [set() for _ in proposals]
    run_reasons: set[str] = set()
    seen_labels: set[uuid.UUID] = set()

    primary_indices = [index for index, item in enumerate(proposals) if item.role == "primary"]
    secondary_indices = [
        index for index, item in enumerate(proposals) if item.role == "secondary"
    ]
    if not primary_indices:
        run_reasons.add("missing_primary")
    if len(secondary_indices) > CLASSIFICATION_MAX_SECONDARY_LABELS:
        run_reasons.add("secondary_limit_exceeded")

    for index, proposal in enumerate(proposals):
        if proposal.label_id is None:
            per_item[index].add("unknown_label")
            run_reasons.add("unknown_label")
            continue
        state = known_labels.get(proposal.label_id)
        if state is None or not state.active:
            per_item[index].add("label_disabled")
            run_reasons.add("label_disabled")
        elif not state.enabled:
            per_item[index].add("label_not_enabled")
            run_reasons.add("label_not_enabled")
        if proposal.label_id in seen_labels:
            per_item[index].add("duplicate_label")
            run_reasons.add("duplicate_label")
        seen_labels.add(proposal.label_id)

    selected_primary_index = primary_indices[0] if primary_indices else None
    if selected_primary_index is not None:
        primary = proposals[selected_primary_index]
        if primary.confidence_micros < CLASSIFICATION_MIN_CONFIDENCE_MICROS:
            per_item[selected_primary_index].add("primary_confidence_below_threshold")
        if len(primary_indices) > 1:
            runner_up = proposals[primary_indices[1]]
            if (
                primary.confidence_micros - runner_up.confidence_micros
                < CLASSIFICATION_MIN_MARGIN_MICROS
            ):
                per_item[selected_primary_index].add("primary_margin_below_threshold")
                per_item[primary_indices[1]].add("primary_margin_below_threshold")

    selected_secondary_indices: list[int] = []
    for index in secondary_indices:
        if proposals[index].confidence_micros < CLASSIFICATION_MIN_CONFIDENCE_MICROS:
            per_item[index].add("secondary_confidence_below_threshold")
        else:
            selected_secondary_indices.append(index)

    ordered_run_reasons = _ordered_reasons(run_reasons)
    ordered_item_reasons = tuple(_ordered_reasons(values) for values in per_item)
    if ordered_run_reasons:
        return ClassificationPolicyEvaluation(
            run_status="pending_review",
            run_reason_codes=ordered_run_reasons,
            proposal_statuses=tuple("pending_review" for _ in proposals),
            proposal_reason_codes=ordered_item_reasons,
            selected_primary_index=selected_primary_index,
            selected_secondary_indices=tuple(selected_secondary_indices),
        )
    if has_manual_decision_protection:
        return ClassificationPolicyEvaluation(
            run_status="blocked_manual",
            run_reason_codes=(),
            proposal_statuses=tuple("blocked_manual" for _ in proposals),
            proposal_reason_codes=ordered_item_reasons,
            selected_primary_index=selected_primary_index,
            selected_secondary_indices=tuple(selected_secondary_indices),
        )
    selected = {selected_primary_index, *selected_secondary_indices}
    return ClassificationPolicyEvaluation(
        run_status="auto_applied",
        run_reason_codes=(),
        proposal_statuses=tuple(
            "auto_selected" if index in selected else "not_selected"
            for index in range(len(proposals))
        ),
        proposal_reason_codes=ordered_item_reasons,
        selected_primary_index=selected_primary_index,
        selected_secondary_indices=tuple(selected_secondary_indices),
    )
