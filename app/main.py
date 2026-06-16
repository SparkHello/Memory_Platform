from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.deprecated_v1 import router as deprecated_v1_router
from app.api.health import router as health_router
from app.api.memories import router as memories_router
from app.config import get_settings
from app.mcp_server.auth import MCPAuthMiddleware
from app.mcp_server.server import create_mcp_server
from app.memory.store import MemoryStore


UI_DIST_DIR = Path(__file__).resolve().parent.parent / "ui" / "dist"


class UTF8JSONResponse(JSONResponse):
    # Windows PowerShell 5.1 等旧客户端在 Content-Type 缺少 charset 时按 ISO-8859-1 解码，中文会乱码
    media_type = "application/json; charset=utf-8"


def create_app() -> FastAPI:
    """应用工厂。MCP 的 session manager 每个实例只能启动一次，
    应用每次构建（含测试反复启动）都需要全新的 FastAPI + FastMCP 实例。"""
    mcp = create_mcp_server()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = get_settings()
        MemoryStore(settings.database_path).init_db()
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
    app.include_router(deprecated_v1_router)
    app.include_router(memories_router)

    @app.get("/ui", include_in_schema=False)
    def redirect_ui_root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")

    app.mount(
        "/ui",
        StaticFiles(directory=UI_DIST_DIR, html=True, check_dir=False),
        name="memory-console",
    )
    # MCP streamable HTTP 子应用兜底挂载在根路径，实际端点是 /mcp（FastAPI 自有路由优先匹配）
    app.mount("/", MCPAuthMiddleware(mcp.streamable_http_app()))
    return app


app = create_app()
