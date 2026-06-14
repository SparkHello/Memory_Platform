# memory-gateway

`memory-gateway` 是一个本地长期记忆网关。它可以放在 AI 客户端和上游大模型之间，提供 OpenAI-compatible Chat Completions API、MCP Streamable HTTP 工具、长期记忆检索与保存、本地 provider 路由、余额账本和用量统计。

项目适合给 Kelivo、Cherry Studio、ChatWise、Chatbox 等支持 OpenAI-compatible API 或 MCP 的客户端增加一层本地记忆能力。所有记忆、provider 配置、用量与本地余额默认存放在本机 SQLite 数据库中。

## 能做什么

- OpenAI-compatible 网关：`/v1/chat/completions` 和 `/v1/models`。
- MCP Streamable HTTP：`/mcp`，提供搜索、保存、合并、删除、恢复、报告等记忆工具。
- 长期记忆：保存、检索、去重、更新、软删除、恢复、导出和导入。
- 核心记忆：从长期记忆中整理稳定的个人背景、偏好、关系、目标等摘要。
- 近期上下文：按 `conversation_id` 保存短期对话摘要。
- Web 管理台：`/ui/`，可管理服务商、模型、路由、价格、余额、用量和记忆。
- 多 provider 路由：按 virtual model、优先级、余额门槛、可用性选择上游模型。
- 本地账本：按经过网关的 token 估算成本并扣减本地余额。
- 多用户隔离：通过 `X-User-Id` 区分用户，不传时使用 `default`。
- UTF-8 JSON 响应：照顾 Windows PowerShell 5.1 等旧客户端。

## 架构速览

```text
AI client
  |  OpenAI-compatible /v1 or MCP /mcp
  v
memory-gateway
  |-- memory search and injection
  |-- provider routing and local billing
  |-- SQLite memory/provider ledger
  v
upstream model provider
```

网关模式下，请求会先检索当前用户相关记忆，将核心记忆、近期上下文和普通长期记忆注入上游消息，再调用上游模型。响应返回后，服务会在后台提取新的长期记忆和更新近期上下文，不阻塞本次聊天响应。

## 技术栈

- Python 3.12
- FastAPI / Uvicorn
- Pydantic v2 / pydantic-settings
- httpx
- MCP Python SDK / FastMCP
- SQLite
- pytest / pytest-asyncio
- React / TypeScript / Vite

## 快速开始

### 1. 安装后端依赖

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

### 2. 创建配置

```powershell
Copy-Item .env.example .env
```

至少需要设置：

```env
GATEWAY_API_KEY=change-me
UPSTREAM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
UPSTREAM_API_KEY=your-upstream-api-key
UPSTREAM_MODEL=glm-5.1
DATABASE_PATH=data/memory.db
```

`GATEWAY_API_KEY` 是客户端访问本地服务时使用的 Bearer token。`UPSTREAM_API_KEY` 是服务端调用上游模型的密钥，不会返回给客户端。

### 3. 构建前端管理台

```powershell
cd ui
npm install
npm run build
cd ..
```

构建产物写入 `ui/dist/`，后端启动后会自动挂载到 `/ui/`。`ui/dist/` 和 `ui/node_modules/` 不提交到 Git。

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
| `UPSTREAM_BASE_URL` | 上游聊天模型的 OpenAI-compatible base URL。 |
| `UPSTREAM_API_KEY` | 上游聊天模型 API key。 |
| `UPSTREAM_MODEL` | 旧式单上游 fallback 模型。没有可用 provider route 时使用。 |
| `PROVIDERS_CONFIG_PATH` | TOML provider 配置路径，默认 `config/providers.toml`。 |
| `EMBEDDING_BASE_URL` | embedding 服务的 OpenAI-compatible base URL。 |
| `EMBEDDING_API_KEY` | embedding API key。为空时使用关键词搜索 fallback。 |
| `EMBEDDING_MODEL` | embedding 模型名。 |
| `EMBEDDING_DIMENSIONS` | embedding 维度。 |
| `DATABASE_PATH` | SQLite 数据库路径，默认 `data/memory.db`。 |
| `REQUEST_TIMEOUT_SECONDS` | 调用上游模型和 embedding 服务的超时时间。 |

