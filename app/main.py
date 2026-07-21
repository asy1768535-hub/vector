"""FastAPI 入口。

挂载：
  - /health
  - /auth/jwt/login, /auth/register, /auth/forgot-password, /users/me ...（fastapi-users）
  - /retrieval, /libraries/*, /me/*, /admin/* ...（业务路由）
  - /console/  管理后台 SPA（StaticFiles）
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from app.api.admin_audit import router as admin_audit_router
from app.api.admin_chat_logs import router as admin_chat_logs_router
from app.api.admin_jobs import router as admin_jobs_router
from app.api.admin_operations import router as admin_operations_router
from app.api.admin_libraries import router as admin_libraries_router
from app.api.admin_permissions import router as admin_permissions_router
from app.api.admin_users import router as admin_users_router
from app.api.api_keys import router as api_keys_router
from app.api.chat import router as chat_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.me import router as me_router
from app.api.organizations import router as organizations_router
from app.api.retrieval import router as retrieval_router
from app.api.v02_m4 import router as v02_m4_router
from app.api.v03_graph import router as v03_graph_router
from app.api.v04_graph_extraction import router as v04_graph_extraction_router
from app.api.v05_graph_publications import router as v05_graph_publications_router
from app.api.v06_graph_retrieval import router as v06_graph_retrieval_router
from app.api.v07_entity_linking import router as v07_entity_linking_router
from app.auth.routes import build_auth_router
from app.casbin.enforcer import get_enforcer
from app.config import (
    settings,
    validate_revision_file_storage_startup,
    validate_revision_cleanup_startup,
    validate_revision_coordinated_purge_startup,
    validate_revision_retention_startup,
    validate_entity_linking_startup,
    validate_graph_extraction_startup,
    validate_knowledge_artifact_startup,
    validate_organization_authorization_startup,
    validate_graph_publication_startup,
    validate_graph_retrieval_startup,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


class NoCacheStaticFiles(StaticFiles):
    """零构建 SPA：给静态文件加 no-cache 头，避免浏览器缓存旧 JS。

    admin-ui 是直接编辑即生效的源码（无打包/无 hash 文件名），默认 StaticFiles
    的条件缓存会让浏览器一直用旧版本。这里强制每次都重新校验。
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


_DEFAULT_JWT_SECRET = "please-change-me-in-env"


def assert_startup_security() -> None:
    """JWT 加固：仍用默认密钥 → 拒绝启动（避免误部署后令牌可被伪造）。

    debug 模式（APP_DEBUG=true）降级为告警，方便本地开发不配 .env 也能起。
    生产（默认 app_debug=False）直接抛 RuntimeError 中止启动。
    """
    if settings.jwt_secret == _DEFAULT_JWT_SECRET:
        msg = ("JWT_SECRET 仍是默认值，存在令牌伪造风险；请在 .env 配置强随机密钥"
               "（如 `python -c \"import secrets;print(secrets.token_urlsafe(48))\"`）。")
        if settings.app_debug:
            log.warning("[安全] %s（debug 模式放行）", msg)
        else:
            raise RuntimeError(f"[安全] 拒绝启动：{msg}")


def assert_graph_extraction_startup_security() -> None:
    validate_graph_extraction_startup(settings)


def assert_knowledge_artifact_startup_security() -> None:
    validate_knowledge_artifact_startup(settings)


def assert_revision_file_storage_startup_security() -> None:
    validate_revision_file_storage_startup(settings)
    if (
        settings.revision_file_storage_enabled
        and settings.document_storage_provider == "local"
    ):
        from app.services.object_storage import build_object_storage_adapter
        from app.services.object_storage_contracts import ObjectStorageError

        try:
            build_object_storage_adapter(settings)
        except ObjectStorageError:
            raise RuntimeError(
                "[security] local document storage root is invalid"
            ) from None


def assert_revision_retention_startup_security() -> None:
    validate_revision_retention_startup(settings)
    validate_revision_cleanup_startup(settings)
    validate_revision_coordinated_purge_startup(settings)


def assert_graph_publication_startup_security() -> None:
    validate_graph_publication_startup(settings)


def assert_graph_retrieval_startup_security() -> None:
    validate_graph_retrieval_startup(settings)


def assert_entity_linking_startup_security() -> None:
    validate_entity_linking_startup(settings)


def assert_organization_authorization_startup_security() -> None:
    validate_organization_authorization_startup(settings)


