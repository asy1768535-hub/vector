"""集中配置：pydantic-settings 从 .env 加载，启动时类型校验。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import uuid
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTITY_LINKING_POLICY_FIELDS = {
    "schema_version",
    "policy_version",
    "algorithm_version",
    "normalization_version",
    "g2_approval_commit",
    "g2_specification_tree_sha256",
    "calibration_ref",
    "approved_thresholds",
    "approval_payload_sha256",
    "dataset_manifest_ref",
    "dataset_content_sha256",
    "evaluation_config_sha256",
    "ontology_schema_set_hash",
    "code_commit",
    "evaluation_tree_sha256",
    "accepted_dependency_closure_sha256",
    "reference_scorer_sha256",
    "external_distribution_set_sha256",
    "control_config_sha256",
    "environment_fingerprint_sha256",
    "pg_cluster_fingerprint_sha256",
    "qdrant_fingerprint_sha256",
    "embedding_fingerprint_sha256",
    "approved_by",
    "approved_at",
    "approval_reference",
}
_ENTITY_LINKING_THRESHOLD_FIELDS = {
    "min_score_micros",
    "min_margin_micros",
    "candidate_floor_micros",
    "max_candidates",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Database ----
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "postgres"
    db_password: str = ""
    db_name: str = "vector_kb"
    db_pool_size: int = Field(default=2, ge=1, le=20)
    db_max_overflow: int = Field(default=8, ge=0, le=40)
    db_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    db_pool_recycle_seconds: int = Field(default=900, ge=60, le=86_400)

    @property
    def db_dsn_async(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def db_dsn_sync(self) -> str:
        # Alembic / Casbin SQLAlchemy adapter 用同步驱动
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ---- Embedding ----
    embedding_base_url: str = "http://10.0.10.2:8111/v1/embeddings"
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024
    # 远程 embedding 服务的 API Key（如阿里云 DashScope）；留空 = 不发鉴权头（本地 bge-m3）
    embedding_api_key: str = ""

    # ---- Rerank（默认关闭；配好本地/远程 reranker 地址再开）----
    # rerank_provider: "standard" = 标准 /rerank 格式（Infinity/TEI/Jina/Cohere 兼容）；
    #                  "dashscope" = 阿里云 DashScope 原生 text-rerank（input/parameters 结构）
    rerank_provider: str = "standard"
    rerank_enabled: bool = False
    rerank_base_url: str = ""
    rerank_model: str = ""
    rerank_api_key: str = ""  # 留空则复用 embedding_api_key（同一家服务商同 key 时省事）
    rerank_candidate_k: int = 50  # 重排前从 Qdrant 召回多少候选
    rerank_batch_size: int = 32  # provider 单次请求上限；TEI 默认最大 batch=32
    rerank_min_score: float = 0.05  # rerank 成功时的最低证据分；低于此值拒答

    # ---- Hybrid 检索（库级 retrieval_mode=hybrid 时生效；轻量第一版，无外部服务）----
    hybrid_candidate_k: int = 50  # dense / keyword 各召回多少候选参与 RRF
    hybrid_rrf_k: int = 60  # RRF 平滑常数：score = Σ 1/(k+rank)，60 为业界常用默认
    hybrid_keyword_threshold: float = 0.3  # pg_trgm word_similarity 命中门槛（0~1）
    hybrid_keyword_title_boost: float = 1.5  # 文件名/标题命中加权
    hybrid_keyword_external_id_boost: float = 2.0  # 文号/external_id 命中加权（编号类问题更稳）
    hybrid_min_dense_score: float = 0.55  # hybrid 无 rerank 时的原始 dense 证据门槛

    # ---- Query Rewrite（多 query dense 召回；第一版，不接 LLM/Agent/Hybrid）----
    # 关：单 query dense 召回（旧逻辑完全不变）。
    # 开：normalize + 同义词/简称扩展出多个 query，分别 embedding+召回，按 chunk_id 合并去重，
    #     再走现有 visibility / source_enrichment / rerank / threshold 流程。
    query_rewrite_enabled: bool = False
    query_rewrite_max_queries: int = 4  # 扩展后最多用几个 query（含原始；规则+LLM 合并后的硬上限）
    query_rewrite_synonyms_path: str = "config/query_synonyms.json"  # 同义词/简称词典

    # ---- LLM Query Rewrite（可选；只生成检索 query，绝不回答/接 Agent/工具）----
    # 开启后：调 OpenAI 兼容 chat 接口把问题改写成多个检索 query，与规则 rewrite 合并。
    # 失败/超时/非法 JSON 一律退回规则 rewrite，绝不阻断检索。
    query_rewrite_llm_enabled: bool = False
    query_rewrite_llm_base_url: str = ""  # 如 http://host:8000/v1 或 .../v1/chat/completions
    query_rewrite_llm_model: str = ""
    query_rewrite_llm_api_key: str = ""  # 本地服务可留空
    query_rewrite_llm_timeout_seconds: float = 8.0
    query_rewrite_llm_max_queries: int = 4  # LLM 单次最多产出几个 query

    # ---- Chat 用户端（轻量问答 v1；默认关；OpenAI 兼容 chat 接口；只生成答案，不接 Agent/工具）----
    # 关闭时 /chat/messages 返回 503。base_url 支持 .../v1 或完整 .../chat/completions；本地模型可不填 key。
    chat_enabled: bool = False
    chat_base_url: str = ""
    chat_model: str = ""
    chat_api_key: str = ""  # 本地模型可留空（不发 Authorization 头）
    chat_timeout_seconds: float = 30.0
    chat_max_context_chars: int = 12000  # 拼进 prompt 的资料上限，超出截断
    chat_temperature: float = 0.2
    # 多轮上下文：带入最近 N 轮历史帮助理解追问（仅理解，不作事实依据）。0=不带历史。
    chat_history_max_turns: int = 5
    chat_graph_answer_enabled: bool = False

    # ---- OCR（图片/扫描件抽文字；默认关，按库 ocr_enabled 覆盖；需装 rapidocr_onnxruntime）----
    ocr_enabled: bool = False
    ocr_intra_op_num_threads: int = Field(default=8, ge=1, le=64)
    ocr_inter_op_num_threads: int = Field(default=1, ge=1, le=8)
    image_ocr_max_input_bytes: int = Field(
        default=128 * 1024 * 1024,
        ge=1024 * 1024,
        le=500 * 1024 * 1024,
    )

    # ---- PDF 扫描页 OCR 安全参数（仅当库 ocr_enabled 开启时生效）----
    # 单页非空白字符数 < 该阈值 → 视为图片页，走渲染 + OCR（否则用文字层）
    pdf_ocr_min_text_chars: int = 20
    # 渲染扫描页的 DPI（建议 150~300）；越高越清晰但越慢越占内存
    pdf_ocr_render_dpi: int = 200
    # 单份 PDF 最多 OCR 多少页（只统计真正进 OCR 的页）；超过即 400 快速失败，不继续渲染
    pdf_ocr_max_pages: int = 50

    # ---- docx 表格感知切块（默认关，按库 docx_table_aware 覆盖）----
    # 关：docx 走扁平正文切分（散文为主的库实测更优）；开：每个表格单独成块带表头/章节上下文，
    # 表格召回更稳但 chunk 数/成本上升。表格很重的库（或 rerank 不可用时）建议在库上开。
    docx_table_aware: bool = False

    # ---- Qdrant ----
    qdrant_url: str = "http://10.0.10.2:6333"
    qdrant_api_key: str = ""

    # ---- API ----
    api_host: str = "0.0.0.0"
    api_port: int = 8100
    app_debug: bool = False
    console_ui_dir: str = "admin-ui"
    deployment_profile: str = "development"
    readiness_dependency_timeout_seconds: float = 3.0

    # ---- Auth ----
    jwt_secret: str = Field(default="please-change-me-in-env", min_length=16)
    jwt_lifetime_seconds: int = 60 * 60 * 12  # 12h
    cookie_secure: bool = False  # 生产置 true
    cookie_name: str = "vk_session"
    # 公开注册 /auth/register：内部部门平台默认关闭，只许超管经 /admin/users 建用户。
    allow_public_registration: bool = False
    organization_authorization_enabled: bool = False
    cross_library_compatibility_enabled: bool = False
    federated_retrieval_enabled: bool = False
    personal_library_scopes_enabled: bool = False
    classification_taxonomy_enabled: bool = False
    classification_decision_enabled: bool = False

    # ---- 文件导入 ----
    # 旧同步 /import-file 单次上传字节上限（默认 50MiB），超出 413。
    max_import_file_bytes: int = 50 * 1024 * 1024
    # 分块 staging/import-session 总量上限独立于旧同步接口，保留大文件导入能力。
    import_staging_max_file_bytes: int = 500 * 1024 * 1024
    import_upload_chunk_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024 * 1024,
        le=64 * 1024 * 1024,
    )
    import_selection_max_files: int = 1000
    import_upload_file_concurrency: int = Field(default=1, ge=1, le=16)
    import_upload_user_inflight_limit: int = Field(default=1, ge=1, le=16)
    import_upload_global_inflight_limit: int = Field(default=10, ge=1, le=128)
    import_upload_claim_heartbeat_seconds: int = Field(default=30, ge=1, le=300)
    import_upload_claim_stale_seconds: int = Field(default=300, ge=30, le=3600)
    import_upload_retry_after_seconds: int = Field(default=2, ge=1, le=60)

    @model_validator(mode="after")
    def validate_import_upload_inflight_limits(self) -> "Settings":
        if self.import_upload_user_inflight_limit > self.import_upload_global_inflight_limit:
            raise ValueError("per-user upload limit cannot exceed the global upload limit")
        if (
            self.import_upload_claim_heartbeat_seconds * 3
            > self.import_upload_claim_stale_seconds
        ):
            raise ValueError("upload claim stale window must cover at least three heartbeats")
        return self
    doc_conversion_max_bytes: int = Field(
        default=200 * 1024 * 1024,
        ge=1024 * 1024,
        le=500 * 1024 * 1024,
    )
    doc_converter_binary: str = "soffice"
    doc_converter_concurrency: int = Field(default=2, ge=1, le=4)
    doc_conversion_timeout_seconds: int = Field(default=180, ge=10, le=3600)
    doc_converter_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    doc_converter_stale_seconds: int = Field(default=600, ge=30, le=7200)
    doc_converter_max_attempts: int = Field(default=3, ge=1, le=10)
    import_worker_batch_size: int = Field(default=4, ge=1, le=16)
    import_worker_poll_seconds: float = 1.0
    import_worker_stale_seconds: int = 3600
    import_worker_max_attempts: int = 5
    import_staging_dir: str = "storage/import_staging"
    import_staging_retention_seconds: int = 24 * 60 * 60
    # XLSX ZIP 元数据和流式解析上限。
    xlsx_max_zip_entries: int = 4096
    xlsx_max_zip_compressed_bytes: int = 500 * 1024 * 1024
    xlsx_max_zip_uncompressed_bytes: int = 256 * 1024 * 1024
    xlsx_max_zip_entry_bytes: int = 128 * 1024 * 1024
    xlsx_max_zip_compression_ratio: int = 100
    xlsx_max_sheets: int = 100
    xlsx_max_rows: int = 100_000
    xlsx_max_cells: int = 1_000_000
    xlsx_max_cell_text_chars: int = 32_768
    xlsx_max_text_chars: int = 10_000_000
    xls_max_input_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=1024 * 1024,
        le=500 * 1024 * 1024,
    )
    # 原始上传文件持久化目录（相对路径基于仓库根目录）。
    document_files_dir: str = "storage/document_files"
    revision_file_storage_enabled: bool = False
    document_storage_provider: str = "local"
    document_storage_endpoint_ref: str = "primary"
    document_storage_endpoint_url: str = ""
    document_storage_bucket: str = ""
    document_storage_access_key: SecretStr = SecretStr("")
    document_storage_secret_key: SecretStr = SecretStr("")
    document_storage_region: str = ""
    document_storage_max_read_bytes: int = 500 * 1024 * 1024
    document_storage_signed_url_seconds: int = 300
    revision_retention_enabled: bool = False
    revision_retention_batch_size: int = 50
    revision_retention_impact_evidence_sample: int = 20
    revision_cleanup_enabled: bool = False
    revision_cleanup_batch_size: int = 16
    revision_cleanup_lease_seconds: int = 300
    revision_cleanup_max_attempts: int = 10
    revision_coordinated_purge_enabled: bool = False
    revision_coordinated_purge_batch_size: int = 10
    revision_coordinated_purge_lease_seconds: int = 300
    revision_coordinated_purge_max_attempts: int = 5
    revision_coordinated_purge_max_evidence: int = 10_000
    revision_coordinated_purge_max_affected_items: int = 1_000
    revision_coordinated_purge_impact_sample: int = 50

    # ---- 检索可见性过滤（#6 批次 A，revision 维度）----
    retrieval_consistency_filter: bool = True  # 总开关；关掉则不回查 PG（灰度/回滚用）
    visibility_overfetch_factor: int = 3  # 初始 overfetch 倍数（相对 top_k / 召回数）
    visibility_overfetch_max: int = 200  # 单次召回上限
    visibility_refetch_max_rounds: int = 2  # 过滤后不足时的补召回轮数上限（2~3）
    visibility_total_candidate_cap: int = 500  # 累计候选硬上限
    visibility_latency_budget_ms: int = 800  # 检索补召回的延迟预算（毫秒）
    # worker 写 Qdrant 的有界超时（秒）——锁内只做一次，禁止长退避
    qdrant_upsert_timeout_seconds: float = 30.0

    # ---- Worker ----
    embed_batch_size: int = 32
    embed_worker_batch_docs: int = 8
    embed_worker_poll_seconds: float = 1.0
    embed_worker_stale_seconds: int = 3600
    embed_worker_max_attempts: int = 5
    # 自检失败时 worker 进入 degraded（暂停消费），每隔这么多秒重测一次
    worker_degraded_retry_seconds: int = 30
    # rebuild operation 周期收口间隔（秒）：即使持续有任务也定期 reconcile，防崩溃遗留 operation 卡住
    worker_reconcile_seconds: int = 30

    # ---- Cleanup Worker（#7：Qdrant 物理清理 outbox 消费）----
    cleanup_worker_batch: int = 16  # 单轮抢多少条 outbox
    cleanup_worker_poll_seconds: float = 2.0  # 空闲轮询间隔
    cleanup_worker_max_attempts: int = 10  # 超过即标 failed（死信）
    cleanup_backoff_base_seconds: float = 5.0  # 指数退避基数
    cleanup_backoff_max_seconds: float = 900.0  # 退避上限（15min）
    cleanup_stale_seconds: int = 600  # processing 超时重置为 pending

    # ---- Heartbeat / 运行状态监控（docs/26）----
    heartbeat_interval_seconds: int = 15  # 各进程心跳写入间隔
    heartbeat_offline_seconds: int = 60  # 超过该秒数未更新即判离线
    heartbeat_prune_seconds: int = 3600  # 超过该秒数未更新即清理过期心跳行

    # ---- Chunking defaults ----
    default_chunk_size: int = 1000
    default_chunk_overlap: int = 120

    # ---- 跨库正文补全（source enrichment）----
    # 整体回查预算（秒）：超过则放弃补全、回退 payload.text，不阻塞检索
    source_enrich_timeout: float = 15.0
    # 建池 / 连接源库的超时（秒）：防止源库不可达时无限挂起、拖垮主库会话
    source_enrich_connect_timeout: float = 10.0

    # ---- 全文源数据库连接（案件库等外部正文库所在的 PG）----
    # 全部留空则回退上面的主库 DB_* 值。案件库（cpwsdata）地址即配在这里，随时可改。
    source_db_host: str = ""
    source_db_port: int = 0
    source_db_user: str = ""
    source_db_password: str = ""
    # 外部正文库名（如 cpwsdata）；库的 source_config 未写 db_name 时用它
    source_db_name: str = ""

    @property
    def source_db_host_effective(self) -> str:
        return self.source_db_host or self.db_host

    @property
    def source_db_port_effective(self) -> int:
        return self.source_db_port or self.db_port

    @property
    def source_db_user_effective(self) -> str:
        return self.source_db_user or self.db_user

    @property
    def source_db_password_effective(self) -> str:
        return self.source_db_password or self.db_password

    @property
    def source_db_name_effective(self) -> str:
        """库的 source_config 未指定 db_name 时用的默认外部源库名（案件库走这里）。"""
        return self.source_db_name or self.db_name

    # ---- 全文源「约定」默认值（新建库自动套用）----
    # 源表正文列（统一）
    source_text_column: str = "content"
    # Qdrant payload 里的外键字段名（统一）
    source_key_field: str = "text_id"
    # 外键类型（统一）
    source_key_type: str = "bigint"

    # ---- v0.2 Evidence Foundation feature flags ----
    enable_evidence_write_path: bool = False
    enable_evidence_locator_read: bool = True
    enable_evidence_locator_projection: bool = True
    enable_revision_id_worker: bool = False
    enable_revision_id_visibility: bool = False
    enable_sync_source_api: bool = False
    sync_batch_max_items: int = 100
    sync_document_text_max_chars: int = 2_000_000

    # ---- v0.3 Graph Relation Foundation feature flags ----
    graph_v03_enabled: bool = False

    # ---- v0.4 Graph Extraction Pipeline (M1 defaults; fail closed) ----
    graph_extraction_enabled: bool = False
    graph_claim_shadow_enabled: bool = False
    graph_extraction_auto_trigger_enabled: bool = False
    graph_extraction_base_url: str = "https://api.deepseek.com/v1"
    graph_extraction_model: str = "deepseek-v4-pro"
    graph_extraction_api_key: SecretStr = SecretStr("")
    graph_extraction_timeout_seconds: float = 240.0
    graph_extraction_temperature: float = 0.0
    graph_extraction_response_format: str = "json_object"
    graph_extraction_max_context_chars: int = 24_000
    graph_extraction_context_window_tokens: int = 10_240
    graph_extraction_previous_chunks: int = 1
    graph_extraction_next_chunks: int = 1
    graph_extraction_default_build_mode: str = "standard"
    graph_extraction_schema_routing_enabled: bool = True
    graph_extraction_cache_enabled: bool = False
    graph_extraction_center_only_enabled: bool = True
    graph_extraction_output_budget_enabled: bool = True
    graph_extraction_max_output_tokens: int = 8_000
    graph_extraction_nuextract_review_library_ids: str = ""
    graph_extraction_nuextract_base_url: str = "http://nuextract3-gpu0:8000/v1"
    graph_extraction_nuextract_model: str = "nuextract3"
    graph_extraction_nuextract_timeout_seconds: float = 240.0
    graph_extraction_nuextract_max_output_tokens: int = 4_000
    graph_extraction_minstral_base_url: str = "http://graph-minstral-3b:8000/v1"
    graph_extraction_minstral_model: str = "graph-minstral-3b"
    graph_extraction_minstral_timeout_seconds: float = 240.0
    graph_extraction_minstral_max_output_tokens: int = 1_000
    graph_extraction_qwen3_draft_base_url: str = "http://graph-qwen3-4b:8000/v1"
    graph_extraction_qwen3_draft_model: str = "graph-qwen3-4b"
    graph_extraction_qwen3_draft_timeout_seconds: float = 240.0
    graph_extraction_qwen3_draft_max_output_tokens: int = 1_000
    graph_schema_discovery_max_source_chunks: int = 8
    graph_schema_discovery_concept_inventory_enabled: bool = False
    graph_schema_discovery_timeout_seconds: float = 300.0
    graph_schema_discovery_context_window_tokens: int = 16_384
    graph_schema_discovery_max_output_tokens: int = 8_000

    graph_extraction_prompt_version: str = "v1"
    graph_extraction_extractor_version: str = "v1"
    graph_extraction_output_parser_version: str = "v1"
    graph_extraction_context_policy_version: str = "v1"
    graph_extraction_policy_version: str = "v1"
    graph_extraction_normalization_rule_version: str = "normalization_v1"
    graph_extraction_confidence_policy_version: str = "v1"

    graph_extraction_entity_materialization_threshold: float = 0.85
    graph_extraction_relation_draft_threshold: float = 0.85
    graph_extraction_weight_model: float = 0.25
    graph_extraction_weight_evidence: float = 0.35
    graph_extraction_weight_schema: float = 0.25
    graph_extraction_weight_normalization: float = 0.15
    graph_extraction_auto_evidence_types: str = "direct_statement,table_cell"
    graph_extraction_evidence_group_policy: str = "all_claims_valid"

    graph_extraction_worker_poll_seconds: float = 3.0
    graph_extraction_unit_lease_seconds: int = 360
    graph_extraction_unit_lease_renew_seconds: int = 30
    graph_extraction_worker_max_model_attempts: int = 3
    graph_extraction_batch_size: int = 1
    graph_extraction_worker_concurrency: int = 6
    graph_extraction_provider_max_concurrency: int = 4
    graph_extraction_review_max_concurrency: int = 1
    graph_extraction_provider_max_retries: int = 2
    graph_extraction_provider_backoff_base_seconds: float = 1.0

    graph_extraction_context_retention_days: int = 30
    graph_extraction_raw_output_retention_days: int = 30
    graph_extraction_candidate_retention_days: int = 180

    # ---- v0.8 Knowledge Artifact Runtime (default fail closed) ----
    knowledge_artifact_runtime_enabled: bool = False
    knowledge_artifact_auto_trigger_enabled: bool = False
    knowledge_artifact_external_model_enabled: bool = False
    knowledge_artifact_base_url: str = "https://api.deepseek.com/v1"
    knowledge_artifact_model: str = "deepseek-v4-pro"
    knowledge_artifact_api_key: SecretStr = SecretStr("")
    knowledge_artifact_provider_timeout_seconds: float = 120.0
    knowledge_artifact_short_summary_max_chars: int = 4_000
    knowledge_artifact_model_max_source_chars: int = 24_000
    knowledge_artifact_summary_extractor_version: str = "summary-extractor-v1"
    knowledge_artifact_outline_extractor_version: str = "outline-extractor-v1"
    knowledge_artifact_prompt_version: str = "summary-prompt-v2"
    knowledge_artifact_worker_poll_seconds: float = 3.0
    knowledge_artifact_worker_lease_seconds: int = 180
    knowledge_artifact_worker_renew_seconds: int = 30
    knowledge_artifact_worker_max_attempts: int = 3

    # ---- v0.8 Document Classification Worker (default fail closed) ----
    classification_runtime_enabled: bool = False
    classification_auto_trigger_enabled: bool = False
    classification_external_model_enabled: bool = False
    classification_base_url: str = "https://api.deepseek.com/v1"
    classification_model: str = "deepseek-v4-pro"
    classification_api_key: SecretStr = SecretStr("")
    classification_provider_timeout_seconds: float = 120.0
    classification_model_max_source_chars: int = 8_000
    classification_classifier_version: str = "document-classifier-v1"
    classification_prompt_version: str = "classification-prompt-v3"
    classification_worker_poll_seconds: float = 3.0
    classification_worker_lease_seconds: int = 180
    classification_worker_renew_seconds: int = 30
    classification_worker_max_attempts: int = 3

    # ---- v0.8 Classification Taxonomy Bootstrap (default fail closed) ----
    classification_taxonomy_bootstrap_enabled: bool = False
    classification_taxonomy_bootstrap_llm_enabled: bool = False
    classification_taxonomy_bootstrap_max_samples: int = 20
    classification_taxonomy_bootstrap_max_source_chars: int = 24_000
    classification_taxonomy_bootstrap_attempt_seconds: int = 180
    classification_taxonomy_bootstrap_prompt_version: str = "taxonomy-bootstrap-v1"

    # ---- v0.8 Document-First Knowledge Catalog (default fail closed) ----
    knowledge_catalog_enabled: bool = False
    knowledge_catalog_max_entity_cards: int = 50
    knowledge_catalog_max_relation_cards: int = 50
    knowledge_catalog_max_evidence_per_fact: int = 20

    # ---- v0.9 Organization Graph Catalog (default fail closed) ----
    graph_catalog_enabled: bool = False

    # ---- v0.9 Graph Governance Actions (default fail closed) ----
    graph_governance_enabled: bool = False

    # ---- v0.9 Schema Lifecycle Console (default fail closed) ----
    schema_lifecycle_enabled: bool = False

    # ---- v0.9 Public Read API v1 (default fail closed) ----
    public_api_v1_enabled: bool = False

    # ---- v0.9 Public API operational controls (default inert) ----
    public_api_operations_enabled: bool = False
    public_api_limits_enabled: bool = False
    public_api_limit_private_organizations: bool = False
    public_api_organization_requests_per_minute: int = 600
    public_api_api_key_requests_per_minute: int = 120
    public_api_organization_concurrent_answers: int = 20
    public_api_api_key_concurrent_answers: int = 4
    public_api_answer_max_seconds: int = 300
    public_api_answer_lease_seconds: int = 330
    public_api_request_retention_days: int = 30
    public_api_cleanup_batch_size: int = 1000
    public_api_cleanup_interval_seconds: int = 3600

    # ---- v0.9 External graph fact synchronization (default fail closed) ----
    external_graph_sync_enabled: bool = False
    external_graph_sync_max_batch_items: int = 100
    external_graph_sync_max_item_bytes: int = 65536

    # ---- v0.5 Active Graph Publication (M1 defaults; fail closed) ----
    graph_publication_enabled: bool = False
    graph_publication_require_entity_evidence: bool = True
    graph_publication_extracted_entity_min_confidence: float = 0.85
    graph_publication_extracted_relation_min_confidence: float = 0.85
    graph_publication_max_items_per_run: int = 10_000
    graph_publication_policy_version: str = "v1"
    graph_publication_manifest_version: str = "v1"

    # ---- v0.6 Published Graph Retrieval (M1 contract; fail closed) ----
    graph_retrieval_enabled: bool = False
    graph_retrieval_contract_version: str = "v1"
    graph_retrieval_max_seeds: int = 10
    graph_retrieval_max_hops: int = 3
    graph_retrieval_max_nodes: int = 100
    graph_retrieval_max_relations: int = 200
    graph_retrieval_max_evidence_per_fact: int = 20
    graph_retrieval_timeout_seconds: float = 3.0

    # ---- v0.7 Publication-scoped Entity Linking (default fail closed) ----
    entity_linking_enabled: bool = False
    entity_linking_contract_version: str = "v1"
    entity_linking_policy_version: str = "entity-linking-policy-v2"
    entity_linking_policy_path: str = "eval/entity_linking/link_policy_v11.json"
    entity_linking_policy_sha256: str = ""
    entity_linking_min_score_micros: int = 920_000
    entity_linking_min_margin_micros: int = 120_000
    entity_linking_candidate_floor_micros: int = 500_000
    entity_linking_max_mentions: int = 10
    entity_linking_max_candidates: int = 10
    entity_linking_max_publication_entities: int = 10_000
    entity_linking_timeout_seconds: float = 2.0


def validate_graph_extraction_startup(config: Settings) -> None:
    weights = {
        "model": config.graph_extraction_weight_model,
        "evidence": config.graph_extraction_weight_evidence,
        "schema": config.graph_extraction_weight_schema,
        "normalization": config.graph_extraction_weight_normalization,
    }
    if any(value < 0 or value > 1 for value in weights.values()):
        raise RuntimeError("[security] graph extraction confidence weight must be within [0, 1]")
    if not math.isclose(math.fsum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise RuntimeError("[security] graph extraction confidence weights must sum to 1.0")

    lease = config.graph_extraction_unit_lease_seconds
    renew = config.graph_extraction_unit_lease_renew_seconds
    if renew >= lease / 2:
        raise RuntimeError("[security] graph extraction lease renew must be less than half the lease")

    retention_values = (
        config.graph_extraction_context_retention_days,
        config.graph_extraction_raw_output_retention_days,
        config.graph_extraction_candidate_retention_days,
    )
    if any(value <= 0 for value in retention_values):
        raise RuntimeError("[security] graph extraction retention days must be positive")

    if config.graph_extraction_timeout_seconds >= lease:
        raise RuntimeError("[security] graph extraction provider timeout must be below the Unit lease")
    if not 1 <= config.graph_extraction_batch_size <= 8:
        raise RuntimeError("[security] graph extraction batch size must be within 1..8")
    if not 1 <= config.graph_extraction_worker_concurrency <= 8:
        raise RuntimeError("[security] graph extraction worker concurrency must be within 1..8")
    if not 512 <= config.graph_extraction_context_window_tokens <= 32_768:
        raise RuntimeError("[security] graph extraction context window must be within 512..32768 tokens")

    if not 1 <= config.graph_extraction_provider_max_concurrency <= 8:
        raise RuntimeError("[security] graph extraction Provider concurrency must be within 1..8")
    if not 1 <= config.graph_extraction_review_max_concurrency <= 4:
        raise RuntimeError("[security] graph extraction review concurrency must be within 1..4")
    if not 0 <= config.graph_extraction_provider_max_retries <= 5:
        raise RuntimeError("[security] graph extraction Provider retries must be within 0..5")
    if config.graph_extraction_provider_backoff_base_seconds <= 0:
        raise RuntimeError("[security] graph extraction Provider backoff must be positive")
    if not 1 <= config.graph_schema_discovery_max_source_chunks <= 512:
        raise RuntimeError("[security] graph Schema discovery source chunks must be within 1..512")
    if (
        not math.isfinite(config.graph_schema_discovery_timeout_seconds)
        or not 1 <= config.graph_schema_discovery_timeout_seconds <= 3_600
    ):
        raise RuntimeError("[security] graph Schema discovery timeout must be within 1..3600 seconds")
    if not 512 <= config.graph_schema_discovery_context_window_tokens <= 32_768:
        raise RuntimeError(
            "[security] graph Schema discovery context window must be within 512..32768 tokens"
        )
    schema_output_budget = config.graph_schema_discovery_max_output_tokens
    if (
        isinstance(schema_output_budget, bool)
        or not isinstance(schema_output_budget, int)
        or not 256 <= schema_output_budget <= 32_768
    ):
        raise RuntimeError(
            "[security] graph Schema discovery output token budget must be within [256, 32768]"
        )
    if config.graph_extraction_default_build_mode not in {"fast", "standard", "deep"}:
        raise RuntimeError("[security] graph extraction default build mode must be fast, standard, or deep")

    output_budget = config.graph_extraction_max_output_tokens
    if (
        isinstance(output_budget, bool)
        or not isinstance(output_budget, int)
        or not 256 <= output_budget <= 32_768
    ):
        raise RuntimeError("[security] graph extraction output token budget must be within [256, 32768]")

    review_library_ids = [
        item.strip()
        for item in config.graph_extraction_nuextract_review_library_ids.split(",")
        if item.strip()
    ]
    try:
        if len(review_library_ids) != len(set(review_library_ids)):
            raise ValueError("duplicate library id")
        for item in review_library_ids:
            uuid.UUID(item)
    except ValueError as exc:
        raise RuntimeError(
            "[security] NuExtract review library ids must be unique UUID values"
        ) from exc
    if review_library_ids:
        if config.graph_extraction_nuextract_timeout_seconds >= lease:
            raise RuntimeError("[security] NuExtract timeout must be below the Unit lease")
        if not 256 <= config.graph_extraction_nuextract_max_output_tokens <= 8_192:
            raise RuntimeError("[security] NuExtract output budget must be within [256, 8192]")
        from app.services.graph_extraction_provider import graph_extraction_provider_name

        try:
            graph_extraction_provider_name(
                base_url=config.graph_extraction_nuextract_base_url,
                model=config.graph_extraction_nuextract_model,
            )
        except ValueError as exc:
            raise RuntimeError(
                "[security] NuExtract review requires the approved private provider contract"
            ) from exc

    if config.graph_extraction_minstral_timeout_seconds >= lease:
        raise RuntimeError("[security] Ministral timeout must be below the Unit lease")
    if not 256 <= config.graph_extraction_minstral_max_output_tokens <= 4_096:
        raise RuntimeError("[security] Ministral output budget must be within [256, 4096]")
    from app.services.graph_extraction_provider import graph_extraction_provider_name

    try:
        graph_extraction_provider_name(
            base_url=config.graph_extraction_minstral_base_url,
            model=config.graph_extraction_minstral_model,
        )
    except ValueError as exc:
        raise RuntimeError(
            "[security] Ministral review requires the approved private provider contract"
        ) from exc

    if config.graph_extraction_qwen3_draft_timeout_seconds >= lease:
        raise RuntimeError("[security] Qwen3 draft timeout must be below the Unit lease")
    if not 256 <= config.graph_extraction_qwen3_draft_max_output_tokens <= 4_096:
        raise RuntimeError("[security] Qwen3 draft output budget must be within [256, 4096]")
    try:
        graph_extraction_provider_name(
            base_url=config.graph_extraction_qwen3_draft_base_url,
            model=config.graph_extraction_qwen3_draft_model,
        )
    except ValueError as exc:
        raise RuntimeError(
            "[security] Qwen3 draft requires the approved private provider contract"
        ) from exc

    if config.graph_extraction_enabled:
        from app.services.graph_extraction_provider import (
            graph_extraction_provider_name,
        )

        try:
            graph_extraction_provider_name(
                base_url=config.graph_extraction_base_url,
                model=config.graph_extraction_model,
            )
        except ValueError as exc:
            raise RuntimeError(
                "[security] graph extraction requires an approved frozen provider configuration"
            ) from exc

    if (
        config.graph_extraction_auto_trigger_enabled
        and not config.graph_extraction_api_key.get_secret_value().strip()
    ):
        raise RuntimeError("[security] GRAPH_EXTRACTION_API_KEY is required when auto trigger is enabled")


def validate_knowledge_artifact_startup(config: Settings) -> None:
    if config.knowledge_artifact_auto_trigger_enabled and not (config.knowledge_artifact_runtime_enabled):
        raise RuntimeError("[security] knowledge artifact auto trigger requires the runtime")

    positive_values = (
        config.knowledge_artifact_provider_timeout_seconds,
        config.knowledge_artifact_short_summary_max_chars,
        config.knowledge_artifact_model_max_source_chars,
        config.knowledge_artifact_worker_poll_seconds,
        config.knowledge_artifact_worker_lease_seconds,
        config.knowledge_artifact_worker_renew_seconds,
        config.knowledge_artifact_worker_max_attempts,
    )
    if any(not math.isfinite(value) or value <= 0 for value in positive_values):
        raise RuntimeError("[security] knowledge artifact runtime limits must be positive")
    if config.knowledge_artifact_short_summary_max_chars > config.knowledge_artifact_model_max_source_chars:
        raise RuntimeError("[security] short Summary limit must not exceed model source limit")
    lease = config.knowledge_artifact_worker_lease_seconds
    if config.knowledge_artifact_worker_renew_seconds >= lease / 2:
        raise RuntimeError("[security] knowledge artifact lease renew must be less than half the lease")
    if config.knowledge_artifact_provider_timeout_seconds >= lease:
        raise RuntimeError("[security] knowledge artifact provider timeout must be below the lease")
    version_values = (
        config.knowledge_artifact_summary_extractor_version,
        config.knowledge_artifact_outline_extractor_version,
        config.knowledge_artifact_prompt_version,
    )
    if any(not value.strip() or len(value) > 64 for value in version_values):
        raise RuntimeError("[security] knowledge artifact versions must be nonblank and bounded")
    if not config.knowledge_artifact_external_model_enabled:
        return
    if not config.graph_extraction_base_url.strip() or not config.graph_extraction_model.strip():
        raise RuntimeError("[security] knowledge artifact model endpoint and model are required")
    if not config.graph_extraction_api_key.get_secret_value().strip():
        raise RuntimeError("[security] GRAPH_EXTRACTION_API_KEY is required for Summary generation")


def validate_classification_runtime_startup(config: Settings) -> None:
    if config.classification_runtime_enabled and not (
        config.classification_decision_enabled
        and config.classification_taxonomy_enabled
        and config.organization_authorization_enabled
    ):
        raise RuntimeError(
            "[security] classification runtime requires decisions, taxonomy, and Organization authorization"
        )
    if config.classification_auto_trigger_enabled and not (
        config.classification_runtime_enabled and config.classification_external_model_enabled
    ):
        raise RuntimeError("[security] classification auto trigger requires runtime and model")
    if config.classification_external_model_enabled and not (
        config.classification_runtime_enabled or config.classification_taxonomy_bootstrap_llm_enabled
    ):
        raise RuntimeError(
            "[security] classification external model requires classifier runtime or taxonomy bootstrap LLM"
        )
    positive_values = (
        config.classification_provider_timeout_seconds,
        config.classification_model_max_source_chars,
        config.classification_worker_poll_seconds,
        config.classification_worker_lease_seconds,
        config.classification_worker_renew_seconds,
        config.classification_worker_max_attempts,
    )
    if any(not math.isfinite(value) or value <= 0 for value in positive_values):
        raise RuntimeError("[security] classification runtime limits must be positive")
    if not 1_000 <= config.classification_model_max_source_chars <= 100_000:
        raise RuntimeError("[security] classification source limit must be within 1000..100000")
    lease = config.classification_worker_lease_seconds
    if config.classification_worker_renew_seconds >= lease / 2:
        raise RuntimeError("[security] classification lease renew must be less than half the lease")
    if config.classification_provider_timeout_seconds >= lease:
        raise RuntimeError("[security] classification provider timeout must be below the lease")
    versions = (
        config.classification_classifier_version,
        config.classification_prompt_version,
    )
    if any(not value.strip() or len(value) > 64 for value in versions):
        raise RuntimeError("[security] classification versions must be nonblank and bounded")
    if not config.classification_external_model_enabled:
        return
    provider_contract = (config.classification_base_url, config.classification_model)
    if provider_contract not in {
        ("https://api.deepseek.com/v1", "deepseek-v4-pro"),
        ("https://api.deepseek.com/v1", "deepseek-v4-flash"),
        ("http://10.0.10.2:8113/v1", "qwen3.5-9b"),
        ("https://model.rhzy.ai/v1", "gemma4-31b-uncensored-bf16-256k-seq4"),
        ("http://10.0.10.2:8114/v1", "gemma4-31b-uncensored-bf16-256k-seq4"),
    }:
        raise RuntimeError("[security] classification requires an approved provider identity")
    if not config.classification_api_key.get_secret_value().strip():
        raise RuntimeError("[security] CLASSIFICATION_API_KEY is required for classification")


def validate_classification_taxonomy_bootstrap_startup(config: Settings) -> None:
    if config.classification_taxonomy_bootstrap_enabled and not (
        config.organization_authorization_enabled and config.classification_taxonomy_enabled
    ):
        raise RuntimeError(
            "[security] taxonomy bootstrap requires Organization authorization and classification taxonomy"
        )
    if config.classification_taxonomy_bootstrap_llm_enabled and not (
        config.classification_taxonomy_bootstrap_enabled and config.classification_external_model_enabled
    ):
        raise RuntimeError("[security] taxonomy bootstrap LLM requires bootstrap and the approved model")
    values = (
        config.classification_taxonomy_bootstrap_max_samples,
        config.classification_taxonomy_bootstrap_max_source_chars,
        config.classification_taxonomy_bootstrap_attempt_seconds,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise RuntimeError("[security] taxonomy bootstrap limits must be integers")
    if not 1 <= config.classification_taxonomy_bootstrap_max_samples <= 20:
        raise RuntimeError("[security] taxonomy bootstrap sample limit must be within 1..20")
    if not 1_000 <= config.classification_taxonomy_bootstrap_max_source_chars <= 100_000:
        raise RuntimeError("[security] taxonomy bootstrap source limit must be within 1000..100000")
    if not 1 <= config.classification_taxonomy_bootstrap_attempt_seconds <= 900:
        raise RuntimeError("[security] taxonomy bootstrap attempt lifetime must be within 1..900 seconds")
    if (
        config.classification_taxonomy_bootstrap_llm_enabled
        and config.classification_provider_timeout_seconds
        >= config.classification_taxonomy_bootstrap_attempt_seconds
    ):
        raise RuntimeError(
            "[security] taxonomy bootstrap attempt lifetime must exceed provider timeout "
            "and be at most 900 seconds"
        )
    version = config.classification_taxonomy_bootstrap_prompt_version
    if not version.strip() or len(version) > 64:
        raise RuntimeError("[security] taxonomy bootstrap prompt version is invalid")


def validate_knowledge_catalog_startup(config: Settings) -> None:
    if config.knowledge_catalog_enabled and not config.organization_authorization_enabled:
        raise RuntimeError("[security] knowledge Catalog requires Organization authorization")
    values = (
        config.knowledge_catalog_max_entity_cards,
        config.knowledge_catalog_max_relation_cards,
        config.knowledge_catalog_max_evidence_per_fact,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise RuntimeError("[security] knowledge Catalog limits must be integers")
    if not 1 <= config.knowledge_catalog_max_entity_cards <= 100:
        raise RuntimeError("[security] knowledge Catalog entity limit must be within 1..100")
    if not 1 <= config.knowledge_catalog_max_relation_cards <= 100:
        raise RuntimeError("[security] knowledge Catalog relation limit must be within 1..100")
    if not 1 <= config.knowledge_catalog_max_evidence_per_fact <= 20:
        raise RuntimeError("[security] knowledge Catalog Evidence limit must be within 1..20")


def validate_graph_catalog_startup(config: Settings) -> None:
    if config.graph_catalog_enabled and not (
        config.organization_authorization_enabled and config.cross_library_compatibility_enabled
    ):
        raise RuntimeError(
            "[security] graph Catalog requires Organization authorization and cross-Library compatibility"
        )


def validate_graph_governance_startup(config: Settings) -> None:
    if config.graph_governance_enabled and not (
        config.organization_authorization_enabled and config.graph_publication_enabled
    ):
        raise RuntimeError(
            "[security] graph governance requires Organization authorization and graph publication"
        )


def validate_external_graph_sync_startup(config: Settings) -> None:
    if config.external_graph_sync_enabled and not (
        config.organization_authorization_enabled
        and config.graph_governance_enabled
        and config.graph_publication_enabled
        and config.enable_sync_source_api
    ):
        raise RuntimeError(
            "[security] external graph sync requires Organization authorization, "
            "graph governance, graph publication, and SyncSource API"
        )
    if not 1 <= config.external_graph_sync_max_batch_items <= 100:
        raise RuntimeError("[security] external graph sync batch limit must be within 1..100")
    if not 1024 <= config.external_graph_sync_max_item_bytes <= 65536:
        raise RuntimeError("[security] external graph sync item bytes must be within 1024..65536")


def validate_schema_lifecycle_startup(config: Settings) -> None:
    if config.schema_lifecycle_enabled and not config.organization_authorization_enabled:
        raise RuntimeError("[security] Schema lifecycle requires Organization authorization")


def validate_public_api_v1_startup(config: Settings) -> None:
    if not config.public_api_v1_enabled:
        return
    dependencies = (
        config.organization_authorization_enabled,
        config.cross_library_compatibility_enabled,
        config.personal_library_scopes_enabled,
        config.federated_retrieval_enabled,
        config.knowledge_catalog_enabled,
        config.graph_catalog_enabled,
    )
    if not all(dependencies):
        raise RuntimeError(
            "[security] Public API v1 requires Organization authorization, "
            "cross-Library compatibility, personal scopes, federated retrieval, "
            "Knowledge Catalog, and Graph Catalog"
        )


def validate_public_api_operations_startup(config: Settings) -> None:
    rate_limits = (
        config.public_api_organization_requests_per_minute,
        config.public_api_api_key_requests_per_minute,
    )
    if any(value < 1 or value > 1_000_000 for value in rate_limits):
        raise RuntimeError("[security] Public API request limits must be within 1..1000000")
    concurrent_limits = (
        config.public_api_organization_concurrent_answers,
        config.public_api_api_key_concurrent_answers,
    )
    if any(value < 1 or value > 10_000 for value in concurrent_limits):
        raise RuntimeError("[security] Public API concurrent answer limits must be within 1..10000")
    if not 1 <= config.public_api_answer_max_seconds <= 3_600:
        raise RuntimeError("[security] Public API answer timeout must be within 1..3600 seconds")
    if not (config.public_api_answer_max_seconds + 30 <= config.public_api_answer_lease_seconds <= 7_200):
        raise RuntimeError(
            "[security] Public API answer lease must exceed the timeout by at least 30 "
            "seconds and be at most 7200 seconds"
        )
    if not 1 <= config.public_api_request_retention_days <= 3_650:
        raise RuntimeError("[security] Public API request retention must be within 1..3650 days")
    if not 1 <= config.public_api_cleanup_batch_size <= 10_000:
        raise RuntimeError("[security] Public API cleanup batch size must be within 1..10000")
    if not 60 <= config.public_api_cleanup_interval_seconds <= 86_400:
        raise RuntimeError("[security] Public API cleanup interval must be within 60..86400 seconds")
    if config.public_api_operations_enabled and not (
        config.public_api_v1_enabled and config.organization_authorization_enabled
    ):
        raise RuntimeError(
            "[security] Public API operations require Public API v1 and Organization authorization"
        )
    if config.public_api_limits_enabled and not config.public_api_operations_enabled:
        raise RuntimeError("[security] Public API limits require Public API operations")
    if config.public_api_limit_private_organizations and not config.public_api_limits_enabled:
        raise RuntimeError("[security] Private Organization limits require Public API limits")


def validate_supported_deployment_startup(config: Settings) -> None:
    """Reject unsafe or ambiguous supported-deployment configuration."""
    if config.deployment_profile not in {"development", "hosted", "private"}:
        raise RuntimeError("[deployment] DEPLOYMENT_PROFILE must be development, hosted, or private")
    if not 0.2 <= config.readiness_dependency_timeout_seconds <= 30:
        raise RuntimeError("[deployment] readiness dependency timeout must be within 0.2..30 seconds")
    if config.deployment_profile == "development":
        return
    if config.app_debug:
        raise RuntimeError("[deployment] APP_DEBUG must be false in supported profiles")
    if config.allow_public_registration:
        raise RuntimeError("[deployment] public registration is forbidden in supported profiles")
    if config.jwt_secret == "please-change-me-in-env":
        raise RuntimeError("[deployment] a non-default JWT_SECRET is required")
    if not config.db_password:
        raise RuntimeError("[deployment] DB_PASSWORD is required")
    if not config.cookie_secure:
        raise RuntimeError("[deployment] COOKIE_SECURE must be true")
    if not config.organization_authorization_enabled:
        raise RuntimeError("[deployment] organization authorization must be enabled")
    if config.public_api_v1_enabled and not config.public_api_operations_enabled:
        raise RuntimeError("[deployment] public API operations must be enabled with public API v1")
    if not config.revision_file_storage_enabled:
        raise RuntimeError("[deployment] immutable revision file storage must be enabled")
    if config.deployment_profile == "hosted" and config.document_storage_provider == "local":
        raise RuntimeError("[deployment] hosted profile requires remote document storage")
    for name, value in (
        ("QDRANT_URL", config.qdrant_url),
        ("EMBEDDING_BASE_URL", config.embedding_base_url),
    ):
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError(f"[deployment] {name} is invalid")
        if config.deployment_profile == "hosted" and parsed.scheme != "https":
            raise RuntimeError(f"[deployment] hosted {name} must use HTTPS")


def validate_revision_file_storage_startup(config: Settings) -> None:
    if not config.revision_file_storage_enabled:
        return
    if config.document_storage_max_read_bytes <= 0:
        raise RuntimeError("[security] document storage read limit must be positive")
    if not 30 <= config.document_storage_signed_url_seconds <= 3_600:
        raise RuntimeError("[security] document storage signed URL lifetime must be within 30..3600 seconds")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
        config.document_storage_endpoint_ref,
    ):
        raise RuntimeError("[security] document storage endpoint reference is invalid")
    if config.document_storage_provider not in {"local", "minio", "oss"}:
        raise RuntimeError("[security] document storage provider is unsupported")
    if not config.enable_evidence_write_path:
        raise RuntimeError("[security] revision file storage requires the evidence Revision write path")
    if config.document_storage_provider == "local":
        if not config.document_files_dir.strip():
            raise RuntimeError("[security] local document storage root is required")
        return
    parsed = urlparse(config.document_storage_endpoint_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("[security] remote document storage endpoint is invalid")
    if config.document_storage_provider == "oss" and parsed.scheme != "https":
        raise RuntimeError("[security] OSS document storage requires HTTPS")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", config.document_storage_bucket):
        raise RuntimeError("[security] remote document storage bucket is invalid")
    if not config.document_storage_access_key.get_secret_value().strip() or not (
        config.document_storage_secret_key.get_secret_value().strip()
    ):
        raise RuntimeError("[security] remote document storage credentials are required")
    dependency = "minio" if config.document_storage_provider == "minio" else "oss2"
    if importlib.util.find_spec(dependency) is None:
        raise RuntimeError(f"[security] document storage optional dependency '{dependency}' is required")


def validate_revision_retention_startup(config: Settings) -> None:
    if not config.revision_retention_enabled:
        return
    if not 1 <= config.revision_retention_batch_size <= 500:
        raise RuntimeError("[security] revision retention batch size must be within 1..500")
    if not 1 <= config.revision_retention_impact_evidence_sample <= 100:
        raise RuntimeError("[security] revision retention Evidence sample must be within 1..100")


def validate_revision_cleanup_startup(config: Settings) -> None:
    if not config.revision_cleanup_enabled:
        return
    if not config.revision_retention_enabled:
        raise RuntimeError("[security] revision cleanup requires retention governance")
    if not config.revision_file_storage_enabled:
        raise RuntimeError("[security] revision cleanup requires revision file storage")
    if not 1 <= config.revision_cleanup_batch_size <= 100:
        raise RuntimeError("[security] revision cleanup batch size must be within 1..100")
    if not 30 <= config.revision_cleanup_lease_seconds <= 3_600:
        raise RuntimeError("[security] revision cleanup lease must be within 30..3600 seconds")
    if not 1 <= config.revision_cleanup_max_attempts <= 100:
        raise RuntimeError("[security] revision cleanup max attempts must be within 1..100")


def validate_revision_coordinated_purge_startup(config: Settings) -> None:
    if not config.revision_coordinated_purge_enabled:
        return
    dependencies = (
        config.graph_publication_enabled,
        config.revision_retention_enabled,
        config.revision_file_storage_enabled,
        config.revision_cleanup_enabled,
    )
    if not all(dependencies):
        raise RuntimeError(
            "[security] coordinated purge requires graph publication, retention, "
            "revision file storage, and revision cleanup"
        )
    if not 1 <= config.revision_coordinated_purge_batch_size <= 100:
        raise RuntimeError("[security] coordinated purge batch size must be within 1..100")
    if not 30 <= config.revision_coordinated_purge_lease_seconds <= 3_600:
        raise RuntimeError("[security] coordinated purge lease must be within 30..3600 seconds")
    if not 1 <= config.revision_coordinated_purge_max_attempts <= 100:
        raise RuntimeError("[security] coordinated purge max attempts must be within 1..100")
    if not 1 <= config.revision_coordinated_purge_max_evidence <= 100_000:
        raise RuntimeError("[security] coordinated purge Evidence limit must be within 1..100000")
    if not 1 <= config.revision_coordinated_purge_max_affected_items <= 10_000:
        raise RuntimeError("[security] coordinated purge item limit must be within 1..10000")
    if not 1 <= config.revision_coordinated_purge_impact_sample <= 100:
        raise RuntimeError("[security] coordinated purge impact sample must be within 1..100")


def validate_graph_publication_startup(config: Settings) -> None:
    confidence_values = (
        config.graph_publication_extracted_entity_min_confidence,
        config.graph_publication_extracted_relation_min_confidence,
    )
    if any(value < 0 or value > 1 for value in confidence_values):
        raise RuntimeError("[security] graph publication confidence threshold must be within [0, 1]")
    if config.graph_publication_max_items_per_run <= 0:
        raise RuntimeError("[security] graph publication max item limit must be positive")
    if not config.graph_publication_policy_version.strip():
        raise RuntimeError("[security] graph publication policy version is required")
    if not config.graph_publication_manifest_version.strip():
        raise RuntimeError("[security] graph publication manifest version is required")


def validate_graph_retrieval_startup(config: Settings) -> None:
    if config.graph_retrieval_contract_version != "v1":
        raise RuntimeError("[security] graph retrieval contract version must be v1")

    limits = (
        ("max seeds", config.graph_retrieval_max_seeds, 10),
        ("max hops", config.graph_retrieval_max_hops, 3),
        ("max nodes", config.graph_retrieval_max_nodes, 100),
        ("max relations", config.graph_retrieval_max_relations, 200),
        ("max Evidence per fact", config.graph_retrieval_max_evidence_per_fact, 20),
    )
    for label, value, absolute_cap in limits:
        if value <= 0 or value > absolute_cap:
            raise RuntimeError(f"[security] graph retrieval {label} must be within [1, {absolute_cap}]")
    if config.graph_retrieval_max_nodes < config.graph_retrieval_max_seeds:
        raise RuntimeError("[security] graph retrieval max nodes must cover every seed")
    if (
        not math.isfinite(config.graph_retrieval_timeout_seconds)
        or config.graph_retrieval_timeout_seconds <= 0
    ):
        raise RuntimeError("[security] graph retrieval timeout must be finite and positive")


def validate_entity_linking_startup(config: Settings) -> None:
    if config.entity_linking_contract_version != "v1":
        raise RuntimeError("[security] entity linking contract version must be v1")
    if config.entity_linking_policy_version != "entity-linking-policy-v2":
        raise RuntimeError("[security] entity linking policy version is invalid")
    thresholds = (
        config.entity_linking_min_score_micros,
        config.entity_linking_min_margin_micros,
    )
    if any(value < 0 or value > 1_000_000 for value in thresholds):
        raise RuntimeError("[security] entity linking thresholds must use valid micros")
    if config.entity_linking_candidate_floor_micros != 500_000:
        raise RuntimeError("[security] entity linking candidate floor must be 500000")
    if config.entity_linking_max_mentions != 10:
        raise RuntimeError("[security] entity linking max mentions must be 10")
    if config.entity_linking_max_candidates != 10:
        raise RuntimeError("[security] entity linking max candidates must be 10")
    if config.entity_linking_max_publication_entities != 10_000:
        raise RuntimeError("[security] entity linking max publication entities must be 10000")
    if (
        not math.isfinite(config.entity_linking_timeout_seconds)
        or config.entity_linking_timeout_seconds <= 0
        or config.entity_linking_timeout_seconds > 2.0
    ):
        raise RuntimeError("[security] entity linking timeout must be within (0, 2.0] seconds")
    if not config.entity_linking_enabled:
        return
    if not _SHA256_RE.fullmatch(config.entity_linking_policy_sha256):
        raise RuntimeError("[security] entity linking policy SHA-256 is required")
    policy_path = Path(config.entity_linking_policy_path)
    if not policy_path.is_absolute():
        policy_path = BASE_DIR / policy_path
    try:
        policy_bytes = policy_path.read_bytes()
        policy = json.loads(policy_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("[security] entity linking policy is unavailable") from exc
    if hashlib.sha256(policy_bytes).hexdigest() != config.entity_linking_policy_sha256:
        raise RuntimeError("[security] entity linking policy SHA-256 mismatch")
    if not isinstance(policy, dict) or set(policy) != _ENTITY_LINKING_POLICY_FIELDS:
        raise RuntimeError("[security] entity linking policy schema is invalid")
    if (
        policy.get("schema_version") != "entity-linking-policy-v2"
        or policy.get("policy_version") != config.entity_linking_policy_version
        or policy.get("algorithm_version") != "lexical-score-v2"
        or policy.get("normalization_version") != "normalize_graph_name_v1"
    ):
        raise RuntimeError("[security] entity linking policy identity is invalid")
    approved = policy.get("approved_thresholds")
    if not isinstance(approved, dict) or set(approved) != _ENTITY_LINKING_THRESHOLD_FIELDS:
        raise RuntimeError("[security] entity linking policy thresholds are invalid")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in approved.values()):
        raise RuntimeError("[security] entity linking policy thresholds are invalid")
    if (
        approved["min_score_micros"] not in {850_000, 880_000, 900_000, 920_000, 950_000}
        or approved["min_margin_micros"] not in {80_000, 100_000, 120_000, 150_000, 200_000}
        or approved["candidate_floor_micros"] != 500_000
        or approved["max_candidates"] != 10
        or approved["min_score_micros"] != config.entity_linking_min_score_micros
        or approved["min_margin_micros"] != config.entity_linking_min_margin_micros
    ):
        raise RuntimeError("[security] entity linking policy thresholds do not match runtime")


def validate_organization_authorization_startup(config: Settings) -> None:
    if config.organization_authorization_enabled and config.allow_public_registration:
        raise RuntimeError(
            "[security] Organization authorization requires public registration to be disabled"
        )


def validate_library_compatibility_startup(config: Settings) -> None:
    if config.cross_library_compatibility_enabled and not config.organization_authorization_enabled:
        raise RuntimeError("[security] Cross-Library compatibility requires Organization authorization")


def validate_federated_retrieval_startup(config: Settings) -> None:
    if config.federated_retrieval_enabled and not (
        config.organization_authorization_enabled and config.cross_library_compatibility_enabled
    ):
        raise RuntimeError(
            "[security] Federated retrieval requires Organization authorization "
            "and cross-Library compatibility"
        )


def validate_personal_library_scopes_startup(config: Settings) -> None:
    if config.personal_library_scopes_enabled and not (
        config.organization_authorization_enabled and config.cross_library_compatibility_enabled
    ):
        raise RuntimeError(
            "[security] Personal Library scopes require Organization authorization "
            "and cross-Library compatibility"
        )


def validate_classification_taxonomy_startup(config: Settings) -> None:
    if config.classification_taxonomy_enabled and not config.organization_authorization_enabled:
        raise RuntimeError("[security] Classification taxonomy requires Organization authorization")


def validate_classification_decision_startup(config: Settings) -> None:
    if config.classification_decision_enabled and not (
        config.organization_authorization_enabled and config.classification_taxonomy_enabled
    ):
        raise RuntimeError(
            "[security] Classification decisions require Organization authorization "
            "and classification taxonomy"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
