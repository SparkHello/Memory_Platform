# memory-gateway

`memory-gateway` 是一个本地长期记忆网关。它可以放在 AI 客户端和上游大模型之间，提供 OpenAI-compatible Chat Completions API、MCP Streamable HTTP 工具、长期记忆检索与保存、多 provider 路由、本地余额账本、用量统计和 Web 管理台。

它适合给 Kelivo、Cherry Studio、ChatWise、Chatbox 等支持 OpenAI-compatible API 或 MCP 的客户端加一层本地记忆能力。默认情况下，记忆、核心记忆、近期上下文、provider 配置、本地余额和用量记录都保存在本机 SQLite 数据库中。

## 功能一览

- OpenAI-compatible 网关：提供 `/v1/chat/completions` 和 `/v1/models`，客户端只需要连接本地 `/v1`。
- 自动记忆注入：聊天前检索长期记忆、核心记忆和近期上下文，并作为 system context 注入上游模型。
- 自动记忆沉淀：聊天响应返回后，后台提取用户本轮长期有用的信息，执行过滤、去重、更新或保存。
- MCP 记忆工具：通过 `/mcp` 暴露搜索、保存、遗忘、恢复、合并、体检、报告、导出等工具。
- 长期记忆库：支持类型、重要度、置信度、稳定性、有效期、复核时间、敏感级别和来源追踪。
- 核心记忆：从长期记忆中整理稳定背景，按 `profile`、`preferences`、`relationships`、`routines`、`goals`、`communication` 六个分区保存版本。
- 近期上下文：按 `conversation_id` 保存短期会话摘要，用来恢复最近话题，不进入长期记忆。
- 多 provider 路由：把对外 virtual model 映射到一个或多个 OpenAI-compatible 上游模型，支持优先级、fallback、冷却和最低余额过滤。
- 自建 API 转发：可以接入官方 API、One API、new-api、自建反代等任意 OpenAI Chat Completions 兼容地址。
- 本地成本账本：根据 token usage 或估算 token 计算费用，记录调用事件并扣减本地 provider 余额。
- Web 管理台：管理 provider、模型、路由、余额、用量、记忆、核心记忆、体检建议、报告和备份。
- 多用户隔离：通过 `X-User-Id` 区分用户；不传时使用 `default`。
- UTF-8 JSON 响应：默认带 `charset=utf-8`，兼容 Windows PowerShell 5.1 等旧客户端。

## 架构速览

```text
AI client
  |  OpenAI-compatible /v1
  |  MCP Streamable HTTP /mcp
  v
memory-gateway
  |-- auth by GATEWAY_API_KEY
  |-- memory search, injection and ingestion
  |-- provider routing, failover and local billing
  |-- SQLite memory/provider ledger
  v
upstream OpenAI-compatible provider
```

网关模式下，请求进入 `/v1/chat/completions` 后，服务会先按当前用户和本轮消息检索记忆，注入核心记忆、近期上下文和普通长期记忆，再调用上游模型。响应返回给客户端后，后台任务会提取新记忆并更新近期上下文，不阻塞本轮聊天。

MCP 模式下，客户端可以直接连接模型，再让模型通过 `/mcp` 主动调用工具来搜索、保存、整理或遗忘记忆。两种模式可以同时使用。

## 技术栈

- Python 3.12
- FastAPI / Uvicorn
- Pydantic v2 / pydantic-settings
- httpx
- MCP Python SDK / FastMCP
- SQLite
- pytest / pytest-asyncio
- React / TypeScript / Vite
- lucide-react

## 快速开始

### 1. 安装后端依赖

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

macOS / Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

### 2. 创建 `.env`

```powershell
Copy-Item .env.example .env
```

至少需要设置：

```env
GATEWAY_API_KEY=change-me

# 没有可用 SQLite/TOML provider route 时使用这个旧式 fallback。
UPSTREAM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
UPSTREAM_API_KEY=your-upstream-api-key
UPSTREAM_MODEL=glm-5.1

PROVIDERS_CONFIG_PATH=config/providers.toml
DATABASE_PATH=data/memory.db

# 可选。为空时使用关键词搜索 fallback。
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024
```