不要提交真实 `.env`、`data/*.db`、`logs/` 或 provider API key。数据库里可能包含长期记忆和本地保存的 provider key。

## Web UI

访问：

```text
http://localhost:2026/ui/
```

首次进入 Settings 页面时填写：

- API Base URL：默认同源即可。
- Gateway API Key：填写 `.env` 中的 `GATEWAY_API_KEY`。
- User ID：默认 `default`，也可以填写自定义用户 ID。

主要页面：

- Gateway Config：导入/导出 provider TOML，查看当前配置来源。
- Providers：新增服务商、设置 API key、维护真实模型、价格和分级价格。
- Routes：把对外 virtual model 绑定到服务商模型。
- Billing：查看和手动调整本地 provider 余额。
- Usage：查看最近调用和按 provider / virtual model 聚合的 token 与成本。
- Memories：查看、搜索、编辑、删除和恢复长期记忆。
- Core：查看核心记忆和历史版本。
- Review：运行记忆体检并按确认执行删除、合并、降权建议。
- Recent：查看近期上下文摘要。
- Reports：生成报告、导出备份、恢复导入。
- Logs：查看记忆保存/忽略决策日志。

UI 中填写的 provider API key 会保存到本机 SQLite，不会在页面、admin API 响应或 TOML 导出中回显。编辑 provider 时，API key 输入框留空表示保留旧 key。

## Provider 路由

推荐在 `/ui/` 的 Providers 和 Routes 页面配置。配置优先级是：

```text
SQLite UI 配置 > config/providers.toml > UPSTREAM_* fallback
```

如果 SQLite 中有可用 provider 和 route，网关优先使用 UI 配置；否则尝试 `config/providers.toml`；再否则使用 `.env` 中的 `UPSTREAM_BASE_URL`、`UPSTREAM_API_KEY`、`UPSTREAM_MODEL`。

也可以从示例 TOML 开始：

```powershell
Copy-Item config\providers.example.toml config\providers.toml
```

TOML 示例不会保存真实 API key，只通过 `api_key_env` 指向环境变量：

```toml
[providers.zhipu]
name = "Zhipu"
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key_env = "ZHIPU_API_KEY"
enabled = true
timeout_seconds = 60
```

路由规则：

- 客户端传入的 `model` 会被当作 virtual model。
- route 按 `priority` 降序选择。
- 会过滤 disabled provider、缺少 API key 的 provider、余额低于 `min_balance` 的 provider 和短暂冷却中的 provider。
- 目前实际代理调用只执行 `api_format = "openai_compatible"` 的模型。
- `pricing_mode = "tiered"` 当前用于记录分级价格；实际扣费仍按 route 的输入/输出单价估算。

本地余额账本只代表 memory-gateway 根据经过本代理的请求估算和扣减的余额，不等同于 provider 官网真实余额。

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

当前限制：`stream=true` 返回 501，暂未实现流式转发。

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

主要 MCP 工具：

| 工具 | 说明 |
| --- | --- |
| `search_memory` | 按主题搜索长期记忆。 |
| `submit_memory_text` | 提交用户原文，由服务端拆分、过滤、去重并保存长期记忆。 |
| `save_memory` | 保存一条结构化长期记忆，通常给能准确填写字段的模型使用。 |
| `why_remember` | 解释记忆来源、保存时间、置信度和核心记忆引用。 |
| `merge_memories` | 合并多条同主题记忆。 |
| `get_recent_context_summary` | 读取近期上下文摘要。 |
| `get_core_memory` | 读取核心记忆。 |
| `get_core_memory_history` | 查看核心记忆历史版本。 |
| `consolidate_core_memory` | 调用上游模型整理核心记忆。 |
| `review_memories` | 体检记忆库并返回建议，不自动修改数据。 |
| `memory_report` | 生成当前用户记忆报告。 |
| `export_memories` | 导出当前用户记忆，embedding 不会导出。 |
| `list_memories` | 列出当前用户长期记忆。 |
| `list_deleted_memories` | 列出软删除记忆。 |
| `delete_memory` | 按 ID 软删除记忆。 |
| `restore_memory` | 按 ID 恢复软删除记忆。 |
| `forget_memories` | 按自然语言主题搜索并软删除相关记忆。 |

