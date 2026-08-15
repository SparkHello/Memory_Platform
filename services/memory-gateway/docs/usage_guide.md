# memory-gateway 使用教程

本教程面向首次部署者，覆盖：第一次启动如何配置、如何运行服务、常用终端命令。
命令以 macOS / Linux（bash）为准；Windows 用户请参考 `README.md` 中的 PowerShell 写法。

## 1. 环境要求

- Python 3.12 或更高版本
- Node.js 18+（仅构建 Web 控制台时需要）
- 一个上游 OpenAI 兼容聊天模型的 API Key（用于 `/v1` 对话代理、记忆提取、核心记忆整理和 AI 体检）
- 可选：一个 OpenAI 兼容 embedding 服务的 API Key（不配则自动回退关键词检索）

## 2. 第一次启动：安装与配置

### 2.1 安装后端依赖

```bash
cd /path/to/memory-gateway

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2.2 创建配置文件

```bash
cp .env.example .env
```

编辑 `.env`，逐项说明如下：

```env
# 【必填】本服务的访问令牌，AI 客户端、REST、Web 控制台共用它。
# 未配置时所有受保护接口返回 500。请改成足够长的随机字符串。
GATEWAY_API_KEY=change-me

# 【默认启用】OpenAI Chat Completions 透明记忆代理。
CHAT_GATEWAY_ENABLED=true
CHAT_GATEWAY_DEFAULT_MEMORY_MODE=read-write
CHAT_GATEWAY_SEARCH_LIMIT=8
CHAT_GATEWAY_CONTEXT_MAX_CHARS=12000
CHAT_GATEWAY_RECALL_TIMEOUT_SECONDS=4
CHAT_GATEWAY_STREAM_READ_TIMEOUT_SECONDS=600
CHAT_GATEWAY_STREAM_WRITE_TIMEOUT_SECONDS=120
CHAT_GATEWAY_TURN_TTL_SECONDS=3600
CHAT_GATEWAY_EXTRACTION_CONTEXT_TURNS=2
CHAT_GATEWAY_EXTRACTION_CONTEXT_MAX_CHARS=8000
CHAT_GATEWAY_CONTEXT_COMPACT_AFTER_TURNS=8
CHAT_GATEWAY_CONTEXT_COMPACT_AFTER_CHARS=6000
CHAT_GATEWAY_COMPACTED_SUMMARY_MAX_CHARS=4000

# 【必填】中央 Model Gateway。/v1 对话代理、记忆提取、核心记忆整理、AI 体检、
# 知识代理与 embedding 都只调用它的稳定 route；两项必须成对配置。
# 未配置时 /readyz 返回 model_runtime_configuration_error，依赖模型的功能不可用，
# 但手动保存、检索、管理功能仍可用。渠道、模型与价格在 Model Gateway 侧管理。
MODEL_GATEWAY_BASE_URL=http://127.0.0.1:2030/v1
MODEL_GATEWAY_API_KEY=your-local-model-gateway-client-key

# 【安全边界】是否允许把本地判定为 private/sensitive 的文本发给远程模型。
# 默认 false，只有确认 provider 获准处理敏感数据时才改 true。
ALLOW_SENSITIVE_EGRESS=false

# 【可选】向量检索。在 Model Gateway 配好 memory.embedding route 后，填写它声明的
# 精确空间 ID 与维度；留空或与空间/维度 Header、实际向量长度不匹配时安全回退关键词/FTS。
MODEL_GATEWAY_EMBEDDING_MODEL=memory.embedding
MODEL_GATEWAY_EMBEDDING_SPACE_ID=
EMBEDDING_DIMENSIONS=1024

# 【一般保持默认】
DATABASE_PATH=data/memory.db     # SQLite 数据库路径，首次启动自动建库建表
KNOWLEDGE_DATABASE_PATH=data/knowledge.db
KNOWLEDGE_MAX_DOCUMENT_BYTES=52428800
KNOWLEDGE_EMBEDDING_BATCH_SIZE=20
KNOWLEDGE_EMBEDDING_MIN_COSINE=0.25
KNOWLEDGE_HYBRID_VECTOR_WEIGHT=0.65
EVAL_DIR=eval                    # 召回评测工作区（含真实数据快照，勿提交 git）
REQUEST_TIMEOUT_SECONDS=60       # 上游请求超时
```

完整配置表见 `README.md` 的「配置项」一节和 `app/config.py`。

### 2.3 构建 Web 控制台（可选但推荐）

```bash
cd ui
npm install
npm run build
cd ..
```

构建产物在 `ui/dist/`，后端会自动把它挂载到 `/ui`。不构建也能跑后端，只是没有 Web 界面。

## 3. 运行服务

### 3.1 启动

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 2026
```