`GATEWAY_API_KEY` 是客户端访问本地服务的 Bearer token。上游 provider key 存在 `.env` 或 SQLite 中，不会返回给客户端。

### 3. 构建前端管理台

```powershell
cd ui
npm install
npm run build
cd ..
```

构建产物写入 `ui/dist/`，后端启动后会挂载到 `/ui/`。开发调试前端时可以运行：

```powershell
cd ui
npm run dev -- --host 127.0.0.1
```

### 4. 启动服务

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 2026
```

检查服务：

```powershell
Invoke-RestMethod http://localhost:2026/health
```

常用地址：

| 功能 | 地址 |
| --- | --- |
| Health | `http://localhost:2026/health` |
| Web UI | `http://localhost:2026/ui/` |
| OpenAI-compatible Base URL | `http://localhost:2026/v1` |
| MCP Streamable HTTP | `http://localhost:2026/mcp` |

## 配置项

| 变量 | 说明 |
| --- | --- |
| `GATEWAY_API_KEY` | 访问本地 memory-gateway 的 Bearer token。 |
| `UPSTREAM_BASE_URL` | 旧式单上游 fallback 的 OpenAI-compatible base URL。 |
| `UPSTREAM_API_KEY` | 旧式单上游 fallback API key。 |
| `UPSTREAM_MODEL` | 旧式单上游 fallback 模型。 |
| `PROVIDERS_CONFIG_PATH` | TOML provider 配置路径，默认 `config/providers.toml`。 |
| `EMBEDDING_BASE_URL` | OpenAI-compatible embeddings base URL。 |
| `EMBEDDING_API_KEY` | embedding API key。为空时退回关键词搜索。 |
| `EMBEDDING_MODEL` | embedding 模型名。 |
| `EMBEDDING_DIMENSIONS` | embedding 维度。 |
| `DATABASE_PATH` | SQLite 数据库路径，默认 `data/memory.db`。 |
| `REQUEST_TIMEOUT_SECONDS` | 上游模型和 embedding 请求超时时间。 |

不要提交真实 `.env`、`data/*.db`、`logs/` 或 provider API key。数据库里可能包含长期记忆和本地保存的 provider key。

## Provider 路由

配置优先级固定为：

```text
SQLite UI 配置 > config/providers.toml > UPSTREAM_* fallback
```

如果 SQLite 中有可用 provider、provider model 和 route，网关优先使用 UI 配置；否则尝试 `config/providers.toml`；再否则使用 `.env` 中的 `UPSTREAM_BASE_URL`、`UPSTREAM_API_KEY` 和 `UPSTREAM_MODEL`。

路由配置分三层：

- `providers`：服务商基础配置，包括 `base_url`、API key、启用状态和超时。`base_url` 应是不带 `/chat/completions` 的 OpenAI-compatible base URL，例如 `https://api.openai.com/v1`。
- `provider_models`：服务商下面的真实上游模型，包括 `upstream_model`、显示名称、接口类型、价格和分级价格元数据。
- `routes`：客户端看到的 `virtual_model` 到真实 provider model 的映射。一个 virtual model 可以有多条 route，用优先级和 fallback 实现多上游切换。

推荐在 `/ui/` 的“服务商与模型”和“路由”页面配置。也可以从示例 TOML 开始：

```powershell
Copy-Item config\providers.example.toml config\providers.toml
```

最小 TOML 示例：

```toml
[router]
default_model = "glm-5.1"
fallback_enabled = true

[providers.zhipu]
name = "Zhipu"
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key_env = "ZHIPU_API_KEY"
enabled = true
timeout_seconds = 60

[[provider_models]]
id = "zhipu-glm-5-1"
provider = "zhipu"
upstream_model = "glm-5.1"
display_name = "GLM 5.1"
api_format = "openai_compatible"
pricing_mode = "flat"
input_price_per_million = 0.0
output_price_per_million = 0.0
currency = "CNY"
enabled = true

[[routes]]
virtual_model = "glm-5.1"
provider = "zhipu"
upstream_model = "glm-5.1"
provider_model_id = "zhipu-glm-5-1"
priority = 100
input_price_per_million = 0.0
output_price_per_million = 0.0
currency = "CNY"
min_balance = 0.0
enabled = true
```

