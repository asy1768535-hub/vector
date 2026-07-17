from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal, Sequence

from app.services.graph_normalization import normalize_graph_name_v1


ALGORITHM_VERSION = "lexical-score-v1"
ROUNDING_VERSION = "integer-half-up-v1"
NORMALIZATION_VERSION = "normalize_graph_name_v1"
MICROS = 1_000_000
PRESENTATION_FLOOR_MICROS = 500_000
MAX_CANDIDATES = 10


@dataclass(frozen=True, slots=True)
class EntityCandidate:
    entity_id: str
    entity_key: str
    entity_type_key: str
    canonical_name: str
    normalized_name: str

    def __post_init__(self) -> None:
        if not self.entity_id or not self.entity_key or not self.entity_type_key:
            raise ValueError("candidate identity fields must be non-empty")
        if not self.normalized_name:
            raise ValueError("candidate normalized_name must be non-empty")
        if normalize_graph_name_v1(self.canonical_name) != self.normalized_name:
            raise ValueError("candidate normalized_name invariant failed")


@dataclass(frozen=True, slots=True)
class FeatureScores:
    character_bigram_dice_micros: int
    token_jaccard_micros: int
    substring_containment_micros: int
    score_micros: int


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: EntityCandidate
    features: FeatureScores


@dataclass(frozen=True, slots=True)
class LinkDecision:
    status: Literal["linked", "ambiguous", "not_found"]
    method: Literal["exact_canonical", "lexical_v1"] | None
    selected: ScoredCandidate | None
    candidates: tuple[ScoredCandidate, ...]


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
    numerator = 2 * intersection
    denominator = sum(left_grams.values()) + sum(right_grams.values())
    return ratio_micros(numerator, denominator)


def token_jaccard_micros(left: str, right: str) -> int:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    union = left_tokens | right_tokens
    return ratio_micros(len(left_tokens & right_tokens), len(union))


def substring_containment_micros(left: str, right: str) -> int:
    if left in right or right in left:
        return ratio_micros(min(len(left), len(right)), max(len(left), len(right)))
    return 0


def score_normalized_pair(left: str, right: str) -> FeatureScores:
    if not left or not right:
        raise ValueError("normalized scorer inputs must be non-empty")
    bigram = character_bigram_dice_micros(left, right)
    jaccard = token_jaccard_micros(left, right)
    containment = substring_containment_micros(left, right)
    return FeatureScores(
        character_bigram_dice_micros=bigram,
        token_jaccard_micros=jaccard,
        substring_containment_micros=containment,
        score_micros=max(bigram, jaccard, containment),
    )


def score_candidate(mention_text: str, candidate: EntityCandidate) -> ScoredCandidate:
    mention = normalize_graph_name_v1(mention_text)
    if not mention:
        raise ValueError("mention normalizes to empty")
    return ScoredCandidate(candidate, score_normalized_pair(mention, candidate.normalized_name))


def stable_candidate_key(value: ScoredCandidate) -> tuple[int, str, str, str]:
    return (
        -value.features.score_micros,
        value.candidate.entity_type_key,
        value.candidate.normalized_name,
        value.candidate.entity_id.lower(),
    )


def _exact_scored(candidate: EntityCandidate) -> ScoredCandidate:
    exact = FeatureScores(MICROS, MICROS, MICROS, MICROS)
    return ScoredCandidate(candidate, exact)


def _filtered_candidates(
    candidates: Sequence[EntityCandidate],
    entity_type_key: str | None,
) -> tuple[EntityCandidate, ...]:
    if entity_type_key is None:
        return tuple(candidates)
    return tuple(row for row in candidates if row.entity_type_key == entity_type_key)


def resolve_exact_only(
    mention_text: str,
    candidates: Sequence[EntityCandidate],
    *,
    entity_type_key: str | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> LinkDecision:
    normalized = normalize_graph_name_v1(mention_text)
    if not normalized:
        raise ValueError("mention normalizes to empty")
    exact = sorted(
        (
            _exact_scored(row)
            for row in _filtered_candidates(candidates, entity_type_key)
            if row.normalized_name == normalized
        ),
        key=stable_candidate_key,
    )
    if len(exact) == 1:
        return LinkDecision("linked", "exact_canonical", exact[0], ())
    if exact:
        return LinkDecision("ambiguous", None, None, tuple(exact[:max_candidates]))
    return LinkDecision("not_found", None, None, ())


def resolve_mention(
    mention_text: str,
    candidates: Sequence[EntityCandidate],
    *,
    entity_type_key: str | None = None,
    min_score_micros: int,
    min_margin_micros: int,
    candidate_floor_micros: int = PRESENTATION_FLOOR_MICROS,
    max_candidates: int = MAX_CANDIDATES,
) -> LinkDecision:
    if not 0 <= min_score_micros <= MICROS:
        raise ValueError("min_score_micros out of range")
    if not 0 <= min_margin_micros <= MICROS:
        raise ValueError("min_margin_micros out of range")
    if candidate_floor_micros != PRESENTATION_FLOOR_MICROS:
        raise ValueError("candidate floor is frozen")
    if max_candidates != MAX_CANDIDATES:
        raise ValueError("max candidates is frozen")

    exact = resolve_exact_only(
        mention_text,
        candidates,
        entity_type_key=entity_type_key,
        max_candidates=max_candidates,
    )
    if exact.status != "not_found":
        return exact

    scored = sorted(
        (score_candidate(mention_text, row) for row in _filtered_candidates(candidates, entity_type_key)),
        key=stable_candidate_key,
    )
    return decide_scored_candidates(
        scored,
        min_score_micros=min_score_micros,
        min_margin_micros=min_margin_micros,
        candidate_floor_micros=candidate_floor_micros,
        max_candidates=max_candidates,
    )


def decide_scored_candidates(
    scored: Sequence[ScoredCandidate],
    *,
    min_score_micros: int,
    min_margin_micros: int,
    candidate_floor_micros: int = PRESENTATION_FLOOR_MICROS,
    max_candidates: int = MAX_CANDIDATES,
) -> LinkDecision:
    if not 0 <= min_score_micros <= MICROS or not 0 <= min_margin_micros <= MICROS:
        raise ValueError("score/margin thresholds out of range")
    if candidate_floor_micros != PRESENTATION_FLOOR_MICROS or max_candidates != MAX_CANDIDATES:
        raise ValueError("presentation controls are frozen")
    ordered = sorted(scored, key=stable_candidate_key)
    presented = tuple(row for row in ordered if row.features.score_micros >= candidate_floor_micros)[
        :max_candidates
    ]
    if not presented:
        return LinkDecision("not_found", None, None, ())

    top_score = presented[0].features.score_micros
    second_score = presented[1].features.score_micros if len(presented) > 1 else 0
    if top_score < min_score_micros or top_score - second_score < min_margin_micros:
        return LinkDecision("ambiguous", None, None, presented)
    return LinkDecision("linked", "lexical_v1", presented[0], ())


def logical_decision(decision: LinkDecision) -> dict[str, object]:
    def candidate_value(row: ScoredCandidate) -> dict[str, object]:
        return {
            "entity_key": row.candidate.entity_key,
            "entity_type_key": row.candidate.entity_type_key,
            "normalized_name": row.candidate.normalized_name,
            "score_micros": row.features.score_micros,
        }

    return {
        "status": decision.status,
        "method": decision.method,
        "selected": candidate_value(decision.selected) if decision.selected else None,
        "candidates": [candidate_value(row) for row in decision.candidates],
    }
