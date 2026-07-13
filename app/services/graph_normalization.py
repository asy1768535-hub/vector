from __future__ import annotations


def normalize_graph_name_v1(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("graph name must be a string")
    return " ".join(value.strip().casefold().split())