路由规则：

- 客户端传入的 `model` 会被当作 virtual model。
- 同一 virtual model 的 route 按 `priority` 降序选择。
- 会跳过禁用 provider、禁用 route、缺 API key、余额低于 `min_balance`、处于冷却期的 provider。
- 如果 route 绑定了 `provider_model_id`，该 provider model 必须存在、启用、属于同一 provider，并且 `api_format = "openai_compatible"`。
- 上游返回 auth、quota、rate limit、5xx、超时或网络错误时，如果开启 fallback，会尝试下一条可用 route。
- 失败的 provider 会进入短暂冷却期。网络、超时、rate limit、5xx 通常冷却 30 秒；auth 或 quota 错误通常冷却 60 秒。
- `claude_sdk` 可以作为 provider model 元数据记录和导出，但当前不会参与 OpenAI-compatible 转发。
- `pricing_mode = "tiered"` 当前主要用于记录价格元数据；实际扣费按 route 上的输入、输出单价计算。

成功响应会追加 `gateway` 字段，标明实际 provider、上游模型、费用和 token 是否估算。若上游响应没有 `usage`，网关会按字符数粗略估算 token。

## Web UI

访问：

```text
http://localhost:2026/ui/
```

首次进入“设置”页，填写：

- API 基础地址：同源部署时可用默认值，也可以填 `http://localhost:2026`。
- 网关 API Key：`.env` 里的 `GATEWAY_API_KEY`。
- 用户 ID：默认 `default`，也可以填自定义用户 ID。

主要页面：

- 总览：服务状态、接入地址、当前用户、记忆统计和快捷操作。
- 网关概览：provider 配置来源、服务商数量、模型数量、启用路由和密钥配置状态。
- 服务商与模型：新增、编辑、禁用、硬删除 provider；保存或清除 API key；维护真实上游模型、接口类型、价格和分级价格元数据；测试连接。
- 路由：把 virtual model 绑定到服务商模型，配置优先级、最低余额和启用状态。
- 导入 / 导出：从 `providers.toml` 合并配置到 SQLite，或导出不含真实 API key 的 TOML。
- 余额账本：查看和手动调整本地 provider 余额。
- 用量统计：查看最近调用记录和按 provider / virtual model 聚合的 token 与成本。
- 记忆库：查看、搜索、筛选、编辑、解释、软删除和恢复长期记忆。
- 核心记忆：查看六个核心分区、历史版本，并手动重新整理核心记忆。
- 记忆体检：生成保留、合并、降权、删除或复核建议，并在确认后应用删除、合并、降权。
- 近期上下文：查看按 `conversation_id` 保存的短期摘要。
- 报告与备份：生成记忆报告，导出 JSON / Markdown，或从 JSON 恢复导入。
- 决策日志：查看每条候选记忆被保存、更新或忽略的原因。
- 设置：保存本地浏览器中的 UI 连接信息。
- 接入信息：复制 OpenAI-compatible、MCP 和 REST 常用接入参数。

UI 中填写的 provider API key 会保存到本机 SQLite，不会在页面、admin API 响应或 TOML 导出中回显。编辑 provider 时，API key 输入框留空表示保留旧 key；显式填空字符串可清除 key。

## OpenAI-compatible 使用

客户端配置：

| 字段 | 值 |
| --- | --- |
| Base URL | `http://<host>:2026/v1` |
| API Key | `.env` 中的 `GATEWAY_API_KEY` |
| Model | 配置好的 virtual model，例如 `glm-5.1` |

PowerShell 示例：