给客户端模型的建议规则：

```text
你可以使用 memory-gateway 的 MCP 工具访问用户长期记忆。
如果用户本轮表达了长期有用、未来可能再次用到的信息，优先调用 submit_memory_text，并传入用户原文。
不要保存假设、玩笑、一次性安排、短期情绪或没有长期价值的信息。
如果需要回答个人相关问题，先搜索相关记忆，再结合结果回答。
```

## REST API 概览

除 `/health` 外，REST 和 `/v1` 路由都需要：

```http
Authorization: Bearer <GATEWAY_API_KEY>
```

可选：

```http
X-User-Id: user-123
```

常用端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查。 |
| `GET` | `/v1/models` | 返回可用 virtual models。 |
| `POST` | `/v1/chat/completions` | OpenAI-compatible 聊天接口。 |
| `GET` | `/memories` | 列出活跃长期记忆。 |
| `POST` | `/memories/search` | 搜索长期记忆。 |
| `POST` | `/memories/ingest` | 提交用户原文并保存记忆。 |
| `PATCH` | `/memories/{memory_id}` | 更新记忆。 |
| `DELETE` | `/memories/{memory_id}` | 软删除记忆。 |
| `POST` | `/memories/{memory_id}/restore` | 恢复软删除记忆。 |
| `GET` | `/memories/{memory_id}/why` | 查看记忆来源解释。 |
| `POST` | `/memories/merge` | 合并记忆。 |
| `POST` | `/memories/review` | 生成记忆体检建议。 |
| `GET` | `/memories/report` | 生成记忆报告，支持 `format=json|markdown`。 |
| `GET` | `/memories/export` | 导出记忆，支持 `format=json|markdown`。 |
| `POST` | `/memories/restore` | 从导出数据恢复导入。 |
| `GET` | `/memories/core` | 查看核心记忆。 |
| `POST` | `/memories/core/consolidate` | 整理核心记忆。 |
| `GET` | `/memories/recent-context` | 查看近期上下文摘要。 |
| `GET` | `/memories/decision-logs` | 查看保存/忽略决策日志。 |
| `GET` | `/admin/provider-config` | 查看 provider/route 配置，不返回真实 API key。 |
| `POST` | `/admin/provider-config/providers` | 新增 UI provider。 |
| `PATCH` | `/admin/provider-config/providers/{provider}` | 更新 provider。 |
| `DELETE` | `/admin/provider-config/providers/{provider}` | 禁用 provider。 |
| `POST` | `/admin/provider-config/models` | 新增 provider 模型。 |
| `PATCH` | `/admin/provider-config/models/{model_id}` | 更新 provider 模型。 |
| `DELETE` | `/admin/provider-config/models/{model_id}` | 禁用 provider 模型。 |
| `POST` | `/admin/provider-config/routes` | 新增 route。 |
| `PATCH` | `/admin/provider-config/routes/{route_id}` | 更新 route。 |
| `DELETE` | `/admin/provider-config/routes/{route_id}` | 删除 route。 |
| `POST` | `/admin/provider-config/import-toml` | 从 TOML 导入 provider 和 route。 |
| `GET` | `/admin/provider-config/export-toml` | 导出不含真实 key 的 TOML。 |
| `POST` | `/admin/provider-config/providers/{provider}/test` | 测试 provider 连接。 |
| `GET` | `/admin/balances` | 查看本地 provider 余额。 |
| `POST` | `/admin/balances/{provider}/adjust` | 手动调整本地余额。 |
| `GET` | `/admin/usage` | 查看最近 provider 调用记录。 |
| `GET` | `/admin/usage/summary` | 查看 token 和成本汇总。 |

