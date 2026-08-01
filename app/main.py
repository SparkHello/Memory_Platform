from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from app.api.chat_gateway import router as chat_gateway_router
from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.memories import router as memories_router
from app.api.usage import router as usage_router
from app.config import get_settings
from app.knowledge.store import KnowledgeStore
from app.mcp_server.auth import MCPAuthMiddleware
from app.mcp_server.server import create_mcp_server
from app.memory.store import MemoryStore
from app.usage.store import UsageStore


UI_DIST_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"
logger = logging.getLogger(__name__)


class UTF8JSONResponse(JSONResponse):
    # Windows PowerShell 5.1 等旧客户端在 Content-Type 缺少 charset 时按 ISO-8859-1 解码，中文会乱码
    media_type = "application/json; charset=utf-8"


class UIStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if Path(path).suffix or path.startswith("assets/"):
                raise
            return await super().get_response("index.html", scope)


def create_app() -> FastAPI:
    """应用工厂。MCP 的 session manager 每个实例只能启动一次，
    应用每次构建（含测试反复启动）都需要全新的 FastAPI + FastMCP 实例。"""
    mcp = create_mcp_server()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        _validate_database_paths(settings.database_path, settings.knowledge_database_path)
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
        version="0.1.0",
        lifespan=lifespan,
        default_response_class=UTF8JSONResponse,
    )
    app.include_router(health_router)
    app.include_router(chat_gateway_router)
    app.include_router(memories_router)
    app.include_router(knowledge_router)
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
        UIStaticFiles(directory=UI_DIST_DIR, html=True, check_dir=False),
        name="memory-console",
    )
    # MCP streamable HTTP 子应用兜底挂载在根路径，实际端点是 /mcp（FastAPI 自有路由优先匹配）
    app.mount("/", MCPAuthMiddleware(mcp.streamable_http_app()))
    return app


def _validate_database_paths(memory_path: str, knowledge_path: str) -> None:
    """Knowledge and memory are deliberately separate failure/security domains."""
    memory = Path(memory_path).expanduser().resolve()
    knowledge = Path(knowledge_path).expanduser().resolve()
    same_file = memory == knowledge
    if not same_file and memory.exists() and knowledge.exists():
        same_file = memory.samefile(knowledge)
    if same_file:
        raise RuntimeError("KNOWLEDGE_DATABASE_PATH 不能与 DATABASE_PATH 指向同一个文件")


app = create_app()
