from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


_DASH_RUN = re.compile(r"[\u2010-\u2015\u2212]+")
_SPACE_AROUND_DASH = re.compile(r"\s*-\s*")
_CJK_IDENTIFIER_SPACE = re.compile(
    r"(?<=[\u3400-\u9fff])\s+(?=[a-z0-9])|(?<=[a-z0-9])\s+(?=[\u3400-\u9fff])",
    re.IGNORECASE,
)
_IDENTIFIER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{0,15}\s*[\-\u2010-\u2015\u2212]\s*[A-Za-z0-9]{1,16})(?![A-Za-z0-9])"
)


def _identifier_tokens(value: str) -> set[str]:
    return {
        normalize_graph_name_v1(match.group(1))
        for match in _IDENTIFIER_TOKEN.finditer(value)
        if normalize_graph_name_v1(match.group(1))
    }


def _display_normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = _DASH_RUN.sub("-", normalized)
    normalized = " ".join(normalized.split())
    normalized = _SPACE_AROUND_DASH.sub("-", normalized)
    return _CJK_IDENTIFIER_SPACE.sub("", normalized)


def evidence_backed_aliases_v1(
    *,
    name: str,
    explicit_aliases: Iterable[str] = (),
    properties: Any = None,
    evidence_quotes: Iterable[str] = (),
) -> list[str]:
    """Derive only formatting or exact identifier variants present in evidence."""

    named_sources = [name, *explicit_aliases]
    evidence_sources = [*evidence_quotes]
    if isinstance(properties, dict):
        named_sources.extend(value for value in properties.values() if isinstance(value, str))
    aliases: dict[str, str] = {}
    for value in named_sources:
        if not isinstance(value, str) or not value.strip():
            continue
        original = value.strip()
        formatted = _display_normalize(original)
        if formatted and formatted != original:
            aliases.setdefault(normalize_graph_name_v1(formatted), formatted)
        for match in _IDENTIFIER_TOKEN.finditer(original):
            identifier = _display_normalize(match.group(1))
            normalized_identifier = normalize_graph_name_v1(identifier)
            if normalized_identifier:
                aliases.setdefault(normalized_identifier, identifier)
    for value in evidence_sources:
        if not isinstance(value, str):
            continue
        for match in _IDENTIFIER_TOKEN.finditer(value):
            identifier = _display_normalize(match.group(1))
            normalized_identifier = normalize_graph_name_v1(identifier)
            if normalized_identifier:
                aliases.setdefault(normalized_identifier, identifier)
    return [aliases[key] for key in sorted(aliases)]


def filter_identifier_aliases_v1(
    entities: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep identifier aliases only when their evidence has an unambiguous owner."""

    rows = [dict(row) for row in entities]
    canonical_owners: dict[str, set[int]] = {}
    for index, row in enumerate(rows):
        values = [row.get("name"), row.get("canonical_name")]
        properties = row.get("properties")
        if isinstance(properties, dict):
            values.extend(value for value in properties.values() if isinstance(value, str))
        for value in values:
            if isinstance(value, str):
                for token in _identifier_tokens(value):
                    canonical_owners.setdefault(token, set()).add(index)

    for index, row in enumerate(rows):
        aliases = row.get("aliases", [])
        if not isinstance(aliases, list):
            continue
        kept: list[str] = []
        for alias in aliases:
            if not isinstance(alias, str) or not alias.strip():
                continue
            tokens = _identifier_tokens(alias)
            if not tokens:
                kept.append(alias)
                continue
            normalized_alias = normalize_graph_name_v1(alias)
            owner_indexes = canonical_owners.get(normalized_alias, set())
            if index in owner_indexes or owner_indexes == {index}:
                kept.append(alias)
                continue
            claimed_by = {
                other_index
                for other_index, other in enumerate(rows)
                if normalized_alias
                in {
                    normalize_graph_name_v1(value)
                    for value in other.get("aliases", [])
                    if isinstance(value, str)
                }
            }
            if not claimed_by or claimed_by == {index}:
                kept.append(alias)
        row["aliases"] = list(dict.fromkeys(kept))
    return rows


def normalize_graph_name_v1(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("graph name must be a string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = _DASH_RUN.sub("-", normalized)
    normalized = " ".join(normalized.strip().split())
    normalized = _SPACE_AROUND_DASH.sub("-", normalized)
    return _CJK_IDENTIFIER_SPACE.sub("", normalized)


def canonicalize_extracted_entity_name_v1(entity_type_key: str, value: str) -> str:
    if not isinstance(entity_type_key, str):
        raise TypeError("entity type key must be a string")
    if not isinstance(value, str):
        raise TypeError("graph name must be a string")
    # Business names are normalized by the frozen Schema and entity-linking
    # aliases, not by a production rule for one test type or suffix.
    return value.strip()
