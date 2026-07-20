from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Literal, Sequence

from app.services.graph_normalization import normalize_graph_name_v1


MICROS = 1_000_000
PRESENTATION_FLOOR_MICROS = 500_000
MAX_CANDIDATES = 10


@dataclass(frozen=True, slots=True)
class EntityLinkingCandidate:
    entity_id: uuid.UUID
    item_hash: str
    entity_type_id: uuid.UUID
    entity_type_key: str
    entity_type_label: str
    canonical_name: str
    normalized_name: str


@dataclass(frozen=True, slots=True)
class ScoredEntityLinkingCandidate:
    candidate: EntityLinkingCandidate
    score_micros: int


@dataclass(frozen=True, slots=True)
class EntityLinkingDecision:
    status: Literal["linked", "ambiguous", "not_found"]
    method: Literal["exact_canonical", "lexical_v1"] | None
    selected: ScoredEntityLinkingCandidate | None
    candidates: tuple[ScoredEntityLinkingCandidate, ...]


def ratio_micros(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0 or numerator > denominator:
        raise ValueError("ratio inputs must satisfy 0 <= numerator <= denominator")
    return (2 * numerator * MICROS + denominator) // (2 * denominator)


def _character_grams(value: str) -> Counter[str]:
    if len(value) == 1:
        return Counter((value,))
    return Counter(value[index : index + 2] for index in range(len(value) - 1))


def character_bigram_dice_micros(left: str, right: str) -> int:
    left_grams = _character_grams(left)
    right_grams = _character_grams(right)
    intersection = sum((left_grams & right_grams).values())
    return ratio_micros(
        2 * intersection,
        sum(left_grams.values()) + sum(right_grams.values()),
    )


def token_jaccard_micros(left: str, right: str) -> int:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    union = left_tokens | right_tokens
    return ratio_micros(len(left_tokens & right_tokens), len(union))


def substring_containment_micros(left: str, right: str) -> int:
    if left in right or right in left:
        return ratio_micros(min(len(left), len(right)), max(len(left), len(right)))
    return 0


def score_normalized_pair(left: str, right: str) -> int:
    if not left or not right:
        raise ValueError("normalized scorer inputs must be non-empty")
    return max(
        character_bigram_dice_micros(left, right),
        token_jaccard_micros(left, right),
        substring_containment_micros(left, right),
    )


def stable_candidate_key(
    value: ScoredEntityLinkingCandidate,
) -> tuple[int, str, str, str]:
    return (
        -value.score_micros,
        value.candidate.entity_type_key,
        value.candidate.normalized_name,
        str(value.candidate.entity_id).lower(),
    )


def resolve_mention(
    mention_text: str,
    candidates: Sequence[EntityLinkingCandidate],
    *,
    entity_type_key: str | None,
    min_score_micros: int,
    min_margin_micros: int,
    candidate_floor_micros: int,
    max_candidates: int,
) -> EntityLinkingDecision:
    if not 0 <= min_score_micros <= MICROS:
        raise ValueError("min_score_micros out of range")
    if not 0 <= min_margin_micros <= MICROS:
        raise ValueError("min_margin_micros out of range")
    if candidate_floor_micros != PRESENTATION_FLOOR_MICROS:
        raise ValueError("candidate floor is frozen")
    if max_candidates != MAX_CANDIDATES:
        raise ValueError("max candidates is frozen")
    normalized = normalize_graph_name_v1(mention_text)
    if not normalized:
        raise ValueError("mention normalizes to empty")
    filtered = tuple(
        row
        for row in candidates
        if entity_type_key is None or row.entity_type_key == entity_type_key
    )
    exact = tuple(
        sorted(
            (
                ScoredEntityLinkingCandidate(row, MICROS)
                for row in filtered
                if row.normalized_name == normalized
            ),
            key=stable_candidate_key,
        )
    )
    if len(exact) == 1:
        return EntityLinkingDecision("linked", "exact_canonical", exact[0], ())
    if exact:
        return EntityLinkingDecision("ambiguous", None, None, exact[:max_candidates])

    scored = tuple(
        sorted(
            (
                ScoredEntityLinkingCandidate(
                    row,
                    score_normalized_pair(normalized, row.normalized_name),
                )
                for row in filtered
            ),
            key=stable_candidate_key,
        )
    )
    return decide_scored_candidates(
        scored,
        min_score_micros=min_score_micros,
        min_margin_micros=min_margin_micros,
        candidate_floor_micros=candidate_floor_micros,
        max_candidates=max_candidates,
    )


def decide_scored_candidates(
    scored: Sequence[ScoredEntityLinkingCandidate],
    *,
    min_score_micros: int,
    min_margin_micros: int,
    candidate_floor_micros: int = PRESENTATION_FLOOR_MICROS,
    max_candidates: int = MAX_CANDIDATES,
) -> EntityLinkingDecision:
    if not 0 <= min_score_micros <= MICROS:
        raise ValueError("min_score_micros out of range")
    if not 0 <= min_margin_micros <= MICROS:
        raise ValueError("min_margin_micros out of range")
    if candidate_floor_micros != PRESENTATION_FLOOR_MICROS:
        raise ValueError("candidate floor is frozen")
    if max_candidates != MAX_CANDIDATES:
        raise ValueError("max candidates is frozen")
    ordered = tuple(sorted(scored, key=stable_candidate_key))
    presented = tuple(
        row for row in ordered if row.score_micros >= candidate_floor_micros
    )[:max_candidates]
    if not presented:
        return EntityLinkingDecision("not_found", None, None, ())
    top_score = presented[0].score_micros
    second_score = presented[1].score_micros if len(presented) > 1 else 0
    if top_score < min_score_micros or top_score - second_score < min_margin_micros:
        return EntityLinkingDecision("ambiguous", None, None, presented)
    return EntityLinkingDecision("linked", "lexical_v1", presented[0], ())
