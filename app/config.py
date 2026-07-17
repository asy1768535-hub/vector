"""集中配置：pydantic-settings 从 .env 加载，启动时类型校验。"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


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
    rerank_api_key: str = ""        # 留空则复用 embedding_api_key（同一家服务商同 key 时省事）
    rerank_candidate_k: int = 50   # 重排前从 Qdrant 召回多少候选

    # ---- Hybrid 检索（库级 retrieval_mode=hybrid 时生效；轻量第一版，无外部服务）----
    hybrid_candidate_k: int = 50            # dense / keyword 各召回多少候选参与 RRF
    hybrid_rrf_k: int = 60                  # RRF 平滑常数：score = Σ 1/(k+rank)，60 为业界常用默认
    hybrid_keyword_threshold: float = 0.3   # pg_trgm word_similarity 命中门槛（0~1）
    hybrid_keyword_title_boost: float = 1.5      # 文件名/标题命中加权
    hybrid_keyword_external_id_boost: float = 2.0  # 文号/external_id 命中加权（编号类问题更稳）

    # ---- Query Rewrite（多 query dense 召回；第一版，不接 LLM/Agent/Hybrid）----
    # 关：单 query dense 召回（旧逻辑完全不变）。
    # 开：normalize + 同义词/简称扩展出多个 query，分别 embedding+召回，按 chunk_id 合并去重，
    #     再走现有 visibility / source_enrichment / rerank / threshold 流程。
    query_rewrite_enabled: bool = False
    query_rewrite_max_queries: int = 4               # 扩展后最多用几个 query（含原始；规则+LLM 合并后的硬上限）
    query_rewrite_synonyms_path: str = "config/query_synonyms.json"  # 同义词/简称词典

    # ---- LLM Query Rewrite（可选；只生成检索 query，绝不回答/接 Agent/工具）----
    # 开启后：调 OpenAI 兼容 chat 接口把问题改写成多个检索 query，与规则 rewrite 合并。
    # 失败/超时/非法 JSON 一律退回规则 rewrite，绝不阻断检索。
    query_rewrite_llm_enabled: bool = False
    query_rewrite_llm_base_url: str = ""             # 如 http://host:8000/v1 或 .../v1/chat/completions
    query_rewrite_llm_model: str = ""
    query_rewrite_llm_api_key: str = ""              # 本地服务可留空
    query_rewrite_llm_timeout_seconds: float = 8.0
    query_rewrite_llm_max_queries: int = 4           # LLM 单次最多产出几个 query

    # ---- Chat 用户端（轻量问答 v1；默认关；OpenAI 兼容 chat 接口；只生成答案，不接 Agent/工具）----
    # 关闭时 /chat/messages 返回 503。base_url 支持 .../v1 或完整 .../chat/completions；本地模型可不填 key。
    chat_enabled: bool = False
    chat_base_url: str = ""
    chat_model: str = ""
    chat_api_key: str = ""                            # 本地模型可留空（不发 Authorization 头）
    chat_timeout_seconds: float = 30.0
    chat_max_context_chars: int = 12000              # 拼进 prompt 的资料上限，超出截断
    chat_temperature: float = 0.2
    # 多轮上下文：带入最近 N 轮历史帮助理解追问（仅理解，不作事实依据）。0=不带历史。
    chat_history_max_turns: int = 5

    # ---- OCR（图片/扫描件抽文字；默认关，按库 ocr_enabled 覆盖；需装 rapidocr_onnxruntime）----
    ocr_enabled: bool = False

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

    # ---- Auth ----
    jwt_secret: str = Field(default="please-change-me-in-env", min_length=16)
    jwt_lifetime_seconds: int = 60 * 60 * 12  # 12h
    cookie_secure: bool = False  # 生产置 true
    cookie_name: str = "vk_session"
    # 公开注册 /auth/register：内部部门平台默认关闭，只许超管经 /admin/users 建用户。
    allow_public_registration: bool = False

    # ---- 文件导入 ----
    # /import-file 单次上传字节上限（默认 50MiB），超出 413，避免一次性 read 打爆内存。
    max_import_file_bytes: int = 50 * 1024 * 1024
    # 原始上传文件持久化目录（相对路径基于仓库根目录）。
    document_files_dir: str = "storage/document_files"

    # ---- 检索可见性过滤（#6 批次 A，revision 维度）----
    retrieval_consistency_filter: bool = True   # 总开关；关掉则不回查 PG（灰度/回滚用）
    visibility_overfetch_factor: int = 3        # 初始 overfetch 倍数（相对 top_k / 召回数）
    visibility_overfetch_max: int = 200         # 单次召回上限
    visibility_refetch_max_rounds: int = 2      # 过滤后不足时的补召回轮数上限（2~3）
    visibility_total_candidate_cap: int = 500   # 累计候选硬上限
    visibility_latency_budget_ms: int = 800     # 检索补召回的延迟预算（毫秒）
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
    cleanup_worker_batch: int = 16              # 单轮抢多少条 outbox
    cleanup_worker_poll_seconds: float = 2.0    # 空闲轮询间隔
    cleanup_worker_max_attempts: int = 10       # 超过即标 failed（死信）
    cleanup_backoff_base_seconds: float = 5.0   # 指数退避基数
    cleanup_backoff_max_seconds: float = 900.0  # 退避上限（15min）
    cleanup_stale_seconds: int = 600            # processing 超时重置为 pending

    # ---- Heartbeat / 运行状态监控（docs/26）----
    heartbeat_interval_seconds: int = 15     # 各进程心跳写入间隔
    heartbeat_offline_seconds: int = 60      # 超过该秒数未更新即判离线
    heartbeat_prune_seconds: int = 3600      # 超过该秒数未更新即清理过期心跳行

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
    enable_revision_id_worker: bool = False
    enable_revision_id_visibility: bool = False
    enable_sync_source_api: bool = False
    sync_batch_max_items: int = 100
    sync_document_text_max_chars: int = 2_000_000

    # ---- v0.3 Graph Relation Foundation feature flags ----
    graph_v03_enabled: bool = False

    # ---- v0.4 Graph Extraction Pipeline (M1 defaults; fail closed) ----
    graph_extraction_enabled: bool = False
    graph_extraction_auto_trigger_enabled: bool = False
    graph_extraction_base_url: str = "https://api.deepseek.com/v1"
    graph_extraction_model: str = "deepseek-v4-pro"
    graph_extraction_api_key: SecretStr = SecretStr("")
    graph_extraction_timeout_seconds: float = 120.0
    graph_extraction_temperature: float = 0.0
    graph_extraction_response_format: str = "json_object"
    graph_extraction_max_context_chars: int = 24_000
    graph_extraction_previous_chunks: int = 1
    graph_extraction_next_chunks: int = 1

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
    graph_extraction_unit_lease_seconds: int = 180
    graph_extraction_unit_lease_renew_seconds: int = 30
    graph_extraction_worker_max_model_attempts: int = 3

    graph_extraction_context_retention_days: int = 30
    graph_extraction_raw_output_retention_days: int = 30
    graph_extraction_candidate_retention_days: int = 180

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
    graph_retrieval_max_hops: int = 2
    graph_retrieval_max_nodes: int = 100
    graph_retrieval_max_relations: int = 200
    graph_retrieval_max_evidence_per_fact: int = 20
    graph_retrieval_timeout_seconds: float = 3.0


def validate_graph_extraction_startup(config: Settings) -> None:
    weights = {
        "model": config.graph_extraction_weight_model,
        "evidence": config.graph_extraction_weight_evidence,
        "schema": config.graph_extraction_weight_schema,
        "normalization": config.graph_extraction_weight_normalization,
    }
    if any(value < 0 or value > 1 for value in weights.values()):
        raise RuntimeError(
            "[security] graph extraction confidence weight must be within [0, 1]"
        )
    if not math.isclose(math.fsum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise RuntimeError(
            "[security] graph extraction confidence weights must sum to 1.0"
        )

    lease = config.graph_extraction_unit_lease_seconds
    renew = config.graph_extraction_unit_lease_renew_seconds
    if renew >= lease / 2:
        raise RuntimeError(
            "[security] graph extraction lease renew must be less than half the lease"
        )

    retention_values = (
        config.graph_extraction_context_retention_days,
        config.graph_extraction_raw_output_retention_days,
        config.graph_extraction_candidate_retention_days,
    )
    if any(value <= 0 for value in retention_values):
        raise RuntimeError("[security] graph extraction retention days must be positive")

    if config.graph_extraction_timeout_seconds >= lease:
        raise RuntimeError(
            "[security] graph extraction provider timeout must be below the Unit lease"
        )

    if config.graph_extraction_enabled and (
        config.graph_extraction_base_url != "https://api.deepseek.com/v1"
        or config.graph_extraction_model != "deepseek-v4-pro"
    ):
        raise RuntimeError(
            "[security] graph extraction requires the frozen official DeepSeek "
            "provider configuration"
        )

    if (
        config.graph_extraction_auto_trigger_enabled
        and not config.graph_extraction_api_key.get_secret_value().strip()
    ):
        raise RuntimeError(
            "[security] GRAPH_EXTRACTION_API_KEY is required when auto trigger is enabled"
        )


def validate_graph_publication_startup(config: Settings) -> None:
    confidence_values = (
        config.graph_publication_extracted_entity_min_confidence,
        config.graph_publication_extracted_relation_min_confidence,
    )
    if any(value < 0 or value > 1 for value in confidence_values):
        raise RuntimeError(
            "[security] graph publication confidence threshold must be within [0, 1]"
        )
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
        ("max hops", config.graph_retrieval_max_hops, 2),
        ("max nodes", config.graph_retrieval_max_nodes, 100),
        ("max relations", config.graph_retrieval_max_relations, 200),
        ("max Evidence per fact", config.graph_retrieval_max_evidence_per_fact, 20),
    )
    for label, value, absolute_cap in limits:
        if value <= 0 or value > absolute_cap:
            raise RuntimeError(
                f"[security] graph retrieval {label} must be within [1, {absolute_cap}]"
            )
    if config.graph_retrieval_max_nodes < config.graph_retrieval_max_seeds:
        raise RuntimeError("[security] graph retrieval max nodes must cover every seed")
    if (
        not math.isfinite(config.graph_retrieval_timeout_seconds)
        or config.graph_retrieval_timeout_seconds <= 0
    ):
        raise RuntimeError("[security] graph retrieval timeout must be finite and positive")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
