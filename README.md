# memory-gateway

`memory-gateway` 是一个给 AI 客户端提供长期记忆能力的本地服务。它主要面向 Kelivo 这类 iOS AI 客户端，也可以被任何支持 OpenAI-compatible API 或 MCP Streamable HTTP 的客户端接入。

项目提供两种模式：

- MCP 模式：`POST /mcp`，暴露记忆工具，由模型主动调用工具完成检索、保存、整理和删除。
- 网关模式：`POST /v1/chat/completions`，兼容 OpenAI Chat Completions API，服务端自动注入记忆、调用上游模型，并在回答后后台提取新记忆。

两种模式共享同一个 SQLite 记忆库和同一套保存门槛。

## 技术栈

- Python 3.12
- FastAPI
- Uvicorn
- Pydantic / pydantic-settings
- httpx
- MCP Python SDK / FastMCP
- SQLite
- pytest / pytest-asyncio

## 主要功能

- MCP Streamable HTTP 服务，端点为 `/mcp`。
- OpenAI-compatible 聊天网关，端点为 `/v1/chat/completions`。
- 长期记忆的保存、检索、去重、更新、软删除和恢复。
- 核心记忆整理：从长期记忆中整理少量稳定背景。
- 近期会话摘要：网关模式会按 `conversation_id` 保存短期上下文摘要。
- 记忆体检：找出重复、过期、需要复核或敏感的记忆，只返回建议，不自动修改数据。
- 记忆报告、导出和恢复导入。
- embedding 搜索优先，未配置 embedding 或调用失败时退回关键词搜索。
- 多用户隔离：通过 `X-User-Id` 请求头区分用户，不传时使用 `default`。
- UTF-8 JSON 响应，兼容 Windows PowerShell 5.1 等旧客户端。

## 快速开始

需要 Python 3.12。

```powershell
cd C:\Users\spari\Documents\Memory\memory-gateway
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

macOS / Linux 激活虚拟环境：

```bash
source .venv/bin/activate
```

复制配置文件：

```powershell
copy .env.example .env
```

编辑 `.env`：

```env
GATEWAY_API_KEY=change-me

UPSTREAM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
UPSTREAM_API_KEY=your-zhipu-api-key
UPSTREAM_MODEL=glm-5.1

EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=your-dashscope-api-key
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024