开发时加 `--reload` 让代码改动自动生效：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 2026
```

`--host 0.0.0.0` 表示监听所有网卡，局域网或 Tailscale 上的 MCP 客户端（包括手机端 Kelivo）才能访问；只在本机用可改成 `--host 127.0.0.1`。

### 3.2 验证

```bash
curl http://localhost:2026/health
# 期望返回 {"status":"ok"}
```

| 用途 | URL |
| --- | --- |
| 健康检查（无需鉴权） | `http://localhost:2026/health` |
| Web 控制台 | `http://localhost:2026/ui` |
| MCP 端点（AI 客户端接入） | `http://localhost:2026/mcp` |
| OpenAI-compatible Base URL | `http://localhost:2026/v1` |
| REST 管理接口 | `http://localhost:2026/memories/*` |

### 3.3 调用受保护接口

除 `/health` 外，所有接口都需要两个请求头：

```http
Authorization: Bearer <你的 GATEWAY_API_KEY>
X-User-Id: default
```

`X-User-Id` 是用户隔离边界，不传时默认 `default`。示例——搜索记忆：

```bash
curl -X POST http://localhost:2026/memories/search \
  -H "Authorization: Bearer change-me" \
  -H "X-User-Id: default" \
  -H "Content-Type: application/json" \
  -d '{"query": "咖啡"}'
```

### 3.4 首次使用 Web 控制台

打开 `http://localhost:2026/ui`，在「设置/接入信息」页填写：

- API Base URL：`http://localhost:2026`（手机访问则填局域网/Tailscale 地址）
- 访问密钥：与 `.env` 里的 `GATEWAY_API_KEY` 相同
- 用户 ID：`default`

连接信息只保存在浏览器 localStorage，不会写回服务端 `.env`。

在「对话上下文」页可以查看 FLIT `/v1` 聊天形成的自动分支树、压缩摘要和最近两轮原文。
搜索命中子节点时页面会自动展开完整路径；“清理此分支”会让该节点及其全部后代停止参与
后续自动匹配，但不会删除长期记忆，也不会改动客户端中的聊天记录。清理采用软删除，
可把状态切换到“已清理”后恢复整条分支。

在「知识库」页可直接上传 UTF-8 TXT/Markdown、PDF、DOCX 或 EPUB，也可粘贴正文。
PDF 必须自带可提取文本层，扫描件需先 OCR。导入时可填写标签和标量 JSON 元数据；
敏感级别以用户选择为准，但若本地规则判断更高，系统会先停止导入并显示警告，只有用户点击
“确认按所选级别导入”后才会写入，同时保存这次确认记录。
「知识检索调试」页可按这些字段限定范围，并显示 FTS/embedding 混合召回信号。

「用量与费用」页改为展示 Model Gateway 的用量汇总：`/v1` 聊天、记忆后台任务、
知识代理和 embedding 按实际渠道、provider/model、Token 和价格归账，以
`modelgw usage summary` 为权威，并按当前用户的归因标签隔离。本版本起 Memory
不再本地记录计量事件；旧版本保存的本地历史事件仍留在本地数据库中，不反向估算，
也不保存提示词或回复正文。

### 3.5 接入支持远程 Streamable HTTP 的 MCP 客户端

在客户端的 MCP 配置中填：

- URL：`http://<主机地址>:2026/mcp`
- Header：`Authorization: Bearer <GATEWAY_API_KEY>`、`X-User-Id: default`

本服务不依赖 Kelivo；Kelivo 只是兼容客户端之一。其他客户端只要支持远程 Streamable HTTP MCP 并能设置 Bearer 请求头，也可使用同一配置。只支持本地 `stdio` 或无法设置请求头的客户端需要 MCP 转接层。

推荐的系统提示词片段见 `README.md` 的「MCP 工具」一节和 `docs/client_integration.md`。

### 3.6 接入 FLIT（原 LastChat Plus）

在 FLIT 的 OpenAI-compatible Provider 中填写：