def resolve_console_ui_dir(root: Path) -> Path | None:
    """Resolve the configured console UI directory without implicit frontend fallback."""
    configured = Path(settings.console_ui_dir)
    ui_dir = configured if configured.is_absolute() else root / configured
    return ui_dir if ui_dir.is_dir() else None


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """FastAPI lifespan：startup → yield → shutdown。"""
    # ── startup ──────────────────────────────────────────────
    # 安全前置校验：默认密钥等危险配置在启动时就拦下
    assert_startup_security()
    assert_graph_extraction_startup_security()
    assert_knowledge_artifact_startup_security()
    assert_revision_file_storage_startup_security()
    assert_revision_retention_startup_security()
    assert_graph_publication_startup_security()
    assert_graph_retrieval_startup_security()
    assert_entity_linking_startup_security()
    assert_organization_authorization_startup_security()
    # 预热 Casbin enforcer，确保 policy 已加载入内存
    get_enforcer()
    # 启动自检：embedding 服务 / Qdrant 配置错配时大声报（非 fatal，不阻断启动）
    from app.services import selfcheck
    await selfcheck.run_startup_check("API")
    # 运行状态心跳：独立后台任务（docs/26）；写失败只记日志，不影响 API 请求处理
    from app.services import heartbeat
    hb_stop = asyncio.Event()
    hb_instance = heartbeat.make_instance_id()
    hb_task = asyncio.create_task(heartbeat.heartbeat_loop(
        "api", hb_instance,
        hostname=heartbeat.HOSTNAME, pid=heartbeat.PID, started_at=heartbeat.STARTED_AT,
        stop_event=hb_stop, also_prune=True,
    ))
    log.info("API started on %s:%s", settings.api_host, settings.api_port)
    yield
    # ── shutdown ─────────────────────────────────────────────
    # 停心跳：置位 → 写一次 stopping → 取消任务（失败均忽略，不阻断关闭）
    hb_stop.set()
    await heartbeat.beat("api", hb_instance, hostname=heartbeat.HOSTNAME, pid=heartbeat.PID,
                         started_at=heartbeat.STARTED_AT, status="stopping")
    hb_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hb_task
    # 优雅关闭跨库补全的 asyncpg 连接池
    from app.services import source_enrichment
    await source_enrichment.close_all_pools()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vector Knowledge Base",
        version="0.1.0",
        description="Multi-tenant vector DB with Dify-compatible retrieval API.",
        debug=settings.app_debug,
        lifespan=lifespan,
    )

    app.include_router(health_router, tags=["health"])
    app.include_router(build_auth_router())
    app.include_router(api_keys_router)
    app.include_router(me_router)
    app.include_router(organizations_router)
    app.include_router(documents_router)
    app.include_router(v02_m4_router)
    app.include_router(v03_graph_router)
    app.include_router(v04_graph_extraction_router)
    app.include_router(v05_graph_publications_router)
    app.include_router(v06_graph_retrieval_router)
    app.include_router(v07_entity_linking_router)
    app.include_router(retrieval_router)
    app.include_router(chat_router)
    app.include_router(admin_users_router)
    app.include_router(admin_libraries_router)
    app.include_router(admin_permissions_router)
    app.include_router(admin_audit_router)
    app.include_router(admin_chat_logs_router)
    app.include_router(admin_jobs_router)
    app.include_router(admin_operations_router)

    # 浏览器/插件会自动探测热重载 WebSocket，静默关闭避免刷屏日志
    @app.websocket("/ws/live")
    async def _ws_live_stub(ws: WebSocket) -> None:
        await ws.accept()          # 先完成握手（HTTP 101），否则仍返回 403
        await ws.close(code=1001)  # 1001 Going Away，立即关闭

    # 管理后台 SPA：挂到 /console/，避免与 /admin/* API 命名空间冲突。
    # 默认固定使用零构建 admin-ui；备用构建产物需通过 CONSOLE_UI_DIR 显式指定。
    root = Path(__file__).resolve().parent.parent
    ui_dir = resolve_console_ui_dir(root)
    if ui_dir is not None:
        app.mount("/console", NoCacheStaticFiles(directory=str(ui_dir), html=True), name="console")

        @app.get("/", include_in_schema=False)
        async def _root_redirect() -> RedirectResponse:
            return RedirectResponse(url="/console/")

    return app


app = create_app()


def run() -> None:
    """以 settings 里（.env 来源）的 host / port 启动 uvicorn。

    用法：
        python -m app.main        # 读 .env，等同于 uvicorn app.main:app --host $API_HOST --port $API_PORT
    """
    import uvicorn  # 局部 import 避免单元测试 import app.main 时强依赖 uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.app_debug,
        log_level="debug" if settings.app_debug else "info",
    )


if __name__ == "__main__":
    run()
