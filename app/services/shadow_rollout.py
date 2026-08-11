"""Production rollout adapter for the DB-free shadow extraction decision."""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace

from app.config import settings
from app.schemas.shadow_raw_response import (
    ShadowExtractionConfigV1,
    ShadowLibraryPolicyV1,
    resolve_shadow_extraction,
)


def _library_policy(library: Any) -> ShadowLibraryPolicyV1:
    value = getattr(library, "claim_graph_shadow_policy", None) or "inherit"
    return value if value in {"inherit", "enabled", "disabled"} else "disabled"


def shadow_extraction_config_for_library(
    library: Any,
    *,
    settings_obj: Any = settings,
) -> ShadowExtractionConfigV1:
    policy = _library_policy(library)
    if not bool(getattr(library, "graph_extraction_enabled", False)):
        policy = "disabled"
    if not bool(getattr(library, "external_llm_enabled", False)):
        policy = "disabled"
    return ShadowExtractionConfigV1(
        global_enabled=bool(getattr(settings_obj, "graph_claim_shadow_enabled", False)),
        library_policy=policy,
    )


def resolve_library_shadow_extraction(
    library: Any,
    *,
    settings_obj: Any = settings,
) -> bool:
    return resolve_shadow_extraction(
        shadow_extraction_config_for_library(library, settings_obj=settings_obj)
    )


def resolve_shadow_enabled(
    *,
    policy: str,
    graph_extraction_enabled: bool,
    external_llm_enabled: bool,
    settings_obj: Any = settings,
) -> bool:
    """Resolve read projection values without requiring a Library ORM instance."""

    return resolve_library_shadow_extraction(
        SimpleNamespace(
            claim_graph_shadow_policy=policy,
            graph_extraction_enabled=graph_extraction_enabled,
            external_llm_enabled=external_llm_enabled,
        ),
        settings_obj=settings_obj,
    )
