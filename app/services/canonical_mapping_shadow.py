"""Independent library/global authorization for the Phase 3 shadow adapter.

This module is deliberately separate from the Phase 2 raw-shadow rollout.
It only builds the typed M5 authorization and delegates to the already
validated, no-publication canary runner.  It has no database, provider,
candidate, materializer, entity, relation, or publication dependency.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal
from uuid import UUID

from app.schemas.canonical_mapping import CanonicalMappingInputV1, CanonicalMappingV1
from app.services.canonical_mapping_rollout import (
    CanonicalMappingCanaryCaseV1,
    CanonicalMappingCanaryLimitsV1,
    CanonicalMappingCanaryMapperOutputV1,
    CanonicalMappingCanaryReportV1,
    CanonicalMappingRolloutAuthorizationV1,
    run_canonical_mapping_canary_suite,
)


CanonicalMappingShadowPolicy = Literal["inherit", "disabled", "enabled"]


def resolve_canonical_mapping_shadow(
    *,
    global_enabled: bool,
    policy: CanonicalMappingShadowPolicy | str,
    schema_mode: str,
    graph_extraction_enabled: bool,
    external_llm_enabled: bool,
) -> bool:
    """Resolve the independent gate without coercing any caller value."""

    if type(global_enabled) is not bool:
        raise TypeError("canonical mapping global gate must be a boolean")
    if policy not in {"inherit", "disabled", "enabled"}:
        raise ValueError("canonical mapping shadow policy is invalid")
    if type(graph_extraction_enabled) is not bool or type(external_llm_enabled) is not bool:
        raise TypeError("canonical mapping library gates must be booleans")
    # ``inherit`` inherits the fail-closed default, not an unrelated raw-shadow
    # or schema-confirmation decision. Explicit library enablement is required.
    return bool(
        global_enabled
        and policy == "enabled"
        and schema_mode != "disabled"
        and graph_extraction_enabled
        and external_llm_enabled
    )


def build_canonical_mapping_shadow_authorization(
    *,
    library_id: UUID,
    global_enabled: bool,
    policy: CanonicalMappingShadowPolicy | str,
    schema_mode: str,
    graph_extraction_enabled: bool,
    external_llm_enabled: bool,
) -> CanonicalMappingRolloutAuthorizationV1:
    """Build an M5 authorization from the two independent production gates."""

    if not isinstance(library_id, UUID):
        raise TypeError("canonical mapping library id must be a UUID")
    resolved = resolve_canonical_mapping_shadow(
        global_enabled=global_enabled,
        policy=policy,
        schema_mode=schema_mode,
        graph_extraction_enabled=graph_extraction_enabled,
        external_llm_enabled=external_llm_enabled,
    )
    return CanonicalMappingRolloutAuthorizationV1(
        global_execution_enabled=global_enabled,
        authorized_library_ids=(library_id,) if resolved else (),
        read_visibility_enabled=resolved,
        bridge_visibility_enabled=False,
    )


MapperCallable = Callable[
    [object], Awaitable[CanonicalMappingCanaryMapperOutputV1]
]
PersistenceCallable = Callable[[CanonicalMappingInputV1, CanonicalMappingV1], Awaitable[None]]


async def run_canonical_mapping_shadow(
    *,
    library_id: UUID,
    global_enabled: bool,
    policy: CanonicalMappingShadowPolicy | str,
    schema_mode: str,
    graph_extraction_enabled: bool,
    external_llm_enabled: bool,
    cases: Sequence[CanonicalMappingCanaryCaseV1],
    mapper: MapperCallable,
    persistence: PersistenceCallable | None = None,
    limits: CanonicalMappingCanaryLimitsV1 = CanonicalMappingCanaryLimitsV1(),
) -> CanonicalMappingCanaryReportV1:
    """Run one explicitly authorized, read-only shadow mapping suite.

    The adapter accepts already-authoritative M0 input cases.  A real caller
    must construct those cases only after raw claim, evidence, occurrence,
    decision, and frozen ontology validation.  Persistence is an explicit
    callback for append-only mapping history; this module never writes a
    candidate, entity, relation, materialized row, or publication row.
    """

    authorization = build_canonical_mapping_shadow_authorization(
        library_id=library_id,
        global_enabled=global_enabled,
        policy=policy,
        schema_mode=schema_mode,
        graph_extraction_enabled=graph_extraction_enabled,
        external_llm_enabled=external_llm_enabled,
    )
    return await run_canonical_mapping_canary_suite(
        authorization=authorization,
        library_id=library_id,
        cases=cases,
        mapper=mapper,
        persistence=persistence,
        limits=limits,
    )


__all__ = [
    "CanonicalMappingShadowPolicy",
    "build_canonical_mapping_shadow_authorization",
    "resolve_canonical_mapping_shadow",
    "run_canonical_mapping_shadow",
]
