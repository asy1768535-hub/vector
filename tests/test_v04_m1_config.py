from __future__ import annotations

import inspect

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.main import assert_graph_extraction_startup_security


EXPECTED_DEFAULTS = {
    "graph_extraction_enabled": False,
    "graph_extraction_auto_trigger_enabled": False,
    "graph_extraction_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "graph_extraction_model": "qwen-plus",
    "graph_extraction_timeout_seconds": 120.0,
    "graph_extraction_temperature": 0.0,
    "graph_extraction_response_format": "json_object",
    "graph_extraction_max_context_chars": 24000,
    "graph_extraction_previous_chunks": 1,
    "graph_extraction_next_chunks": 1,
    "graph_extraction_prompt_version": "v1",
    "graph_extraction_extractor_version": "v1",
    "graph_extraction_output_parser_version": "v1",
    "graph_extraction_context_policy_version": "v1",
    "graph_extraction_policy_version": "v1",
    "graph_extraction_normalization_rule_version": "normalization_v1",
    "graph_extraction_confidence_policy_version": "v1",
    "graph_extraction_entity_materialization_threshold": 0.85,
    "graph_extraction_relation_draft_threshold": 0.85,
    "graph_extraction_weight_model": 0.25,
    "graph_extraction_weight_evidence": 0.35,
    "graph_extraction_weight_schema": 0.25,
    "graph_extraction_weight_normalization": 0.15,
    "graph_extraction_auto_evidence_types": "direct_statement,table_cell",
    "graph_extraction_evidence_group_policy": "all_claims_valid",
    "graph_extraction_worker_poll_seconds": 3.0,
    "graph_extraction_unit_lease_seconds": 180,
    "graph_extraction_unit_lease_renew_seconds": 30,
    "graph_extraction_worker_max_model_attempts": 3,
    "graph_extraction_context_retention_days": 30,
    "graph_extraction_raw_output_retention_days": 30,
    "graph_extraction_candidate_retention_days": 180,
}


def _set_valid_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import settings

    for name, value in EXPECTED_DEFAULTS.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr(""))


def test_graph_extraction_defaults_match_master_plan():
    fields = Settings.model_fields
    for name, expected in EXPECTED_DEFAULTS.items():
        assert fields[name].default == expected
    assert isinstance(fields["graph_extraction_api_key"].default, SecretStr)
    assert fields["graph_extraction_api_key"].default.get_secret_value() == ""


def test_graph_extraction_api_key_is_masked_and_independent_from_chat():
    cfg = Settings(
        _env_file=None,
        graph_extraction_api_key="graph-secret-value",
        chat_api_key="different-chat-secret",
    )
    assert "graph-secret-value" not in repr(cfg.graph_extraction_api_key)
    source = inspect.getsource(assert_graph_extraction_startup_security).lower()
    assert "chat_api_key" not in source
    assert "chat_" not in source


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("graph_extraction_weight_model", -0.01),
        ("graph_extraction_weight_evidence", 1.01),
    ],
)
def test_startup_rejects_weight_outside_unit_interval(monkeypatch, name, value):
    _set_valid_runtime(monkeypatch)
    from app.main import settings

    monkeypatch.setattr(settings, name, value)
    with pytest.raises(RuntimeError, match="confidence weight"):
        assert_graph_extraction_startup_security()


def test_startup_rejects_weight_sum_not_one(monkeypatch):
    _set_valid_runtime(monkeypatch)
    from app.main import settings

    monkeypatch.setattr(settings, "graph_extraction_weight_model", 0.2)
    with pytest.raises(RuntimeError, match="sum to 1.0"):
        assert_graph_extraction_startup_security()


def test_startup_rejects_renew_at_or_above_half_lease(monkeypatch):
    _set_valid_runtime(monkeypatch)
    from app.main import settings

    monkeypatch.setattr(settings, "graph_extraction_unit_lease_renew_seconds", 90)
    with pytest.raises(RuntimeError, match="less than half"):
        assert_graph_extraction_startup_security()


@pytest.mark.parametrize(
    "name",
    [
        "graph_extraction_context_retention_days",
        "graph_extraction_raw_output_retention_days",
        "graph_extraction_candidate_retention_days",
    ],
)
def test_startup_rejects_non_positive_retention(monkeypatch, name):
    _set_valid_runtime(monkeypatch)
    from app.main import settings

    monkeypatch.setattr(settings, name, 0)
    with pytest.raises(RuntimeError, match="retention"):
        assert_graph_extraction_startup_security()


def test_startup_rejects_timeout_at_or_above_lease(monkeypatch):
    _set_valid_runtime(monkeypatch)
    from app.main import settings

    monkeypatch.setattr(settings, "graph_extraction_timeout_seconds", 180.0)
    with pytest.raises(RuntimeError, match="timeout"):
        assert_graph_extraction_startup_security()


def test_startup_requires_graph_key_only_when_auto_trigger_is_enabled(monkeypatch):
    _set_valid_runtime(monkeypatch)
    from app.main import settings

    monkeypatch.setattr(settings, "graph_extraction_auto_trigger_enabled", True)
    with pytest.raises(RuntimeError, match="GRAPH_EXTRACTION_API_KEY") as exc:
        assert_graph_extraction_startup_security()
    assert "graph-secret-value" not in str(exc.value)

    monkeypatch.setattr(settings, "graph_extraction_api_key", SecretStr("graph-secret-value"))
    assert_graph_extraction_startup_security()


def test_valid_graph_extraction_startup_contract_passes(monkeypatch):
    _set_valid_runtime(monkeypatch)
    assert_graph_extraction_startup_security()
