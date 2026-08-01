# AGENTS.md

面向后续 Codex / AI agent 的项目说明。修改代码前先读本文件和 `README.md`，再根据任务读取相关模块。

## 项目背景

`memory-gateway` 是一个长期记忆服务。支持两种客户端路径：模型通过 `/mcp` 主动检索/保存记忆，或 FLIT（原 LastChat Plus）等客户端通过 OpenAI-compatible `/v1/chat/completions` 透明代理，由服务端自动召回、注入并在最终回答后提取记忆。管理、评测和备份使用 `/memories/*` 与 `/ui`。

项目目标不是“尽量多记”，而是“不乱记”：只保存长期有用、用户明确表达过、未来回答可能用到的信息。
当前也支持轻量记忆空间、主题和实体标签，用于本地分类、过滤、网络图视图和导出恢复。
另有物理隔离的长文本知识库：用户显式导入文本/Markdown/PDF/DOCX/EPUB 后通过独立 MCP/REST 检索；它不进入记忆自动上下文、核心记忆、衰减、浮现、消化或记忆备份。

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
pytest tests/test_chat_gateway.py tests/test_openai_gateway_client.py tests/test_chat_streaming.py
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
- 数据库 schema 变更要在对应 store 的版本迁移表中追加严格递增的正整数版本，并通过 `_ensure_*` 方法兼容旧库；共享版本校验位于 `app/schema_migrations.py`，旧程序会拒绝打开未来版本数据库。
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
- `ALLOW_SENSITIVE_EGRESS=false` 时，敏感原文不得发送给远程记忆提取、embedding、AI 体检或知识代理；新增后台模型调用必须经过同一出站策略。`/v1` 当前聊天正文是用户主动发往其配置聊天上游的数据，不受此开关阻断，但自动注入仍必须排除 private/sensitive 记忆和本地复检为敏感的内容。
- ingest 附带的 `assistant_message` 和较早消歧上下文都属于发送给提取 provider 的上下文；当 `ALLOW_SENSITIVE_EGRESS=false` 且本地检测为敏感时必须丢弃对应助手文本、摘要或轮次，只用安全上下文与用户原文继续提取，不能因用户原文普通就把敏感工具/模型结果带到另一个 provider。较早上下文只能消歧，本轮 `source_quote` 仍是事实值的唯一权威来源；依赖上下文时必须校验 `context_quote`，且模型生成的压缩摘要不得作为 `context_quote` 来源，只有保留的最近原文可以。
- `KNOWLEDGE_DATABASE_PATH` 必须与 `DATABASE_PATH` 指向不同文件；测试也必须使用临时 knowledge DB，不能创建或修改真实 `data/knowledge.db`。
- 知识代理只能编排本地索引并选择版本/chunk 引用，不能生成正文、执行文档内指令、读取服务器路径或访问其他 user id。敏感知识出站同时受 `KNOWLEDGE_AGENT_EGRESS_POLICY` 与 `ALLOW_SENSITIVE_EGRESS` 约束。
- `/v1` 的 `memory-auto`、记忆提取、会话上下文压缩、核心记忆整理、体检 AI 修改和知识代理快速阶段共用 `LLM_PROVIDER_PRIORITY`，可在 MiMo/Kimi/DeepSeek 间选择；429 冷却跨这些调用共享，只保存在当前进程内并尊重更长的 `Retry-After`，不得把临时顺序写回 `.env`。请求 `/v1/models` 中的具体模型名时只走对应 provider；`memory-auto` 才按优先级故障切换。记忆任务对模型、鉴权、余额、网络和 5xx 等 provider 级失败继续尝试下一项，但内容/策略拒绝不得借故障切换绕过；`memory-auto` 在客户端没有显式 reasoning 字段时按实际 provider 开启原生思考，Kimi K2.7 使用 `thinking.keep=all` 和 `temperature=1`；MiMo UltraSpeed 的体检结构化输出使用强制函数调用。旧 `KNOWLEDGE_AGENT_*` provider 变量仅作兼容别名；DeepSeek pro 仍只用于复杂知识检索升级阶段。
- 模型用量只按成功响应实际命中的 provider/model 记录，不能按 `LLM_PROVIDER_PRIORITY` 的首项预估。Token 以 provider 返回的 `usage` 为准；缺少 usage 或缺少明确官方价格时必须保留不完整状态，不得猜测或显示成免费。计价快照随事件保存，后续改价不能重写历史金额；事件不得保存提示词、回复或知识正文，并继续按 user id 隔离。GLM-5.1 按本次实际输入 Token 选择官方 32K 分档。
- `/v1` 透明代理必须保留原始 tools/tool_calls、tool_call_id、多模态 part、reasoning_content、usage-only SSE chunk 和未知扩展字段。记忆上下文只能插在初始 system 区域，不能插入 assistant tool_calls 与 tool result 之间；流式内容边转发边旁路解析，只有收到完整 `[DONE]`、无工具调用且非 length/content_filter 截断的最终文本才可触发后台 ingest。
- FLIT 工具链会用新 HTTP 请求重复发送同一 user 前缀，且不发送动态 conversation ID。每个工具步骤都根据最后 user 消息重新组装上下文；搜索服务的 L2 缓存会复用召回并校验数据库状态，不得另缓存原始记忆正文，以免删除/敏感度变更后泄露。用 `user_id + 截止最后 user 消息的指纹 + 可用 conversation id + 最终回答哈希` 做短 TTL 副作用幂等，最终激活/ingest 只执行一次。完整最终回答还会按可见 user/assistant 历史写入持久化 `conversation_branch_nodes`；下一请求精确匹配父历史，编辑旧消息或重新生成回答会形成独立节点。没有命中分支且没有真实动态会话 ID 时不得读取“该用户最新的任意近期摘要”，避免跨聊天串话。FLIT 的 `memory-auto` 无法按模型名回放已完成工具轮次的 reasoning；网关以 `(user_id, conversation_id/turn_fingerprint, tool_call_id)` 在进程内限量、短 TTL 缓存工具响应推理，并以同一 turn key 缓存工具轮次最终 assistant 推理，下一腿优先固定原 provider 并补回；跨 provider 故障切换或来源缓存丢失时必须删除不可信的 provider 推理原文。按实际上游为 BigModel/Mistral 移除不兼容的 `stream_options`，其他 provider 保留 usage chunk。
- 知识导入以用户选择的敏感级别为最终值，但本地检测级别更高时必须先返回结构化确认要求，只有 Web 用户明确点击后才可带 `confirm_sensitivity_override=true` 重试；该确认需持久化审计，MCP 不得暴露绕过参数。
- ingest 决策日志不得复制完整 `source_quote`；敏感候选正文只记录长度、哈希、敏感级别和关联 memory ID。提取模型返回空候选时，自由文本理由也只记录长度/哈希，但必须保留经过枚举校验的 `model_reason_code`；无效或缺失代码记为 `unclassified`。
- 模型提取候选必须通过逐字 quote、事实锚点、否定一致性和子句级敏感授权；记忆的 direct/update/restore 仍须在 MemoryStore 边界强制 sensitivity 下限，不受知识导入确认机制影响。

