from contextlib import asynccontextmanager
import logging
from pathlib import Path, PurePosixPath

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from app.api.auth_tokens import router as auth_tokens_router
from app.api.chat_gateway import router as chat_gateway_router
from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.memories import router as memories_router
from app.api.providers import router as providers_router
from app.api.usage import router as usage_router
from app.auth.middleware import EarlyAuthMiddleware
from app.auth.tokens import AuthTokenStore
from app.cli_config import cli_paths
from app.config import get_settings
from app.disk_capacity import DiskCapacityMiddleware
from app.knowledge.store import KnowledgeStore
from app.mcp_server.auth import MCPAuthMiddleware
from app.mcp_server.server import create_mcp_server
from app.memory.store import MemoryStore
from app.model_catalog import validate_catalog_and_routes
from app.request_limits import (
    RequestTargetLimitMiddleware,
    RouteAwareRequestBodyLimitMiddleware,
    initialize_request_spool_directories,
)
from app.security_headers import SecurityHeadersMiddleware
from app.stack_backup import assert_no_interrupted_stack_restore
from app.usage.pricing import configure_pricing_catalog
from app.usage.store import UsageStore


UI_DIST_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"
_UI_ROOT_STATIC_FILES = frozenset(
    {
        "index.html",
        "backdrop.svg",
        "backdrop.jpg",
        "backdrop-credit.txt",
    }
)
logger = logging.getLogger(__name__)


def _resolve_ui_dist_dir(settings) -> Path:
    configured = settings.ui_dist_dir.strip()
    if configured:
        resolved = Path(configured).expanduser().resolve()
        if not (
            resolved.is_dir()
            and (resolved / "index.html").is_file()
            and (resolved / "assets").is_dir()
        ):
            raise RuntimeError(
                "UI_DIST_DIR 必须指向包含 index.html 和 assets/ 的专用 UI 构建目录"
            )
        return resolved
    return UI_DIST_DIR


def _normalized_ui_request_path(path: str) -> str | None:
    candidate = PurePosixPath(path)
    if any(part == ".." or part.startswith(".") for part in candidate.parts):
        return None
    normalized = candidate.as_posix()
    return "" if normalized == "." else normalized


class UTF8JSONResponse(JSONResponse):
    # Windows PowerShell 5.1 等旧客户端在 Content-Type 缺少 charset 时按 ISO-8859-1 解码，中文会乱码
    media_type = "application/json; charset=utf-8"


class UIStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope):
        normalized = _normalized_ui_request_path(path)
        if normalized is None:
            raise StarletteHTTPException(status_code=404)
        is_static_file = normalized in _UI_ROOT_STATIC_FILES or normalized.startswith(
            "assets/"
        )
        if not is_static_file:
            if PurePosixPath(normalized).suffix:
                raise StarletteHTTPException(status_code=404)
            return await super().get_response("index.html", scope)
        return await super().get_response(normalized, scope)


def create_app() -> FastAPI:
    """应用工厂。MCP 的 session manager 每个实例只能启动一次，
    应用每次构建（含测试反复启动）都需要全新的 FastAPI + FastMCP 实例。"""
    mcp = create_mcp_server()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        assert_no_interrupted_stack_restore(cli_paths().home)
        validate_catalog_and_routes(
            catalog_path=settings.model_catalog_path,
            routes_path=settings.model_routes_path,
        )
        configure_pricing_catalog(settings.pricing_catalog_path)
        _validate_database_paths(
            settings.database_path,
            settings.knowledge_database_path,
            settings.auth_database_path,
        )
        initialize_request_spool_directories(settings)
        AuthTokenStore(settings.auth_database_path).init_db()
        MemoryStore(settings.database_path).init_db()
        UsageStore(settings.database_path).init_db()
        app.state.knowledge_init_error = ""
        try:
            KnowledgeStore(
                settings.knowledge_database_path,
                max_document_bytes=settings.knowledge_max_document_bytes,
            ).init_db()
        except Exception as exc:  # knowledge failure must not take memory/MCP offline
            app.state.knowledge_init_error = str(exc)
            logger.exception("知识库初始化失败；长期记忆服务将继续启动。")
        # mount 的子应用不会被 FastAPI 触发 lifespan，MCP 的 session manager 在这里启动
        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title="memory-gateway",
        version="0.2.0",
        lifespan=lifespan,
        default_response_class=UTF8JSONResponse,
    )
    app.add_middleware(RouteAwareRequestBodyLimitMiddleware)
    app.add_middleware(RequestTargetLimitMiddleware)
    # Starlette executes the most recently added middleware first. Auth is
    # therefore outside body buffering, but inside the response hardening layer.
    app.add_middleware(EarlyAuthMiddleware)
    # Storage exhaustion from body spooling, auth bookkeeping and endpoint
    # transactions shares one safe 507 contract.
    app.add_middleware(DiskCapacityMiddleware)
    # Security headers remain the outermost response wrapper, including 507s.
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(health_router)
    app.include_router(chat_gateway_router)
    app.include_router(auth_tokens_router)
    app.include_router(memories_router)
    app.include_router(knowledge_router)
    app.include_router(providers_router)
    app.include_router(usage_router)

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    @app.get("/studio", include_in_schema=False)
    @app.get("/memory-studio", include_in_schema=False)
    @app.get("/记忆工作室", include_in_schema=False)
    def redirect_to_ui() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    @app.get("/ui", include_in_schema=False)
    def redirect_ui_root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    app.mount(
        "/ui",
        UIStaticFiles(
            directory=_resolve_ui_dist_dir(get_settings()),
            html=True,
            check_dir=False,
        ),
        name="memory-console",
    )
    # MCP streamable HTTP 子应用兜底挂载在根路径，实际端点是 /mcp（FastAPI 自有路由优先匹配）
    app.mount("/", MCPAuthMiddleware(mcp.streamable_http_app()))
    return app


def _validate_database_paths(
    memory_path: str,
    knowledge_path: str,
    auth_path: str | None = None,
) -> None:
    """Memory, knowledge and auth remain separate failure/security domains."""

    paths = {
        "DATABASE_PATH": Path(memory_path).expanduser().resolve(),
        "KNOWLEDGE_DATABASE_PATH": Path(knowledge_path).expanduser().resolve(),
    }
    if auth_path is not None:
        paths["AUTH_DATABASE_PATH"] = Path(auth_path).expanduser().resolve()
    items = list(paths.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1 :]:
            same_file = left == right
            if not same_file and left.exists() and right.exists():
                same_file = left.samefile(right)
            if same_file:
                raise RuntimeError(f"{right_name} 不能与 {left_name} 指向同一个文件")


app = create_app()
