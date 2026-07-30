# memory-gateway 使用教程

本教程面向首次部署者，覆盖：第一次启动如何配置、如何运行服务、常用终端命令。
命令以 macOS / Linux（bash）为准；Windows 用户请参考 `README.md` 中的 PowerShell 写法。

## 1. 环境要求

- Python 3.12 或更高版本
- Node.js 18+（仅构建 Web 控制台时需要）
- 一个上游 OpenAI 兼容聊天模型的 API Key（用于记忆提取、核心记忆整理、AI 体检）
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

# 【推荐】上游聊天模型，用于记忆提取、核心记忆整理、AI 体检。
# 不配则记忆自动提取不可用，但手动保存、检索、管理功能仍可用。
# DeepSeek、MiMo、Kimi 和支持 thinking 的智谱 GLM 会自动开启思考。
UPSTREAM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
UPSTREAM_API_KEY=your-upstream-api-key
UPSTREAM_MODEL=glm-5.1

# 【安全边界】是否允许把本地判定为 private/sensitive 的文本发给远程模型。
# 默认 false，只有确认 provider 获准处理敏感数据时才改 true。
ALLOW_SENSITIVE_EGRESS=false

# 【可选】embedding 服务。EMBEDDING_API_KEY 留空 = 使用关键词检索，不调用 embedding。
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=
EMBEDDING_MODEL=text-embedding-v4
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
TIME_RIPPLE_DELTA=0.0            # 实验性邻近激活，0.0 = 关闭，普通用户不要改
TIME_RIPPLE_WINDOW_HOURS=48
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

在「知识库」页可直接上传 UTF-8 TXT/Markdown、PDF、DOCX 或 EPUB，也可粘贴正文。
PDF 必须自带可提取文本层，扫描件需先 OCR。导入时可填写标签和标量 JSON 元数据；
敏感级别以用户选择为准，但若本地规则判断更高，系统会先停止导入并显示警告，只有用户点击
“确认按所选级别导入”后才会写入，同时保存这次确认记录。
「知识检索调试」页可按这些字段限定范围，并显示 FTS/embedding 混合召回信号。

### 3.5 接入支持远程 Streamable HTTP 的 MCP 客户端

在客户端的 MCP 配置中填：

- URL：`http://<主机地址>:2026/mcp`
- Header：`Authorization: Bearer <GATEWAY_API_KEY>`、`X-User-Id: default`

本服务不依赖 Kelivo；Kelivo 只是兼容客户端之一。其他客户端只要支持远程 Streamable HTTP MCP 并能设置 Bearer 请求头，也可使用同一配置。只支持本地 `stdio` 或无法设置请求头的客户端需要 MCP 转接层。

推荐的系统提示词片段见 `README.md` 的「MCP 工具」一节和 `docs/client_integration.md`。

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
pytest tests/test_knowledge_import.py tests/test_knowledge_retrieval.py  # 文件解析与混合检索
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
- **记忆没有被自动提取**：确认 `UPSTREAM_API_KEY` / `UPSTREAM_MODEL` 配置正确；敏感文本在 `ALLOW_SENSITIVE_EGRESS=false` 时不会发给远程提取，属预期行为。
- **搜索只有关键词效果**：`EMBEDDING_API_KEY` 为空时记忆与知识库都会自动回退关键词检索，属预期行为；知识状态接口会显示 embedding 是否启用。
- **扫描 PDF 无法导入**：当前只读取 PDF 自带文本层，请先用 OCR 工具生成可搜索 PDF。
- **AI 客户端连不上 `/mcp`**：确认服务用 `--host 0.0.0.0` 启动、端口 2026 未被防火墙拦截，且客户端带了 Bearer token。
- **`data/memory.db` 是真实数据**：不要手工编辑或删除；测试和脚本用的是临时库，不会污染它。