## 记忆保存规则

项目目标是“不乱记”。只有长期有用、用户明确表达过、未来回答可能用到的信息才保存。

硬性门槛：

- `action` 必须是 `create` 或 `update`。
- `importance >= 6`。
- `confidence >= 0.8`。
- `memory` 和 `source_quote` 必须非空。
- 网关模式下，`source_quote` 必须来自用户原文。
- 假设表达不保存，例如“如果”“假如”“假设”“suppose”“imagine”“let's say”。
- `private` / `sensitive` 记忆要求更高门槛，并要求用户明确希望记住。

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

## 数据文件

- SQLite 默认路径：`data/memory.db`。
- 日志默认目录：`logs/`。
- embedding 存在 SQLite 的 JSON 字段中，不使用向量数据库。
- 删除是软删除，可通过 REST 或 MCP 恢复。
- 导出不会包含 embedding，迁移后需要重新生成。
- provider UI 配置、provider key、本地余额、用量记录也存放在同一个 SQLite 数据库中。

## Windows 服务

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

运行全部测试：

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
├─ app/
│  ├─ api/              # REST API
│  ├─ llm/              # OpenAI-compatible LLM client and prompts
│  ├─ mcp_server/       # FastMCP server and auth
│  ├─ memory/           # memory models, store, search, review, report
│  ├─ openai_compat/    # /v1 chat and model endpoints
│  ├─ providers/        # provider routing, billing and local ledger
│  ├─ config.py
│  └─ main.py
├─ config/
│  └─ providers.example.toml
├─ scripts/
├─ tests/
├─ ui/
│  ├─ src/
│  ├─ package.json
│  └─ vite.config.ts
├─ .env.example
├─ .gitignore
├─ pyproject.toml
└─ README.md
```

## 故障排查

- `401 Unauthorized`：检查 `Authorization: Bearer <GATEWAY_API_KEY>` 是否正确。
- `GATEWAY_API_KEY 未配置`：确认服务从项目根目录启动，并读取到了 `.env`。
- 中文乱码：确认客户端按 UTF-8 读取响应；服务端 JSON 响应包含 `charset=utf-8`。
- `/ui` 打不开：先运行 `cd ui; npm run build`，并访问 `http://localhost:2026/ui/`。
- `stream=true` 返回 501：当前尚未实现流式转发，客户端需要关闭 streaming。
- 搜索结果不准：未配置 `EMBEDDING_API_KEY` 时会回退到关键词搜索。
- 局域网设备无法访问：服务需要 `--host 0.0.0.0`，并确认防火墙放行端口。
- Windows 服务启动失败：检查 NSSM 路径、项目路径、虚拟环境 Python 路径和端口占用。

## 安全提醒

- 不要把本服务直接暴露到公网。
- 不要提交真实 `.env`、SQLite 数据库、日志或任何真实 API key。
- `data/memory.db` 可能包含真实记忆和 provider key，是敏感文件。
- UI 仅保存浏览器本地连接信息到 `localStorage`；服务端密钥保存在 `.env` 或 SQLite。
- TOML 导出会使用 `api_key_env` 占位，不包含真实 API key。

## 当前限制

- `/v1/chat/completions` 暂不支持流式转发。
- 网关模式每轮响应后会额外调用一次上游模型做记忆提取。
- 分级价格目前主要用于记录；实际扣费按 route 的输入/输出单价估算。
- embedding 存储在 SQLite JSON 字段中，没有独立向量索引。
- 导出不包含 embedding，迁移恢复后需要重新生成。
- Windows 服务脚本包含本机路径，需要按部署环境调整。
