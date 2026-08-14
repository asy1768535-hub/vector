"""Strict datetime ingress shared by the M0-M3 typed contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any


_RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)


def strict_datetime(value: Any, *, field: str) -> datetime:
    """Validate a timezone-aware Python datetime or canonical RFC3339 string."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and _RFC3339_DATETIME.fullmatch(value):
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field} must be a valid canonical RFC3339 datetime") from exc
    else:
        raise ValueError(f"{field} must be a timezone-aware datetime or canonical RFC3339 string")

    try:
        offset = parsed.utcoffset()
        normalized = parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite and timezone-normalizable") from exc
    if offset is None or normalized.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone")
    return parsed


def canonical_datetime_string(value: Any, *, field: str) -> str:
    """Return one UTC-Z representation without accepting loose datetime input."""
    return strict_datetime(value, field=field).astimezone(UTC).isoformat().replace("+00:00", "Z")
