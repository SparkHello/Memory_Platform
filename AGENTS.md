# AGENTS.md

面向后续 Codex / AI agent 的项目说明。修改代码前先读本文件和 `README.md`，再根据任务读取相关模块。

## 项目背景

`memory-gateway` 是一个长期记忆服务，主要给 Kelivo 这类 iOS AI 客户端使用。客户端模型通过 `/mcp` 主动检索、保存和整理记忆；管理、评测和备份使用 `/memories/*` 与 `/ui`。外部 OpenAI-compatible `/v1` 网关已经废弃并返回 `410 Gone`。

项目目标不是“尽量多记”，而是“不乱记”：只保存长期有用、用户明确表达过、未来回答可能用到的信息。
当前也支持轻量记忆空间、主题和实体标签，用于本地分类、过滤、网络图视图和导出恢复。

## 技术栈

- Python 3.12
- FastAPI
- Uvicorn
- Pydantic v2 / pydantic-settings
- httpx
- MCP Python SDK / FastMCP
- SQLite
- pytest / pytest-asyncio
- TypeScript / React / Vite（本地 Memory Console）

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
.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 2026
```

健康检查：

```powershell
curl http://localhost:2026/health
```

运行测试：

```powershell
pytest
# 如果 pytest 不在 PATH 中，使用项目虚拟环境：
.\.venv\Scripts\python -m pytest
```

常用定向测试：

```powershell
pytest tests/test_mcp_server.py
pytest tests/test_memory_extraction.py
pytest tests/test_memory_store.py
pytest tests/test_memory_search.py
```

真实库只读巡检：

```powershell
.\.venv\Scripts\python.exe scripts\audit_memory_db.py --database data\memory.db --env-file .env
.\.venv\Scripts\python.exe scripts\audit_memory_db.py --database data\memory.db --json
```

记忆机制健康度诊断（只读，判定扇区分化、生命周期、temporal KG、图结构是否被真实数据激活）：

```powershell
.\.venv\Scripts\python.exe scripts\diagnose_memory_health.py --database data\memory.db
```

微型召回评测（只读快照真实库到 eval/，再用人工标注 query 给 search_memory 打分，record_usage=False 不污染数据）：

```powershell
.\.venv\Scripts\python.exe scripts\eval_recall.py --init --database data\memory.db
# 编辑 eval\labels.jsonl 为每个 query 填 relevant_ids
.\.venv\Scripts\python.exe scripts\eval_recall.py --run
```

同一套诊断/评测能力也暴露在 Web Console 的“评测闭环”页。该页只把真实库快照到 `EVAL_DIR`（默认 `eval/`，已 gitignore），标注和结果都留在本地工作区。

查看 LAN / Tailscale 地址：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1 -Port 2026
```

前端 UI 构建：

```powershell
cd ui
npm install
npm run build
```

