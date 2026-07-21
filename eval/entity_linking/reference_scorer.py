from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Sequence

from app.services.graph_normalization import normalize_graph_name_v1


ALGORITHM_VERSION = "lexical-score-v2"
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
class PreparedLexicalValue:
    value: str
    character_grams: Counter[str]
    character_gram_count: int
    tokens: frozenset[str]
    compact: str
    token_initials: str


@dataclass(frozen=True, slots=True)
class PreparedEntityCandidate:
    candidate: EntityCandidate
    lexical: PreparedLexicalValue


@dataclass(frozen=True, slots=True)
class FeatureScores:
    character_bigram_dice_micros: int
    token_jaccard_micros: int
    substring_containment_micros: int
    boundary_omission_micros: int
    ordered_abbreviation_micros: int
    score_micros: int


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: EntityCandidate
    features: FeatureScores


@dataclass(frozen=True, slots=True)
class LinkDecision:
    status: Literal["linked", "ambiguous", "not_found"]
    method: Literal["exact_canonical", "lexical_v2"] | None
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


def prepare_normalized_value(value: str) -> PreparedLexicalValue:
    if not value:
        raise ValueError("normalized scorer inputs must be non-empty")
    grams = _character_grams(value)
    tokens = value.split()
    return PreparedLexicalValue(
        value=value,
        character_grams=grams,
        character_gram_count=sum(grams.values()),
        tokens=frozenset(tokens),
        compact="".join(tokens),
        token_initials="".join(token[0] for token in tokens),
    )


@lru_cache(maxsize=10_000)
def _prepare_candidate_normalized_value(value: str) -> PreparedLexicalValue:
    return prepare_normalized_value(value)


def prepare_candidates(
    candidates: Sequence[EntityCandidate],
) -> tuple[PreparedEntityCandidate, ...]:
    return tuple(
        PreparedEntityCandidate(
            candidate=row,
            lexical=_prepare_candidate_normalized_value(row.normalized_name),
        )
        for row in candidates
    )


def _character_bigram_dice_prepared(
    left: PreparedLexicalValue,
    right: PreparedLexicalValue,
) -> int:
    intersection = sum(
        min(count, right.character_grams.get(gram, 0))
        for gram, count in left.character_grams.items()
    )
    return ratio_micros(
        2 * intersection,
        left.character_gram_count + right.character_gram_count,
    )


def character_bigram_dice_micros(left: str, right: str) -> int:
    return _character_bigram_dice_prepared(
        prepare_normalized_value(left),
        prepare_normalized_value(right),
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


def scale_support_micros(support_micros: int) -> int:
    if not 0 <= support_micros <= MICROS:
        raise ValueError("support_micros out of range")
    return 850_000 + (2 * 150_000 * support_micros + MICROS) // (2 * MICROS)


def is_strict_subsequence(left: str, right: str) -> bool:
    if not left or not right:
        return False
    position = 0
    for value in right:
        if value == left[position]:
            position += 1
            if position == len(left):
                return True
    return False


def boundary_omission_micros(left: str, right: str) -> int:
    if len(left) < 2 or len(left) >= len(right):
        return 0
    if not (right.startswith(left) or right.endswith(left)):
        return 0
    return scale_support_micros(ratio_micros(len(left), len(right)))


def ordered_abbreviation_micros(left: str, right: str, *, bigram_micros: int) -> int:
    compact_left = "".join(left.split())
    compact_right = "".join(right.split())
    right_tokens = right.split()
    initialism = bool(
        len(right_tokens) >= 2
        and compact_left == "".join(token[0] for token in right_tokens)
    )
    subsequence = bool(
        2 <= len(compact_left) < len(compact_right)
        and is_strict_subsequence(compact_left, compact_right)
        and compact_left not in compact_right
        and 4 * len(compact_left) <= 3 * len(compact_right)
    )
    if not (initialism or subsequence):
        return 0
    support = max(
        bigram_micros,
        ratio_micros(len(compact_left), len(compact_right)),
    )
    return scale_support_micros(support)


def score_prepared_pair(
    left: PreparedLexicalValue,
    right: PreparedLexicalValue,
) -> FeatureScores:
    bigram = _character_bigram_dice_prepared(left, right)
    union = left.tokens | right.tokens
    jaccard = ratio_micros(len(left.tokens & right.tokens), len(union))
    containment = substring_containment_micros(left.value, right.value)
    boundary = boundary_omission_micros(left.value, right.value)
    initialism = bool(len(right.tokens) >= 2 and left.compact == right.token_initials)
    subsequence = bool(
        2 <= len(left.compact) < len(right.compact)
        and is_strict_subsequence(left.compact, right.compact)
        and left.compact not in right.compact
        and 4 * len(left.compact) <= 3 * len(right.compact)
    )
    abbreviation = 0
    if initialism or subsequence:
        support = max(
            bigram,
            ratio_micros(len(left.compact), len(right.compact)),
        )
        abbreviation = scale_support_micros(support)
    return FeatureScores(
        character_bigram_dice_micros=bigram,
        token_jaccard_micros=jaccard,
        substring_containment_micros=containment,
        boundary_omission_micros=boundary,
        ordered_abbreviation_micros=abbreviation,
        score_micros=max(bigram, jaccard, containment, boundary, abbreviation),
    )


def score_normalized_pair(left: str, right: str) -> FeatureScores:
    return score_prepared_pair(
        prepare_normalized_value(left),
        prepare_normalized_value(right),
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
    exact = FeatureScores(MICROS, MICROS, MICROS, 0, 0, MICROS)
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
    return resolve_prepared_mention(
        mention_text,
        prepare_candidates(candidates),
        entity_type_key=entity_type_key,
        min_score_micros=min_score_micros,
        min_margin_micros=min_margin_micros,
        candidate_floor_micros=candidate_floor_micros,
        max_candidates=max_candidates,
    )


def resolve_prepared_mention(
    mention_text: str,
    candidates: Sequence[PreparedEntityCandidate],
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

    normalized = normalize_graph_name_v1(mention_text)
    if not normalized:
        raise ValueError("mention normalizes to empty")
    filtered = tuple(
        row
        for row in candidates
        if entity_type_key is None or row.candidate.entity_type_key == entity_type_key
    )
    exact = sorted(
        (_exact_scored(row.candidate) for row in filtered if row.candidate.normalized_name == normalized),
        key=stable_candidate_key,
    )
    if len(exact) == 1:
        return LinkDecision("linked", "exact_canonical", exact[0], ())
    if exact:
        return LinkDecision("ambiguous", None, None, tuple(exact[:max_candidates]))

    mention = prepare_normalized_value(normalized)
    scored = sorted(
        (
            ScoredCandidate(row.candidate, score_prepared_pair(mention, row.lexical))
            for row in filtered
        ),
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
    return LinkDecision("linked", "lexical_v2", presented[0], ())


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