```powershell
$headers = @{
  Authorization = "Bearer change-me"
  "Content-Type" = "application/json; charset=utf-8"
  "X-User-Id" = "default"
}

$body = @{
  model = "glm-5.1"
  messages = @(
    @{ role = "user"; content = "我喜欢黑咖啡，请记住。" }
  )
  temperature = 0.7
  conversation_id = "demo-chat"
} | ConvertTo-Json -Depth 10

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:2026/v1/chat/completions" `
  -Headers $headers `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

说明：

- `conversation_id` 是网关扩展字段，用于近期上下文摘要。
- `X-User-Id` 用于隔离不同用户的记忆和日志。
- 当前 `stream=true` 会返回 501，暂未实现流式转发。
- 上游响应里的 assistant message 会被清理为 `role` 和 `content`，避免把 `reasoning_content` 等字段透传给客户端。

## MCP 使用

MCP endpoint:

```text
http://<host>:2026/mcp
```

请求头：

```http
Authorization: Bearer <GATEWAY_API_KEY>
X-User-Id: optional-user-id
```

MCP 工具：

| 工具 | 说明 |
| --- | --- |
| `search_memory` | 按主题搜索当前用户长期记忆，并刷新使用计数。 |
| `submit_memory_text` | 提交用户原文，由服务端拆分、过滤、去重并保存多条记忆。 |
| `save_memory` | 保存一条结构化长期记忆。 |
| `why_remember` | 解释某条记忆的来源、置信度、保存时间和核心记忆引用。 |
| `merge_memories` | 合并多条同主题记忆，保留证据 ID，并软删除多余记忆。 |
| `get_recent_context_summary` | 读取近期上下文摘要。 |
| `get_core_memory` | 读取核心记忆当前版本。 |
| `get_core_memory_history` | 查看核心记忆历史版本。 |
| `consolidate_core_memory` | 调用上游模型重新整理核心记忆。 |
| `review_memories` | 体检记忆库，只返回建议，不自动修改数据。 |
| `memory_report` | 生成当前用户记忆报告。 |
| `export_memories` | 导出当前用户记忆，embedding 不会导出。 |
| `list_memories` | 列出当前用户活跃长期记忆。 |
| `list_deleted_memories` | 列出软删除记忆。 |
| `delete_memory` | 按 ID 软删除记忆。 |
| `restore_memory` | 按 ID 恢复软删除记忆。 |
| `forget_memories` | 按自然语言主题搜索并软删除相关记忆。 |

给客户端模型的建议规则：

```text
你可以使用 memory-gateway 的 MCP 工具访问用户长期记忆。
当问题涉及用户偏好、习惯、家人朋友、健康、计划、长期事项或过去话题时，先调用 search_memory。
如果用户本轮提供了长期有用的新信息，调用 submit_memory_text 并传入用户原文。
检索旧记忆和保存新信息可以在同一轮连续发生，不要二选一。
不要保存假设、玩笑、一次性安排、短期情绪或没有长期价值的信息。
用户要求忘记某类信息时，优先调用 forget_memories；要求忘记明确 ID 时调用 delete_memory。
```

## REST API 概览

除 `/health` 外，REST、admin 和 `/v1` 路由都需要：

```http
Authorization: Bearer <GATEWAY_API_KEY>
```

可选：

```http
X-User-Id: user-123
```

### 健康与聊天

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查。 |
| `GET` | `/v1/models` | 返回可用 virtual models；没有 provider route 时返回 `UPSTREAM_MODEL`。 |
| `POST` | `/v1/chat/completions` | OpenAI-compatible 聊天接口，带记忆注入和后台记忆提取。 |

### 记忆管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/memories` | 列出活跃长期记忆。 |
| `GET` | `/memories/deleted` | 列出软删除记忆。 |
| `POST` | `/memories/search` | 搜索长期记忆。 |
| `POST` | `/memories/ingest` | 提交用户原文并保存长期记忆。 |
| `PATCH` | `/memories/{memory_id}` | 更新记忆内容、类型、重要度、置信度等字段。 |
| `DELETE` | `/memories/{memory_id}` | 软删除记忆。 |
| `POST` | `/memories/{memory_id}/restore` | 恢复软删除记忆。 |
| `GET` | `/memories/{memory_id}/why` | 查看记忆来源解释。 |
| `POST` | `/memories/merge` | 合并多条记忆。 |
| `POST` | `/memories/review` | 生成记忆体检建议。 |
| `GET` | `/memories/report?format=json|markdown` | 生成记忆报告。 |
| `GET` | `/memories/export?format=json|markdown` | 导出记忆备份。 |
| `POST` | `/memories/restore` | 从导出数据恢复导入。 |
| `GET` | `/memories/core` | 查看核心记忆。 |
| `GET` | `/memories/core/history` | 查看核心记忆历史版本。 |
| `POST` | `/memories/core/consolidate` | 整理核心记忆。 |
| `GET` | `/memories/recent-context` | 查看近期上下文摘要。 |
| `GET` | `/memories/decision-logs` | 查看保存、更新、忽略决策日志。 |

