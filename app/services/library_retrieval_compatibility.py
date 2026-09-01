from __future__ import annotations

import hashlib
from typing import Any

from app.config import settings
from app.models.library import Library
from app.services import llm_query_rewrite, query_rewrite
from app.services import rerank as rerank_service
from app.services.library_compatibility_contracts import (
    FEDERATED_FUSION_CONTRACT_VERSION,
    FEDERATED_RETRIEVAL_CONTRACT_VERSION,
    FEDERATED_RRF_K,
    RETRIEVAL_PROFILE_VERSION,
    CompatibilityFingerprint,
)
from app.services.graph_canonical import canonical_graph_value_hash_v1


def _optional_endpoint_hash(value: str) -> str | None:
    normalized = value.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None


def build_retrieval_profile(library: Library) -> CompatibilityFingerprint:
    effective_rerank = (
        library.rerank_enabled
        if library.rerank_enabled is not None
        else settings.rerank_enabled
    ) and rerank_service.is_configured()
    synonyms_sha256 = None
    if settings.query_rewrite_enabled:
        synonyms_sha256 = canonical_graph_value_hash_v1(
            query_rewrite.load_synonyms(
                settings.query_rewrite_synonyms_path,
                use_cache=False,
            )
        )
    llm_rewrite_enabled = (
        settings.query_rewrite_llm_enabled and llm_query_rewrite.is_configured()
    )
    payload: dict[str, Any] = {
        "contract_version": RETRIEVAL_PROFILE_VERSION,
        "chunking": {
            "size": library.chunk_size,
            "overlap": library.chunk_overlap,
            "docx_table_aware": (
                library.docx_table_aware
                if library.docx_table_aware is not None
                else settings.docx_table_aware
            ),
        },
        "retrieval_mode": library.retrieval_mode,
        "query_rewrite": {
            "rule_enabled": settings.query_rewrite_enabled,
            "max_queries": settings.query_rewrite_max_queries,
            "synonyms_sha256": synonyms_sha256,
            "llm_enabled": llm_rewrite_enabled,
            "llm_model": settings.query_rewrite_llm_model if llm_rewrite_enabled else None,
            "llm_endpoint_sha256": (
                _optional_endpoint_hash(settings.query_rewrite_llm_base_url)
                if llm_rewrite_enabled
                else None
            ),
            "llm_max_queries": (
                settings.query_rewrite_llm_max_queries if llm_rewrite_enabled else None
            ),
            "llm_timeout_seconds": (
                settings.query_rewrite_llm_timeout_seconds
                if llm_rewrite_enabled
                else None
            ),
        },
        "hybrid": {
            "candidate_k": settings.hybrid_candidate_k,
            "rrf_k": settings.hybrid_rrf_k,
            "keyword_threshold": settings.hybrid_keyword_threshold,
            "title_boost": settings.hybrid_keyword_title_boost,
            "external_id_boost": settings.hybrid_keyword_external_id_boost,
        }
        if library.retrieval_mode == "hybrid"
        else None,
        "rerank": {
            "enabled": bool(effective_rerank),
            "provider": settings.rerank_provider if effective_rerank else None,
            "model": settings.rerank_model if effective_rerank else None,
            "endpoint_sha256": (
                _optional_endpoint_hash(settings.rerank_base_url)
                if effective_rerank
                else None
            ),
            "candidate_k": settings.rerank_candidate_k if effective_rerank else None,
        },
        "score_semantics": "per_library_rank_rrf_v1",
        "visibility": {
            "contract": "library_visibility_v1",
            "overfetch_factor": settings.visibility_overfetch_factor,
            "overfetch_max": settings.visibility_overfetch_max,
            "refetch_max_rounds": settings.visibility_refetch_max_rounds,
            "total_candidate_cap": settings.visibility_total_candidate_cap,
            "latency_budget_ms": settings.visibility_latency_budget_ms,
        },
        "source_enrichment_contract": "post_visibility_enrichment_v1",
        "federation": {
            "contract_version": FEDERATED_RETRIEVAL_CONTRACT_VERSION,
            "fusion_contract_version": FEDERATED_FUSION_CONTRACT_VERSION,
            "rrf_k": FEDERATED_RRF_K,
            "score_input": "local_rank_only_v1",
            "tie_break": "selection_order_local_rank_hit_id_v1",
        },
    }
    return CompatibilityFingerprint(
        RETRIEVAL_PROFILE_VERSION,
        canonical_graph_value_hash_v1(payload),
        True,
    )
