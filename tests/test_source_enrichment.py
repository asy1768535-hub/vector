from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services import source_enrichment as S


def _cfg(**overrides):
    base = {
        "db_name": "source_db",
        "table": "case_full_texts",
        "key_field": "case_id",
        "key_column": "case_id",
        "text_column": "full_text",
        "key_type": "bigint",
        "extra_columns": ["court", "year"],
    }
    base.update(overrides)
    return base


def test_parse_source_config_rejects_sql_identifier_injection():
    with pytest.raises(S.SourceConfigError):
        S.parse_source_config(_cfg(table="case_full_texts;DROP_TABLE"))
    with pytest.raises(S.SourceConfigError):
        S.parse_source_config(_cfg(text_column="full_text) FROM secrets --"))
    with pytest.raises(S.SourceConfigError):
        S.parse_source_config(_cfg(extra_columns=["court", "bad-column"]))


def test_parse_source_config_rejects_non_postgres_dsn_and_bad_key_type():
    with pytest.raises(S.SourceConfigError):
        S.parse_source_config(_cfg(dsn="http://metadata.internal/db"))
    with pytest.raises(S.SourceConfigError):
        S.parse_source_config(_cfg(key_type="jsonb"))


def test_coerce_key_rejects_bad_or_out_of_range_integer_values():
    assert S._coerce_key("42", "bigint") == 42
    assert S._coerce_key("not-int", "bigint") is None
    assert S._coerce_key(str(2**63), "bigint") is None
    assert S._coerce_key(str(2**31), "int4") is None
    assert S._coerce_key("abc", "text") == "abc"


def test_enrich_payloads_preserves_order_and_merges_rows_by_coerced_key():
    async def run():
        rows = {
            1: {"case_id": 1, "full_text": "one", "court": "A", "year": 2024},
            2: {"case_id": 2, "full_text": "two", "court": "B", "year": 2025},
        }
        with patch.object(S, "fetch_source_rows", new=AsyncMock(return_value=rows)) as fetch:
            result = await S.enrich_payloads(
                _cfg(),
                [{"case_id": "2"}, {"case_id": "missing"}, {"case_id": "1"}],
            )
        assert result.enabled is True
        assert result.texts == ["two", None, "one"]
        assert result.rows[0]["court"] == "B"
        assert result.rows[2]["year"] == 2024
        fetch.assert_awaited_once()
        assert fetch.await_args.args[1] == [1, 2]

    asyncio.run(run())


def test_enrich_payloads_degrades_runtime_errors_to_payload_text_fallback():
    async def run():
        with patch.object(
            S,
            "fetch_source_rows",
            new=AsyncMock(side_effect=S.SourceEnrichmentRuntimeError("source down")),
        ):
            result = await S.enrich_payloads(_cfg(), [{"case_id": "1"}])
        assert result.enabled is True
        assert result.texts == [None]
        assert result.rows == [None]

    asyncio.run(run())


def test_enrich_payloads_can_raise_runtime_errors_in_strict_mode():
    async def run():
        with patch.object(
            S,
            "fetch_source_rows",
            new=AsyncMock(side_effect=S.SourceEnrichmentRuntimeError("source down")),
        ):
            with pytest.raises(S.SourceEnrichmentRuntimeError):
                await S.enrich_payloads(_cfg(), [{"case_id": "1"}], degrade_on_runtime_error=False)

    asyncio.run(run())