### Provider 配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/admin/providers` | 查看当前生效 provider/router 配置摘要。 |
| `GET` | `/admin/provider-config` | 查看可编辑 provider/route 配置，不返回真实 API key。 |
| `POST` | `/admin/provider-config/providers` | 新增或保存 UI provider。 |
| `PATCH` | `/admin/provider-config/providers/{provider}` | 更新 provider；省略 `api_key` 表示保留旧值，传空字符串表示清除。 |
| `DELETE` | `/admin/provider-config/providers/{provider}` | 禁用 provider。 |
| `DELETE` | `/admin/provider-config/providers/{provider}?hard=true` | 硬删除 provider，并删除其模型和路由。 |
| `POST` | `/admin/provider-config/providers/{provider}/test` | 测试 provider 连接。 |
| `POST` | `/admin/provider-config/models` | 新增 provider model。 |
| `PATCH` | `/admin/provider-config/models/{model_id}` | 更新 provider model。 |
| `DELETE` | `/admin/provider-config/models/{model_id}` | 禁用 provider model。 |
| `DELETE` | `/admin/provider-config/models/{model_id}?hard=true` | 硬删除 provider model，并删除绑定路由。 |
| `POST` | `/admin/provider-config/routes` | 新增 route。 |
| `PATCH` | `/admin/provider-config/routes/{route_id}` | 更新 route。 |
| `DELETE` | `/admin/provider-config/routes/{route_id}` | 删除 route。 |
| `POST` | `/admin/provider-config/import-toml` | 从 `PROVIDERS_CONFIG_PATH` 导入 provider、model 和 route 到 SQLite。 |
| `GET` | `/admin/provider-config/export-toml` | 导出不含真实 key 的 TOML。 |

### 成本与用量

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/admin/balances` | 查看本地 provider 余额。 |
| `POST` | `/admin/balances/{provider}/adjust` | 手动调整本地余额。 |
| `GET` | `/admin/usage` | 查看最近 provider 调用记录。 |
| `GET` | `/admin/usage/summary` | 查看 token 和成本汇总。 |

## 记忆保存规则

项目目标是“不乱记”。只有长期有用、用户明确表达过、未来回答可能用到的信息才保存。

硬性门槛：

- `action` 必须是 `create` 或 `update`。
- `memory` 和 `source_quote` 必须非空。
- `importance >= 6`。
- `confidence >= 0.8`。
- 网关和 `submit_memory_text` 模式下，`source_quote` 必须来自用户原文。
- 假设场景不保存，例如“如果”“假如”“假设”“suppose”“imagine”“let's say”。
- `private` / `sensitive` 记忆要求 `importance >= 8`、`confidence >= 0.9`，并且用户要明确表达希望记住。

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

搜索支持 embedding 语义搜索和关键词 fallback。配置 `EMBEDDING_API_KEY` 后会调用 OpenAI-compatible `/embeddings`；为空时使用关键词、字符重叠和元数据排序。排序会综合相关度、重要度、使用次数、时间衰减、有效期和敏感级别。`person` 和 `relationship` 不会因时间自然衰减。

## 数据文件

- SQLite 默认路径：`data/memory.db`。
- 日志默认目录：`logs/`。
- embedding 存在 SQLite 的 JSON 字段中，没有独立向量数据库。
- 删除是软删除，可通过 REST、MCP 或 UI 恢复。
- 记忆导出不会包含 embedding，迁移恢复后需要后续重新生成。
- provider UI 配置、provider key、本地余额、余额调整和用量记录也存放在同一个 SQLite 数据库中。

## Windows 服务与访问地址

项目提供 NSSM 辅助脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
powershell -ExecutionPolicy Bypass -File scripts\uninstall-service.ps1
```

注意：`scripts/install-service.ps1` 当前包含本机路径和固定端口，换机器或换目录前需要检查 `$nssm`、`$projectDir` 和 `$port`。