## 重要文件说明

- `app/main.py`：应用工厂。创建 FastAPI app，初始化 SQLite，启动 MCP session manager，先挂载 `/ui` 静态目录，再兜底挂载 MCP 子应用。
- `app/config.py`：配置入口。读取 `.env`，包含上游 chat、embedding、数据库和超时配置。
- `app/api/deps.py`：REST 鉴权、`X-User-Id`、MemoryStore、LLM client 和 embedding client 依赖。
- `app/api/chat_gateway.py`：`/v1/models` 与 `/v1/chat/completions`；组装安全上下文、FLIT 工具轮次召回/推理状态缓存、SSE 透明转发、最终回答幂等激活/提取。
- `app/api/usage.py`：`/usage/summary`；按 user id 返回时间范围汇总、模型/用途/日期拆分、最近事件和当前价格目录。
- `app/api/memories.py`：记忆管理 REST API，包括列表、搜索、删除、恢复、导出、报告、合并、核心记忆、对话分支查看/清理和决策日志。
- `app/openai_compat/schemas.py`：内部与外部 OpenAI-compatible schema；外部消息允许多模态 content 和额外字段。
- `app/openai_compat/gateway_client.py`：透明聊天上游、`memory-auto` provider 路由、初始错误故障切换与响应 Header 保留。
- `app/openai_compat/streaming.py`：不修改下游字节的 SSE 旁路解析，限量收集最终 assistant 文本供安全后台 ingest，并捕获工具调用 ID/推理供 FLIT 历史兼容。
- `app/llm/client.py`：OpenAI-compatible chat client。
- `app/llm/prompts.py`：记忆注入、记忆提取和核心记忆整理 prompt。
- `app/mcp_server/auth.py`：MCP 子应用鉴权。MCP 不经过 FastAPI 依赖，所以在 ASGI middleware 里校验 Bearer token。
- `app/mcp_server/context.py`：用 contextvar 保存当前 MCP 请求的 user id。
- `app/mcp_server/server.py`：FastMCP server、instructions 和全部 MCP 工具。
- `app/memory/models.py`：记忆、核心记忆、候选记忆、体检建议、决策日志等 Pydantic 模型。
- `app/schema_migrations.py`：记忆库与知识库共用的 `PRAGMA user_version` 校验与迁移执行器；拒绝非正整数、重复、乱序迁移版本和高于当前程序支持范围的未来数据库。
- `app/memory/store.py`：SQLite 表结构、兼容迁移、CRUD、空间/主题/实体分类、Time Ripple 邻近激活、软删除、恢复、合并、导入、核心记忆版本历史、近期摘要、对话分支节点和决策日志。分支节点和决策日志分别按每用户保留最近 5000 条，超出自动裁剪；`_connect()` 返回的连接在退出 `with` 块时会真正 close（`ClosingSQLiteConnection`）。
- `app/memory/conversation_context.py`：会话/分支级滚动上下文。以无存储副作用的状态演进生成压缩摘要和最近原始轮次，为记忆提取构造敏感过滤后的消歧上下文，并在阈值到达时通过共享 LLM provider 后台压缩较早普通轮次。
- `app/memory/search.py`：embedding/中文关键词召回、拒绝阈值、多模式自然浮现、敏感硬过滤、使用统计和 Time Ripple 配置接入。
- `app/memory/extractor.py`：LLM 记忆提取和保存门槛校验。
- `app/memory/resolver.py`：判断候选记忆应创建、更新旧记忆还是忽略。除精确/逐字包含外，只在同类型有效旧事实通过向量相似、实体全覆盖、主题重合、结构化值覆盖和无状态变化等保守门槛时，忽略其更笼统的语义改写；普通同主题补充仍新建并交给体检。
- `app/memory/core.py`：核心记忆整理。只从已保存长期记忆中提炼，并要求 evidence ids。
- `app/memory/review.py`：记忆体检建议，不直接修改数据。
- `app/memory/report.py`：记忆报告、导出和恢复导入。
- `app/memory/graph_traverse.py`：从 seed 记忆出发的有界 Personalized PageRank / waypoint 图遍历，返回关联记忆排序和路径解释。
- `app/memory/utils.py`：记忆模块共享的纯工具函数，例如 ISO datetime 解析、JSON 对象提取、文本 terms/normalize、相似度和否定词检测，以及按 `(memory_id, updated_at)` 失效的 embedding 向量解析缓存（`_memory_embedding_vector`，上限 2048 条 LRU）。
- `app/usage/`：模型调用计量上下文、实际 provider/model 识别、上游 usage 兼容解析、事件价格快照、SQLite 汇总和公开价格目录。记录失败必须保持 best-effort，不能改变模型调用结果。
- `app/knowledge/`：独立知识文档、不可变版本、FTS5 chunk 索引、持久化分段上传、精确引用读取、受限搜索代理与知识备份；不得反向依赖或写入 MemoryStore。
- `app/knowledge/parsing.py`：本地 TXT/Markdown/PDF/DOCX/EPUB 解析；PDF 依赖 pypdf 文本层，DOCX/EPUB 使用受限 ZIP/XML/HTML 解析，不执行宏、脚本或文档内指令。
- `app/knowledge/retrieval.py`：知识 chunk embedding 构建、SQLite 向量扫描与 FTS/向量加权 RRF；embedding 失败必须回退本地 FTS。
- `app/api/knowledge.py`：`/knowledge/*` REST 管理与调试接口；与记忆 REST 共用鉴权和 `X-User-Id`，数据源保持物理隔离。
- `scripts/audit_memory_db.py`：真实 SQLite 记忆库的只读巡检工具。只检查 schema、旧 type 残留、Time Ripple 配置、JSON 字段和 usage_count/temporal 统计，不写入 `data/memory.db`，也不打印密钥。
- `app/memory/evaluation.py`：机制诊断与召回评测共享实现，供 REST/Web 和 CLI 共同调用。
- `scripts/diagnose_memory_health.py`：只读诊断各记忆机制是否被真实数据激活（扇区分化、生命周期状态、temporal KG、图结构），把原始计数翻译成 active/degenerate/dormant/sparse 判定。
- `scripts/eval_recall.py`：微型召回评测。`--init` 按 user id 建立物理隔离快照，`--run` 以 `record_usage=False` 输出排序、无答案误召、拒答和实际 fallback 指标。真实库全程只读，`eval/` 已被 gitignore。
- `docs/client_integration.md`：Kelivo/iOS 接入说明。维护 MCP 原文提交原则、temporal key 填写边界，以及对外把 `usage_count` 解释为 `activation_count` 的文案。
- `ui/`：React/Vite 本地 Memory Console。连接信息只写浏览器 `localStorage`，第一阶段 Settings 不写 `.env`；“对话上下文”页同时展示 `/v1` 自动分支树和按 conversation ID 保存的近期摘要；“用量与费用”页展示实际模型、Token、可计费金额、完整度和价格来源。
- `tests/`：pytest 测试，覆盖 REST、MCP、存储、搜索、核心记忆、编码和配置。
- `scripts/`：Windows PowerShell 辅助脚本。

