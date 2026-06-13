from contextvars import ContextVar

# MCP 工具函数拿不到 FastAPI 的依赖注入，user_id 由鉴权中间件按请求写入 contextvar。
# 默认值与 REST 层 get_user_id 的行为一致：不传 X-User-Id 时按 default 用户处理。
current_user_id: ContextVar[str] = ContextVar("current_user_id", default="default")