Windows 服务脚本：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1
powershell -ExecutionPolicy Bypass -File scripts\uninstall-service.ps1
```

## 开发工作流

- 项目根目录是 `C:\Users\spari\Documents\Memory\memory-gateway`，外层 `Memory` 目录不是仓库根。
- 修改前先跑 `git status --short`。如果只看到 Git 读取用户级 ignore 的权限警告，但没有文件列表，通常表示工作区干净。
- 搜索优先用 `rg` / `rg --files`，再按任务读取相关模块；不要只凭 README 推断实现。
- 文档改动通常不需要跑完整测试；代码、接口、schema 或保存规则变更需要跑相关定向测试，风险较大时再跑完整 `pytest`。
- UI 代码位于 `ui/`，改动后至少跑 `npm run build`；`ui/dist/` 只是构建产物，后端通过 `/ui` 挂载它。
- 测试应继续使用 `tests/conftest.py` 里的 fake LLM、临时 SQLite 和空 embedding key，不要引入真实网络调用。
- 不要通过测试或脚本污染 `data/memory.db`，真实运行数据与测试数据必须隔离。

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

## 接口与安全边界

- FastAPI 自有路由先注册，`/ui` 静态目录必须放在 MCP 兜底挂载之前，随后再把 MCP 子应用挂载到 `/`；实际 MCP Streamable HTTP 端点仍是 `/mcp`。
- REST 端通过 `app/api/deps.py` 校验 Bearer token；MCP 不走 FastAPI dependency，由 `app/mcp_server/auth.py` 的 `MCPAuthMiddleware` 校验。
- `X-User-Id` 是用户隔离边界；未传时使用 `default`。新增查询、导出、恢复、日志或工具时都要确认按 user id 过滤。
- MCP 使用 `stateless_http=True` 和 `json_response=True`，测试默认每个 POST 都是独立请求。
- MCP 关闭 DNS rebinding protection 是为了让局域网/iPhone/Tailscale 访问可用；不要在没有替代接入方案的情况下改回默认。
- `GATEWAY_API_KEY` 是本地服务的客户端密钥；`UPSTREAM_API_KEY` 和 `EMBEDDING_API_KEY` 只用于服务端调用上游模型，不应该透传给客户端。
- `ALLOW_SENSITIVE_EGRESS=false` 时，敏感原文不得发送给远程提取、embedding 或 AI 体检；新增模型调用必须经过同一出站策略。
- ingest 决策日志不得复制完整 `source_quote`；敏感候选正文只记录长度、哈希、敏感级别和关联 memory ID。
- 模型提取候选必须通过逐字 quote、事实锚点、否定一致性和子句级敏感授权；direct/update/restore 仍须在 store 边界强制 sensitivity 下限。

## 重要文件说明

- `app/main.py`：应用工厂。创建 FastAPI app，初始化 SQLite，启动 MCP session manager，先挂载 `/ui` 静态目录，再兜底挂载 MCP 子应用。
- `app/config.py`：配置入口。读取 `.env`，包含上游 chat、embedding、数据库和超时配置。
- `app/api/deps.py`：REST 鉴权、`X-User-Id`、MemoryStore、LLM client 和 embedding client 依赖。
- `app/api/memories.py`：记忆管理 REST API，包括列表、搜索、删除、恢复、导出、报告、合并、核心记忆和决策日志。
- `app/openai_compat/schemas.py`：内部上游 LLM 请求使用的 OpenAI-compatible schema。
- `app/llm/client.py`：OpenAI-compatible chat client。
- `app/llm/prompts.py`：记忆注入、记忆提取和核心记忆整理 prompt。
- `app/mcp_server/auth.py`：MCP 子应用鉴权。MCP 不经过 FastAPI 依赖，所以在 ASGI middleware 里校验 Bearer token。
- `app/mcp_server/context.py`：用 contextvar 保存当前 MCP 请求的 user id。
- `app/mcp_server/server.py`：FastMCP server、instructions 和全部 MCP 工具。
- `app/memory/models.py`：记忆、核心记忆、候选记忆、体检建议、决策日志等 Pydantic 模型。
- `app/memory/store.py`：SQLite 表结构、兼容迁移、CRUD、空间/主题/实体分类、Time Ripple 邻近激活、软删除、恢复、合并、导入、核心记忆版本历史、近期摘要和决策日志。
- `app/memory/search.py`：embedding/中文关键词召回、拒绝阈值、多模式自然浮现、敏感硬过滤、使用统计和 Time Ripple 配置接入。
- `app/memory/extractor.py`：LLM 记忆提取和保存门槛校验。
- `app/memory/resolver.py`：判断候选记忆应创建、更新旧记忆还是忽略。
- `app/memory/core.py`：核心记忆整理。只从已保存长期记忆中提炼，并要求 evidence ids。
- `app/memory/review.py`：记忆体检建议，不直接修改数据。
- `app/memory/report.py`：记忆报告、导出和恢复导入。
- `app/memory/graph_traverse.py`：从 seed 记忆出发的有界 Personalized PageRank / waypoint 图遍历，返回关联记忆排序和路径解释。
- `app/memory/utils.py`：记忆模块共享的纯工具函数，例如 ISO datetime 解析、JSON 对象提取、文本 terms/normalize、相似度和否定词检测。
- `scripts/audit_memory_db.py`：真实 SQLite 记忆库的只读巡检工具。只检查 schema、旧 type 残留、Time Ripple 配置、JSON 字段和 usage_count/temporal 统计，不写入 `data/memory.db`，也不打印密钥。
- `app/memory/evaluation.py`：机制诊断与召回评测共享实现，供 REST/Web 和 CLI 共同调用。
- `scripts/diagnose_memory_health.py`：只读诊断各记忆机制是否被真实数据激活（扇区分化、生命周期状态、temporal KG、图结构），把原始计数翻译成 active/degenerate/dormant/sparse 判定。
- `scripts/eval_recall.py`：微型召回评测。`--init` 按 user id 建立物理隔离快照，`--run` 以 `record_usage=False` 输出排序、无答案误召、拒答和实际 fallback 指标。真实库全程只读，`eval/` 已被 gitignore。
- `docs/client_integration.md`：Kelivo/iOS 接入说明。维护 MCP 原文提交原则、temporal key 填写边界，以及对外把 `usage_count` 解释为 `activation_count` 的文案。
- `ui/`：React/Vite 本地 Memory Console。连接信息只写浏览器 `localStorage`，第一阶段 Settings 不写 `.env`。
- `tests/`：pytest 测试，覆盖 REST、MCP、存储、搜索、核心记忆、编码和配置。
- `scripts/`：Windows PowerShell 辅助脚本。

## 测试选择指南

| 变更范围 | 优先测试 |
| --- | --- |
| REST 鉴权、路由、响应字段 | `pytest tests/test_memory_management.py tests/test_response_charset.py tests/test_deprecated_v1.py` |
| MCP 工具、instructions、鉴权 | `pytest tests/test_mcp_server.py` |
| 保存门槛、source_quote、敏感信息 | `pytest tests/test_memory_extraction.py tests/test_mcp_server.py` |
| SQLite schema、迁移、CRUD、空间分类、软删除 | `pytest tests/test_memory_store.py` |
| 搜索排序、embedding fallback、使用统计 | `pytest tests/test_memory_search.py tests/test_embedding_config.py` |
| 真实库只读巡检脚本 | `pytest tests/test_memory_audit_script.py`，必要时再运行 `scripts\audit_memory_db.py --database data\memory.db --env-file .env` |
| 核心记忆整理和历史 | `pytest tests/test_core_memory.py` |
| LLM client 编码或上游请求格式 | `pytest tests/test_llm_client.py tests/test_memory_extraction.py` |
| 前端 UI、`/ui` 静态挂载 | `cd ui; npm run build`，必要时再启动后端访问 `http://localhost:2026/ui/` |