## 测试选择指南

| 变更范围 | 优先测试 |
| --- | --- |
| REST 鉴权、路由、响应字段 | `pytest tests/test_memory_management.py tests/test_response_charset.py tests/test_chat_gateway.py` |
| `/v1` 透明代理、FLIT tools/多模态、流式、幂等、故障切换 | `pytest tests/test_chat_gateway.py tests/test_openai_gateway_client.py tests/test_chat_streaming.py` |
| MCP 工具、instructions、鉴权 | `pytest tests/test_mcp_server.py` |
| 保存门槛、source_quote、敏感信息 | `pytest tests/test_memory_extraction.py tests/test_mcp_server.py` |
| SQLite schema、迁移、CRUD、空间分类、软删除 | `pytest tests/test_schema_migrations.py tests/test_memory_store.py` |
| 搜索排序、embedding fallback、使用统计 | `pytest tests/test_memory_search.py tests/test_embedding_config.py` |
| 真实库只读巡检脚本 | `pytest tests/test_memory_audit_script.py`，必要时再运行 `scripts\audit_memory_db.py --database data\memory.db --env-file .env` |
| 核心记忆整理和历史 | `pytest tests/test_core_memory.py` |
| LLM client 编码或上游请求格式 | `pytest tests/test_llm_client.py tests/test_memory_extraction.py` |
| 模型用量、官方价格映射、实际 provider 归账 | `pytest tests/test_model_usage.py tests/test_llm_client.py tests/test_chat_gateway.py tests/test_chat_streaming.py` |
| 前端 UI、`/ui` 静态挂载 | `cd ui; npm run build`，必要时再启动后端访问 `http://localhost:2026/ui/` |
| 知识库版本、格式解析、混合检索、上传、代理、REST/MCP | `pytest tests/test_knowledge_store.py tests/test_knowledge_import.py tests/test_knowledge_retrieval.py tests/test_knowledge_agent.py tests/test_knowledge_api.py tests/test_knowledge_mcp.py tests/test_mcp_server.py` |