查看 LAN / Tailscale 地址：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1 -Port 2026
```

## 测试

运行后端测试：

```powershell
pytest
```

如果 `pytest` 不在 PATH：

```powershell
.\.venv\Scripts\python -m pytest
```

常用定向测试：

```powershell
pytest tests/test_mcp_server.py
pytest tests/test_chat_gateway.py
pytest tests/test_memory_store.py
pytest tests/test_provider_router.py
pytest tests/test_provider_admin_api.py
```

前端构建：

```powershell
cd ui
npm run build
```

测试使用 fake LLM、临时 SQLite 和空 embedding key，不需要真实上游模型或外部网络。

## 目录结构

```text
memory-gateway/
|-- app/
|   |-- api/              # REST API
|   |-- llm/              # OpenAI-compatible LLM client and prompts
|   |-- mcp_server/       # FastMCP server and auth middleware
|   |-- memory/           # memory models, store, search, resolver, review, report
|   |-- openai_compat/    # /v1 chat and model endpoints
|   |-- providers/        # provider routing, billing and local ledger
|   |-- config.py
|   `-- main.py
|-- config/
|   `-- providers.example.toml
|-- scripts/
|-- tests/
|-- ui/
|   |-- src/
|   |   |-- components/
|   |   |-- hooks/
|   |   |-- layout/
|   |   |-- pages/
|   |   |-- utils/
|   |   |-- App.tsx
|   |   |-- api.ts
|   |   |-- main.tsx
|   |   |-- storage.ts
|   |   `-- types.ts
|   |-- package.json
|   `-- vite.config.ts
|-- .env.example
|-- .gitignore
|-- pyproject.toml
`-- README.md
```

## 故障排查

- `401 Unauthorized`：检查 `Authorization: Bearer <GATEWAY_API_KEY>` 是否正确。
- `GATEWAY_API_KEY 未配置`：确认服务从项目根目录启动，并读取到了 `.env`。
- `/ui` 打不开：先运行 `cd ui; npm run build`，并访问 `http://localhost:2026/ui/`。
- `stream=true` 返回 501：当前尚未实现流式转发，客户端需要关闭 streaming。
- `/v1/models` 只有 fallback 模型：说明没有可用 SQLite/TOML provider route。
- provider 不参与路由：检查 provider 是否启用、API key 是否存在、provider model 是否启用且为 `openai_compatible`、route 是否启用、余额是否低于 `min_balance`。
- provider 调用失败：检查 base URL、API key、真实模型名，或先用 `/admin/provider-config/providers/{provider}/test` 测试连接。
- 搜索结果不准：未配置 `EMBEDDING_API_KEY` 时会退回关键词搜索。
- 局域网设备无法访问：服务需要用 `--host 0.0.0.0` 启动，并确认防火墙放行端口。
- Windows 服务启动失败：检查 NSSM 路径、项目路径、虚拟环境 Python 路径和端口占用。

## 安全提醒

- 不要把本服务直接暴露到公网。
- 不要提交真实 `.env`、SQLite 数据库、日志或任何真实 API key。
- `data/memory.db` 可能包含真实记忆和 provider key，是敏感文件。
- UI 只把连接信息保存到浏览器 `localStorage`；服务端密钥保存在 `.env` 或 SQLite。
- TOML 导出只保留 `api_key_env` 占位，不包含真实 API key。
- MCP 子应用关闭了 SDK 默认 DNS rebinding 防护，以便局域网或 Tailscale 访问；鉴权由 `MCPAuthMiddleware` 负责。

## 当前限制

- `/v1/chat/completions` 暂不支持流式转发，`stream=true` 返回 501。
- 网关模式每轮聊天响应后会额外调用一次上游模型做记忆提取。
- 分级价格当前主要用于展示和导出；实际扣费按 route 上的输入、输出单价计算。
- `claude_sdk` provider model 当前只记录元数据，不参与 OpenAI-compatible 转发。
- embedding 存在 SQLite JSON 字段中，没有独立向量索引。
- 导出不包含 embedding，迁移恢复后需要重新生成。
- 设置页当前只保存 UI 连接信息，服务端 `.env` 修改仍需手动编辑。
- Windows 服务脚本包含本机路径，部署到其他机器前需要调整。