## 已知限制

- MCP 模式依赖模型主动调用工具，效果受客户端系统提示词影响。
- embedding 存在 SQLite JSON 字段中，没有向量数据库或向量索引。
- 导出不会包含 embedding，迁移后需要重新生成。
- JSON 导出中的核心记忆历史和决策日志仅供审计，restore 不写回；响应会显式列出这些分区。
- 永久删除会清理当前库与本地 eval 工作区，但无法删除用户已经复制到外部的导出或备份。
- Windows 服务脚本包含本机绝对路径和固定端口 2026。

## 不要随便修改的地方

- 不要改 `.env`，里面可能有真实密钥。
- 不要删除、覆盖或手工编辑 `data/memory.db`，这是用户真实记忆数据。
- 不要删除 `logs/` 里的日志，除非用户明确要求。
- 不要改 `.venv/`、`.pytest_cache/`、`__pycache__/`、`memory_gateway.egg-info/`。
- 不要手工编辑或提交 `ui/node_modules/`、`ui/dist/`。
- 不要把真实 API key、Tailscale 地址或私人路径写进公开文档。
- 不要随意改变记忆保存门槛，尤其是敏感信息、假设场景和 `source_quote` 校验。
- 不要把 MCP 的 DNS rebinding 设置改回默认，当前服务需要被局域网/iPhone 访问，鉴权由 `MCPAuthMiddleware` 负责。
- 不要把软删除改成硬删除，除非用户明确要求设计变更。
- 不要把上游超时/5xx 伪装成普通 ignore；ingest 应返回 `retryable=true`，同时不能污染正常记忆数据。

## 后续开发注意事项

- 新增记忆字段时，同步更新 `models.py`、`store.py`、REST 返回、MCP 返回、导出恢复和测试。
- 新增 MCP 工具时，同步更新 `EXPECTED_TOOLS` 相关测试和 README。
- 修改 REST 鉴权时，同步检查 MCP 鉴权，因为 MCP 子应用不走 FastAPI dependency。
- 修改搜索排序时，重点跑 `tests/test_memory_search.py` 和 MCP 搜索相关测试。
- 修改 `usage_count`、`mark_memories_used` 或 Time Ripple 时，确认默认 `TIME_RIPPLE_DELTA=0.0` 无副作用，且敏感/归档/钉选记忆不会被邻近激活。
- 修改客户端接入文案、MCP instructions 或记忆提取 prompt 时，同步检查 `README.md`、`docs/client_integration.md`、`app/mcp_server/server.py` 和 `app/llm/prompts.py`，保持“提交原文、不猜 temporal key、activation_count 不是精确次数”的口径一致。
- 修改保存门槛时，重点跑 `tests/test_memory_extraction.py` 和 `tests/test_mcp_server.py`。
- 修改召回评测时，保持 `k<=20` 与真实搜索上限一致，并确认快照临时文件在过滤成功后才原子发布。
- 修改核心记忆整理时，重点跑 `tests/test_core_memory.py`。
- 修改 `app/memory/utils.py` 里的共享工具函数时，要同时考虑搜索、提取、解析、体检、核心记忆和 prompt 注入路径，并优先跑相关定向测试后再跑完整 `pytest`。
- 修改响应格式时，确保 `tests/test_response_charset.py` 和相关 REST/MCP 测试仍通过。
- 修改空间、主题、实体、导出恢复或网络图过滤时，重点跑 `pytest tests/test_memory_store.py tests/test_memory_management.py tests/test_memory_network.py tests/test_mcp_server.py`。
- 修改配置项时，同步更新 `app/config.py`、`.env.example`、README 的配置表和安装说明。
- 修改 Windows 服务端口、NSSM 路径或访问脚本时，同步更新 README 的 Windows 服务模式和故障排查。
- 修改 MCP 工具、REST 端点、记忆字段、保存门槛或当前限制时，同步更新 README 和本文件，避免下一位 agent 读到旧契约。
- 测试应继续使用 fake LLM，不要引入真实网络调用。
- 当前仓库可能存在用户未提交改动。修改前先看 `git status --short`，不要回滚用户改动。