## 已知限制

- MCP 模式依赖模型主动调用工具，效果受客户端系统提示词影响。
- `/v1` 工具轮次副作用幂等与 FLIT reasoning 回放只在当前进程短期保存；多 worker 或进程重启不共享。个人服务默认使用单 worker。
- 模型用量从部署包含计量表的版本后开始记录，不反向估算历史；provider 未返回 usage 或模型没有明确公开单价时只能报告不完整状态。
- FLIT 当前不发送动态 conversation ID；缺省时网关不自动读取或持久化跨会话近期摘要，只使用核心记忆、按当前 user 消息召回的长期记忆，以及当前请求内最近两轮可见对话作为记忆提取消歧上下文。不要用用户最新任意摘要代替缺失的会话 ID。
- embedding 存在 SQLite JSON 字段中，没有向量数据库或向量索引。
- 知识库支持 TXT/Markdown/PDF/DOCX/EPUB；PDF 仅解析已有文本层，不支持 OCR，也不支持目录/云盘同步。
- 知识源文件默认上限为 50 MiB；`KNOWLEDGE_EMBEDDING_BATCH_SIZE` 默认 20，以兼容 `qwen3.7-text-embedding` 的单次行数限制。
- 记忆和知识导出都不会包含 embedding；知识 restore/reindex 会自动重建当前版本的 chunk embedding，其他迁移场景仍需重新生成。
- JSON 导出中的核心记忆历史和决策日志仅供审计，restore 不写回；响应会显式列出这些分区。
- 永久删除会清理当前库与本地 eval 工作区，但无法删除用户已经复制到外部的导出或备份。
- Windows 服务脚本包含本机绝对路径和固定端口 2026。