- Base URL：`http://<主机地址>:2026/v1`
- API 路径：`/chat/completions`
- API Key：`GATEWAY_API_KEY`
- 模型：`memory-auto`
- Provider 中关闭 Responses API；在助手的模型设置中保持“流式输出”开启（默认已开启）
- 自定义 Header：`X-User-Id: default`

同步模型后，进入“设置 → 提供商 → 当前 Provider → 编辑 `memory-auto` 模型”，设置：

- 输入模态：“文本 + 图片”；输出模态：“文本”
- 能力：“工具 + 推理”

这是 FLIT 的客户端侧开关，标准 `/v1/models` 无法替它声明。不打开时 FLIT 不会发送工具/推理字段，图片也可能先走 OCR 而不是原图透传。

不要添加第二个 `Authorization` Header，也不要给所有聊天设置相同的静态 `X-Conversation-Id`。FLIT 不会动态发送本地会话 ID；不设置时，网关会用它回传的可见用户/助手历史匹配本地分支节点。正常续聊接到原分支，修改旧消息或重新生成回答会形成独立分支，不会混合滚动摘要。

默认 `read-write` 会自动检索/注入安全记忆，并在完整最终回复后提取、去重和嵌入新长期记忆。提取时以最后一条用户文本为唯一事实来源，同时附带最近两轮可见对话消歧；system、工具内容和 reasoning 不会进入提取上下文。依赖上下文的候选必须同时通过本轮 `source_quote` 和较早 `context_quote` 校验，所以“前文问年龄、本轮回答 18”可保存，孤立的“18”会忽略。每个完整回答会保存本地分支节点；较早对话在后台压缩成滚动摘要，节点保留“摘要 + 最近两轮”。压缩摘要只能辅助理解，不能作为 `context_quote` 授权保存。如果客户端既没有动态 `conversation_id` 又截断了用于指纹匹配的旧历史，本轮会从请求自带上下文保守重建，而不会猜测其他分支。

可用静态或按请求 Header `X-Memory-Mode: read` 关闭自动写入，或用 `off` 作为纯代理。多模态、tools、上游 reasoning 响应和 SSE 会透明转发；图片与音频数据不会送入记忆 embedding。网关会按实际上游处理 BigModel/Mistral 的 `stream_options` 差异，并在进程内短暂缓存、恢复 FLIT 使用 `memory-auto` 时省略的工具推理状态；历史 reasoning 无法证明属于当前 provider 时会在转发上游前清除，避免故障切换时跨 provider 泄露。`ALLOW_SENSITIVE_EGRESS=false` 时，敏感历史只保存在本地，不会发送给记忆提取或上下文压缩 provider。

## 4. 终端命令速查

以下命令都假设在项目根目录、且已 `source .venv/bin/activate`。

### 4.1 服务与测试

```bash
# 启动服务（生产）
uvicorn app.main:app --host 0.0.0.0 --port 2026

# 启动服务（开发，自动重载）
uvicorn app.main:app --reload --host 0.0.0.0 --port 2026

# 健康检查
curl http://localhost:2026/health

# 跑全部测试（使用 fake LLM 和临时数据库，不发真实网络请求）
pytest

# 定向测试示例
pytest tests/test_mcp_server.py          # MCP 工具与鉴权
pytest tests/test_memory_extraction.py   # 保存门槛 / source_quote / 敏感信息
pytest tests/test_memory_store.py        # SQLite schema、迁移、CRUD
pytest tests/test_memory_search.py       # 搜索排序、embedding fallback
pytest tests/test_chat_gateway.py tests/test_openai_gateway_client.py tests/test_chat_streaming.py  # /v1 + FLIT
pytest tests/test_knowledge_import.py tests/test_knowledge_retrieval.py  # 文件解析与混合检索
pytest tests/test_model_usage.py       # Token、价格、故障切换实际归账和用户隔离
```

### 4.2 前端

```bash
cd ui
npm install        # 首次安装依赖
npm run build      # 构建到 ui/dist（后端挂载到 /ui）
npm run dev        # Vite 开发服务器（前端调试用）
```

### 4.3 真实数据库只读巡检（不写库）

```bash
# 结构/残留/统计巡检，人类可读输出
python scripts/audit_memory_db.py --database data/memory.db --env-file .env

# JSON 输出（给脚本消费）
python scripts/audit_memory_db.py --database data/memory.db --json

# 机制健康诊断：扇区分化、生命周期、Temporal KG、图结构是否被真实数据激活
python scripts/diagnose_memory_health.py --database data/memory.db
python scripts/diagnose_memory_health.py --database data/memory.db --user-id default --json
```

