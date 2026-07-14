from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _require_json_value(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} JSON object keys must be strings")
            _require_json_value(item, path=f"{path}.{key}")
        return
    raise TypeError(f"{path} contains unsupported JSON value {type(value).__name__}")


def canonical_graph_json_v1(value: Any) -> str:
    _require_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_graph_value_hash_v1(value: Any) -> str:
    return hashlib.sha256(canonical_graph_json_v1(value).encode("utf-8")).hexdigest()