DATABASE_PATH=data/memory.db
REQUEST_TIMEOUT_SECONDS=60
```

启动开发服务：

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```powershell
curl http://localhost:8000/health
```

## 配置项

| 变量 | 说明 |
| --- | --- |
| `GATEWAY_API_KEY` | 客户端访问本服务的 Bearer token。 |
| `UPSTREAM_BASE_URL` | 聊天模型的 OpenAI-compatible base URL。 |
| `UPSTREAM_API_KEY` | 聊天模型 API key。 |
| `UPSTREAM_MODEL` | 实际调用的聊天模型。客户端传入的 model 会被映射到这里。 |
| `EMBEDDING_BASE_URL` | embedding 服务的 OpenAI-compatible base URL。 |
| `EMBEDDING_API_KEY` | embedding API key。留空时使用关键词搜索 fallback。 |
| `EMBEDDING_MODEL` | embedding 模型名。 |
| `EMBEDDING_DIMENSIONS` | embedding 维度。 |
| `DATABASE_PATH` | SQLite 数据库路径，默认 `data/memory.db`。 |
| `REQUEST_TIMEOUT_SECONDS` | 调用上游模型和 embedding 服务的超时时间，默认 60 秒。 |

不要提交真实 `.env`。`.gitignore` 已忽略 `.env`、`data/*.db`、`logs/`、`.venv/` 等本地文件。

## 运行方式

### 开发模式

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

本地访问：

- Health: `http://localhost:8000/health`
- MCP: `http://localhost:8000/mcp`
- OpenAI-compatible Base URL: `http://localhost:8000/v1`

### Windows 服务模式

脚本位于 `scripts/`，使用 NSSM 注册 Windows 服务。当前脚本里硬编码了：

- NSSM 路径：`C:\Users\spari\Tools\nssm.exe`
- 项目路径：`C:\Users\spari\Documents\Memory\memory-gateway`
- 服务端口：`2026`

安装服务需要管理员 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
```

卸载服务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall-service.ps1
```

查看 LAN / Tailscale 访问地址：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1
```

如果开发服务跑在 8000 端口：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1 -Port 8000
```

## OpenAI-compatible 网关模式

客户端配置：

- Base URL: `http://<host>:8000/v1`
- API Key: `.env` 中的 `GATEWAY_API_KEY`
- Model: 任意，服务端会改用 `UPSTREAM_MODEL`

请求示例：

```powershell
$headers = @{
  Authorization = "Bearer change-me"
  "Content-Type" = "application/json; charset=utf-8"
}

$body = @{
  model = "any-model"
  messages = @(
    @{ role = "user"; content = "我喜欢黑咖啡，请记住。" }
  )
  temperature = 0.7
  conversation_id = "optional-conversation-id"
} | ConvertTo-Json -Depth 10

$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/v1/chat/completions" `
  -Headers $headers `
  -Body $bytes
```

网关模式行为：

- 每次请求先检索当前用户的相关长期记忆。
- 优先注入核心记忆，再注入近期会话摘要和普通长期记忆。
- 调用上游 chat completions。
- 返回前会移除 `reasoning_content`、`tool_calls` 等不适合透传给普通客户端的字段。
- 回答完成后后台提取新记忆，不阻塞聊天响应。
- `stream=true` 当前返回 501，未实现流式转发。

## MCP 模式

MCP 端点：

```text
http://<host>:8000/mcp
```

请求头：

```http
Authorization: Bearer <GATEWAY_API_KEY>
X-User-Id: optional-user-id
```

传输方式：

- Streamable HTTP
- stateless
- JSON response

当前 MCP 工具：

| 工具 | 说明 |
| --- | --- |
| `search_memory` | 按主题检索长期记忆，会更新 `usage_count` 和 `last_used_at`。 |
| `save_memory` | 保存长期记忆，服务端会做门槛校验、去重和同主题更新。 |
| `why_remember` | 解释某条记忆的来源、保存时间、置信度和核心记忆引用情况。 |
| `merge_memories` | 合并多条同主题记忆，保留第一条，软删除其余条目。 |
| `get_recent_context_summary` | 读取近期会话摘要。 |
| `get_core_memory` | 读取核心记忆。 |
| `get_core_memory_history` | 查看核心记忆历史版本。 |
| `consolidate_core_memory` | 调用上游模型整理核心记忆。 |
| `review_memories` | 体检记忆库，返回建议，不自动修改数据。 |
| `memory_report` | 生成当前用户的记忆报告，支持 markdown/json。 |
| `export_memories` | 导出当前用户记忆，embedding 不会导出。 |
| `list_memories` | 列出当前用户的长期记忆。 |
| `list_deleted_memories` | 列出软删除记忆。 |
| `delete_memory` | 按 id 软删除记忆。 |
| `restore_memory` | 按 id 恢复软删除记忆。 |
| `forget_memories` | 按自然语言主题搜索并软删除相关记忆。 |

MCP 模式依赖模型主动调用工具。建议在客户端系统提示词里明确：

- 与用户个人有关的问题先调用 `search_memory`。
- 用户自然流露长期信息时调用 `save_memory`，不必等用户说“记住”。
- 检索旧记忆和保存新记忆是两个独立判断，可以同一轮都发生。
- 用户要求忘记一类信息时优先调用 `forget_memories`。
- 用户要求检查或清理记忆时调用 `review_memories`，根据用户确认再删除或合并。

服务端也会通过 MCP `instructions` 下发简版规则。

## 记忆保存规则

目标是“不乱记”。只有长期有用、用户明确表达过、未来回答可能用到的信息才保存。

硬性门槛：

- `action` 必须是 `create` 或 `update`。
- `importance >= 6`。
- `confidence >= 0.8`。
- `memory` 非空。
- `source_quote` 非空。
- 网关模式下，`source_quote` 必须是用户原话片段。
- 命中假设表达时不保存，例如“如果”“假如”“假设”“suppose”“if I use”“imagine”“let's say”。
- `private` / `sensitive` 记忆要求 `importance >= 8`、`confidence >= 0.9`，且用户明确要求记住。

记忆类型：

- `project`
- `preference`
- `fact`
- `learning`
- `style`
- `person`
- `relationship`

稳定性：

- `temporary`
- `medium`
- `stable`

敏感级别：

- `normal`
- `private`
- `sensitive`

保存前会和已有记忆比对：

- 完全相同或已有更完整版本时忽略。
- 新信息补充旧记忆时更新旧记忆。
- 同主题新事实取代旧记忆时更新旧记忆。
- 无相似旧记忆时创建新记忆。

## REST API

所有 `/memories` 和 `/v1` 路由都需要：

```http
Authorization: Bearer <GATEWAY_API_KEY>
```

可选：

```http
X-User-Id: user-123
```

主要端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查，不需要鉴权。 |
| `POST` | `/v1/chat/completions` | OpenAI-compatible 聊天接口。 |
| `GET` | `/memories` | 列出活跃长期记忆，不返回 embedding。 |
| `POST` | `/memories/search` | 搜索长期记忆。 |
| `GET` | `/memories/deleted` | 列出软删除记忆。 |
| `DELETE` | `/memories/{memory_id}` | 软删除记忆。 |
| `POST` | `/memories/{memory_id}/restore` | 恢复软删除记忆。 |
| `GET` | `/memories/{memory_id}/why` | 解释某条记忆来源。 |
| `POST` | `/memories/merge` | 合并多条记忆。 |
| `POST` | `/memories/review` | 生成记忆体检建议。 |
| `GET` | `/memories/report` | 生成记忆报告，`format=json|markdown`。 |
| `GET` | `/memories/export` | 导出记忆，`format=json|markdown`。 |
| `POST` | `/memories/restore` | 从导出数据恢复导入。 |
| `GET` | `/memories/decision-logs` | 查看保存/忽略决策日志。 |
| `GET` | `/memories/recent-context` | 查看近期会话摘要。 |
| `GET` | `/memories/core` | 查看核心记忆。 |
| `GET` | `/memories/core/history` | 查看核心记忆历史版本。 |
| `POST` | `/memories/core/consolidate` | 整理核心记忆。 |

## 测试

运行全部测试：

```powershell
pytest
```

运行单个测试文件：

```powershell
pytest tests/test_mcp_server.py
pytest tests/test_chat_gateway.py
pytest tests/test_memory_store.py
```

测试使用 FastAPI `TestClient`、临时 SQLite 数据库和 fake LLM，不需要真实上游模型或 embedding 服务。

主要覆盖：

- 鉴权和 UTF-8 响应头。
- OpenAI-compatible 网关的记忆注入、近期摘要、后台提取和 `stream=true` 501。
- MCP 初始化、工具列表、保存、搜索、删除、恢复、体检、导出。
- 记忆保存门槛、假设场景拦截、敏感信息门槛、去重和更新。
- 核心记忆整理、历史版本和敏感记忆过滤。
- embedding 配置独立于聊天模型配置。

## 构建

项目没有前端，也没有定义单独的构建命令。日常开发使用 editable install：

```powershell
python -m pip install -e ".[dev]"
```

如需做 Python 包分发，可基于 `pyproject.toml` 的 setuptools 配置另行引入构建工具。

## 目录结构

```text
memory-gateway/
├─ app/
│  ├─ api/
│  │  ├─ deps.py
│  │  ├─ health.py
│  │  └─ memories.py
│  ├─ llm/
│  │  ├─ client.py
│  │  └─ prompts.py
│  ├─ mcp_server/
│  │  ├─ auth.py
│  │  ├─ context.py
│  │  └─ server.py
│  ├─ memory/
│  │  ├─ core.py
│  │  ├─ extractor.py
│  │  ├─ models.py
│  │  ├─ report.py
│  │  ├─ resolver.py
│  │  ├─ review.py
│  │  ├─ search.py
│  │  └─ store.py
│  ├─ openai_compat/
│  │  ├─ chat.py
│  │  ├─ schemas.py
│  │  └─ streaming.py
│  ├─ config.py
│  └─ main.py
├─ scripts/
│  ├─ install-service.ps1
│  ├─ show-access-urls.ps1
│  └─ uninstall-service.ps1
├─ tests/
├─ data/
├─ logs/
├─ .env.example
├─ pyproject.toml
└─ README.md
```

重要文件说明：

- `app/main.py`：FastAPI 应用工厂，初始化数据库，挂载 MCP 子应用。
- `app/config.py`：从 `.env` 读取配置。
- `app/api/deps.py`：REST 鉴权、用户 id、依赖注入。
- `app/api/memories.py`：记忆管理 REST API。
- `app/openai_compat/chat.py`：聊天网关、记忆注入、后台提取和近期摘要。
- `app/mcp_server/auth.py`：MCP 子应用鉴权。
- `app/mcp_server/server.py`：MCP server、instructions 和工具实现。
- `app/memory/store.py`：SQLite schema、兼容迁移、记忆 CRUD、软删除、恢复、合并、核心记忆历史。
- `app/memory/search.py`：embedding/关键词搜索、排序、衰减和使用统计。
- `app/memory/extractor.py`：调用 LLM 提取候选记忆并做保存门槛校验。
- `app/memory/resolver.py`：创建、更新、忽略的落库决策。
- `app/memory/core.py`：核心记忆整理。
- `app/memory/review.py`：记忆体检建议。
- `app/memory/report.py`：报告、导出和恢复导入。
- `scripts/install-service.ps1`：Windows 服务安装脚本，使用 2026 端口。
- `scripts/show-access-urls.ps1`：列出 LAN/Tailscale 访问地址。

## 数据文件

- SQLite 数据库默认在 `data/memory.db`。
- 日志默认在 `logs/`。
- embedding 存在 SQLite 的 JSON 字符串字段里，不使用向量数据库。
- 导出接口不会导出 embedding，迁移后应重新生成。
- 删除是软删除，`restore_memory` 或 REST restore 可以恢复。

## 当前限制

- 网关模式 `stream=true` 未实现。
- 网关模式每轮回答后会额外调用一次上游模型做记忆提取。
- MCP 模式下，是否检索和保存取决于客户端模型是否主动调用工具。
- embedding 存储仍在 SQLite JSON 字段里，没有向量索引。
- Windows 服务脚本包含本机路径和固定端口，换机器前需要检查。
- `data/memory.db` 是真实本地数据，调试时不要随意删除、覆盖或用测试数据污染。