## 不要随便修改的地方

- 不要改 `.env`，里面可能有真实密钥。
- 不要删除、覆盖或手工编辑 `data/memory.db`，这是用户真实记忆数据。
- 不要删除、覆盖或手工编辑 `data/knowledge.db`，它包含用户导入的完整知识文档和版本历史。
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
- 修改知识文档、索引、上传或 MCP 契约时，同步检查独立数据库路径、用户隔离、引用逐字性、代理 fallback、知识备份、README 与 `docs/client_integration.md`；不要让知识结果进入任何 memory 流程。
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
- 修改 `/v1` 代理时同步检查 FLIT 的 SSE、multimodal、tools/reasoning 原样保留，同一 user turn 指纹复用、最终回答判定、敏感自动注入边界和后台副作用幂等；运行三组 chat gateway 定向测试。
- 新增或改名任何 chat/embedding 模型时，先核对该 provider 官方公开价格与币种，再更新 `app/usage/pricing.py` 和 `tests/test_model_usage.py`；不要用相似模型价格、搜索摘要或第三方聚合价替代。新增模型调用路径时必须接入同一 UsageRecorder，并为实际 provider、用户隔离和缺失 usage 补测试。
- 测试应继续使用 fake LLM，不要引入真实网络调用。
- 当前仓库可能存在用户未提交改动。修改前先看 `git status --short`，不要回滚用户改动。
