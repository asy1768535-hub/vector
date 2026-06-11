"""集中配置：pydantic-settings 从 .env 加载，启动时类型校验。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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

    # ---- Qdrant ----
    qdrant_url: str = "http://10.0.10.2:6333"
    qdrant_api_key: str = ""

    # ---- API ----
    api_host: str = "0.0.0.0"
    api_port: int = 8100
    app_debug: bool = False

    # ---- Auth ----
    jwt_secret: str = Field(default="please-change-me-in-env", min_length=16)
    jwt_lifetime_seconds: int = 60 * 60 * 12  # 12h
    cookie_secure: bool = False  # 生产置 true
    cookie_name: str = "vk_session"

    # ---- Worker ----
    embed_batch_size: int = 32
    embed_worker_batch_docs: int = 8
    embed_worker_poll_seconds: float = 1.0
    embed_worker_stale_seconds: int = 3600
    embed_worker_max_attempts: int = 5

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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