### 4.4 历史记忆分类回填（会写库，先 dry-run）

```bash
# 1. 先预览统计，不写库
python scripts/backfill_memory_classification.py --database data/memory.db --dry-run

# 2. 确认后执行；脚本会自动先生成 data/memory.backup.<timestamp>.db
python scripts/backfill_memory_classification.py --database data/memory.db

# 可选：只处理某个用户 / 限制条数
python scripts/backfill_memory_classification.py --database data/memory.db --user-id default --limit 50
```

### 4.5 微型召回评测（真实库全程只读）

```bash
# 1. 建立按用户隔离的评测快照和标注脚手架（写入 eval/）
python scripts/eval_recall.py --init --database data/memory.db

# 2. 编辑 eval/labels.jsonl，为每个 query 标注 relevant_ids

# 3. 跑评测（关键词检索）
python scripts/eval_recall.py --run

# 用 embedding 跑、输出 JSON、调整 top-k（1-20）
python scripts/eval_recall.py --run --use-embedding --json --k 8
```

同一套诊断/评测在 Web 控制台的「评测闭环」页也能做，标注和结果都只留在本地 `eval/`。

### 4.6 Windows 专属脚本（macOS/Linux 不适用）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-service.ps1     # 注册为 Windows 服务
powershell -ExecutionPolicy Bypass -File scripts\show-access-urls.ps1    # 查看 LAN / Tailscale 地址
powershell -ExecutionPolicy Bypass -File scripts\uninstall-service.ps1   # 卸载服务
```

## 5. 常见问题

- **接口返回 500 / 401**：检查 `.env` 是否配置了 `GATEWAY_API_KEY`，以及请求头 `Authorization: Bearer ...` 是否一致。
- **记忆没有被自动提取**：确认 `MODEL_GATEWAY_BASE_URL` / `MODEL_GATEWAY_API_KEY` 成对配置且 `memory.extract` route 可用（可用 `memgw doctor` 检查），并到“决策日志”查看 `model_reason_code`；空候选会显示临时事项、假设、非用户陈述、敏感授权不足、无长期价值或未分类等受控原因码，自由文本理由仍保持脱敏。敏感文本在 `ALLOW_SENSITIVE_EGRESS=false` 时不会发给远程提取，属预期行为。
- **搜索只有关键词效果**：`MODEL_GATEWAY_EMBEDDING_SPACE_ID` 未配置，或与 Model Gateway 返回的空间/维度 Header、实际向量长度不匹配时，记忆与知识库都会安全回退关键词/FTS 检索，属预期行为；知识状态接口会显示 embedding 是否启用。
- **扫描 PDF 无法导入**：当前只读取 PDF 自带文本层，请先用 OCR 工具生成可搜索 PDF。
- **AI 客户端连不上 `/mcp`**：确认服务用 `--host 0.0.0.0` 启动、端口 2026 未被防火墙拦截，且客户端带了 Bearer token。
- **FLIT 连不上或不流式**：Base URL 应以 `/v1` 结尾，API 路径使用 `/chat/completions`，Responses API 必须关闭；API Key 填本地 `GATEWAY_API_KEY`，助手模型的“流式输出”保持开启。
- **FLIT 看不到工具、推理或原图能力**：编辑 `memory-auto`，把输入模态设为“文本 + 图片”、输出模态设为“文本”，并开启“工具 + 推理”；这些能力不会由标准模型列表自动识别。
- **FLIT 能聊天但没有语义记忆**：确认 `X-Memory-Mode` 不是 `off`，并在 Model Gateway 配好 `memory.embedding` route 后填写 `MODEL_GATEWAY_EMBEDDING_SPACE_ID`；未配置时仍会使用关键词召回，自动保存也仍可工作。
- **“用量与费用”金额少于 provider 账单**：页面只累加同时具有上游 `usage` 和明确官方单价的调用，并且不含历史调用、套餐、赠金、折扣或账户优惠。先查看“计费完整度”和最近调用中的“缺少 usage / 待定价”状态。
- **`data/memory.db` 是真实数据**：不要手工编辑或删除；测试和脚本用的是临时库，不会污染它。
