# AGENTS.md

面向后续 Codex / AI agent 的项目说明。修改代码前先读本文件和 `README.md`，再根据任务读取相关模块。

## 项目背景

`memory-gateway` 是一个长期记忆服务，主要给 Kelivo 这类 iOS AI 客户端使用。它把用户长期信息保存到 SQLite，并通过两种方式提供给模型：

- MCP 模式：客户端直连模型，模型通过 `/mcp` 的工具主动检索、保存、整理、删除记忆。
- OpenAI-compatible 网关模式：客户端请求 `/v1/chat/completions`，服务端自动注入记忆、调用上游模型，并在响应后后台提取新记忆。

项目目标不是“尽量多记”，而是“不乱记”：只保存长期有用、用户明确表达过、未来回答可能用到的信息。

## 技术栈

- Python 3.12
- FastAPI
- Uvicorn
- Pydantic v2 / pydantic-settings
- httpx
- MCP Python SDK / FastMCP
- SQLite
- pytest / pytest-asyncio

## 常用命令

安装开发依赖：

```powershell
cd C:\Users\spari\Documents\Memory\memory-gateway
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

启动开发服务：

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```powershell
curl http://localhost:8000/health
```

运行测试：

```powershell
pytest
```

常用定向测试：

```powershell
pytest tests/test_mcp_server.py
pytest tests/test_chat_gateway.py
pytest tests/test_memory_extraction.py
pytest tests/test_memory_store.py
```

查看 LAN / Tailscale 地址：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1 -Port 8000
```

Windows 服务脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
powershell -ExecutionPolicy Bypass -File scripts\uninstall-service.ps1
```

## 代码风格

- 保持现有简洁 Python 风格，优先使用小函数和显式数据模型。
- API schema 使用 Pydantic model。
- 路由依赖放在 `app/api/deps.py`，不要在每个路由里重复解析配置。
- 记忆相关字段尽量先写入 `app/memory/models.py`，再扩展 store/API/MCP/tests。
- 数据库 schema 变更要在 `MemoryStore.init_db()` 中兼容旧库，参考 `_ensure_*` 方法。
- 不要让后台记忆提取失败影响聊天接口本身。
- REST 和 MCP 的行为应尽量一致，尤其是鉴权、用户隔离、保存门槛和返回字段。
- 中文内容要用 UTF-8；响应 JSON 应保持 `application/json; charset=utf-8`。
- 测试里使用 fake LLM 和临时数据库，不依赖外部网络服务。

## 重要文件说明

- `app/main.py`：应用工厂。创建 FastAPI app，初始化 SQLite，启动 MCP session manager，挂载 `/mcp`。
- `app/config.py`：配置入口。读取 `.env`，包含上游 chat、embedding、数据库和超时配置。
- `app/api/deps.py`：REST 鉴权、`X-User-Id`、MemoryStore、LLM client 和 embedding client 依赖。
- `app/api/memories.py`：记忆管理 REST API，包括列表、搜索、删除、恢复、导出、报告、合并、核心记忆和决策日志。
- `app/openai_compat/chat.py`：OpenAI-compatible 聊天接口。负责检索记忆、注入上下文、调用上游、清理响应、后台提取记忆和更新近期摘要。
- `app/openai_compat/schemas.py`：聊天请求模型。
- `app/openai_compat/streaming.py`：`stream=true` 当前返回 501。
- `app/llm/client.py`：OpenAI-compatible chat client。
- `app/llm/prompts.py`：记忆注入、记忆提取和核心记忆整理 prompt。
- `app/mcp_server/auth.py`：MCP 子应用鉴权。MCP 不经过 FastAPI 依赖，所以在 ASGI middleware 里校验 Bearer token。
- `app/mcp_server/context.py`：用 contextvar 保存当前 MCP 请求的 user id。
- `app/mcp_server/server.py`：FastMCP server、instructions 和全部 MCP 工具。
- `app/memory/models.py`：记忆、核心记忆、候选记忆、体检建议、决策日志等 Pydantic 模型。
- `app/memory/store.py`：SQLite 表结构、兼容迁移、CRUD、软删除、恢复、合并、导入、核心记忆版本历史、近期摘要和决策日志。
- `app/memory/search.py`：embedding 搜索、关键词 fallback、排序衰减、敏感降权和使用统计。
- `app/memory/extractor.py`：LLM 记忆提取和保存门槛校验。
- `app/memory/resolver.py`：判断候选记忆应创建、更新旧记忆还是忽略。
- `app/memory/core.py`：核心记忆整理。只从已保存长期记忆中提炼，并要求 evidence ids。
- `app/memory/review.py`：记忆体检建议，不直接修改数据。
- `app/memory/report.py`：记忆报告、导出和恢复导入。
- `tests/`：pytest 测试，覆盖 REST、MCP、存储、搜索、核心记忆、编码和配置。
- `scripts/`：Windows PowerShell 辅助脚本。

## 已知限制

- 网关模式 `stream=true` 未实现，会返回 501。
- 网关模式每轮回答后会额外调用一次上游模型做记忆提取。
- MCP 模式依赖模型主动调用工具，效果受客户端系统提示词影响。
- embedding 存在 SQLite JSON 字段中，没有向量数据库或向量索引。
- 导出不会包含 embedding，迁移后需要重新生成。
- Windows 服务脚本包含本机绝对路径和固定端口 2026。

## 不要随便修改的地方

- 不要改 `.env`，里面可能有真实密钥。
- 不要删除、覆盖或手工编辑 `data/memory.db`，这是用户真实记忆数据。
- 不要删除 `logs/` 里的日志，除非用户明确要求。
- 不要改 `.venv/`、`.pytest_cache/`、`__pycache__/`、`memory_gateway.egg-info/`。
- 不要把真实 API key、Tailscale 地址或私人路径写进公开文档。
- 不要随意改变记忆保存门槛，尤其是敏感信息、假设场景和 `source_quote` 校验。
- 不要把 MCP 的 DNS rebinding 设置改回默认，当前服务需要被局域网/iPhone 访问，鉴权由 `MCPAuthMiddleware` 负责。
- 不要把软删除改成硬删除，除非用户明确要求设计变更。
- 不要让记忆提取、核心记忆整理或 embedding 失败阻断正常聊天响应。

## 后续开发注意事项

- 新增记忆字段时，同步更新 `models.py`、`store.py`、REST 返回、MCP 返回、导出恢复和测试。
- 新增 MCP 工具时，同步更新 `EXPECTED_TOOLS` 相关测试和 README。
- 修改 REST 鉴权时，同步检查 MCP 鉴权，因为 MCP 子应用不走 FastAPI dependency。
- 修改搜索排序时，重点跑 `tests/test_memory_search.py` 和 MCP 搜索相关测试。
- 修改保存门槛时，重点跑 `tests/test_memory_extraction.py` 和 `tests/test_mcp_server.py`。
- 修改核心记忆整理时，重点跑 `tests/test_core_memory.py`。
- 修改响应格式时，确保 `tests/test_response_charset.py` 和聊天网关测试仍通过。
- 测试应继续使用 fake LLM，不要引入真实网络调用。
- 当前仓库可能存在用户未提交改动。修改前先看 `git status --short`，不要回滚用户改动。
