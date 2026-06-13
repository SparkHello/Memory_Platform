# memory-gateway

`memory-gateway` 是一个给 iOS AI 客户端（如 Kelivo）提供长期记忆的 FastAPI 服务，支持两种接入方式：

1. **MCP 模式（推荐）**：作为远程 MCP 服务器（Streamable HTTP）暴露 `search_memory` / `save_memory` 等记忆工具。Kelivo 直连各家模型 API（流式、换模型都不受影响），模型在对话中按需调用工具完成记忆的检索与保存（RAG）。
2. **网关模式**：兼容 OpenAI Chat Completions API。客户端把本服务当上游使用，服务在中间自动注入记忆、调用上游模型、回答后后台提取记忆。`stream=true` 暂未实现。

两种模式共享同一套记忆库与「不乱记」校验逻辑，可同时开启。

## 功能

- MCP 端点：`POST /mcp`（Streamable HTTP，无状态），提供 `search_memory` / `save_memory` / `list_memories` / `delete_memory` 四个工具
- OpenAI 风格聊天接口：`POST /v1/chat/completions`
- 长期记忆列表：`GET /memories`（不返回向量字段）
- 长期记忆搜索：`POST /memories/search`
- 软删除记忆：`DELETE /memories/{memory_id}`
- 记忆决策日志：`GET /memories/decision-logs`，调试「为什么记了 / 为什么没记」
- SQLite 存储
- embedding 搜索优先，关键词搜索 fallback
- 聊天模型和 embedding 模型使用独立站点、独立 API Key
- iOS 客户端只持有 `GATEWAY_API_KEY`，不接触上游模型密钥

## 记忆管理逻辑

目标：**不乱记**。只保存长期有用、用户明确表达过、未来回答可能用到的信息。

### Memory Extractor（提取）

模型回答完成后，后台任务调用上游模型分析本轮对话，要求输出严格 JSON：

```json
{
  "action": "create | update | ignore",
  "memory": "记忆内容",
  "type": "project | preference | fact | learning | style",
  "importance": 7,
  "confidence": 0.9,
  "reason": "为什么要保存或忽略",
  "source_quote": "来自用户原话的短引用"
}
```

模型输出之后，代码层还会硬性校验一遍，全部满足才允许写库：

| 规则 | 说明 |
| --- | --- |
| `action` 是 `create` 或 `update` | `ignore` 直接忽略 |
| `importance >= 6` | 临时情绪、玩笑闲聊、一次性任务不保存 |
| `confidence >= 0.8` | 猜测、推断不保存 |
| `memory` 非空 | |
| `source_quote` 必须是用户原话的逐字片段 | 防止模型自己编造依据 |
| 引用所在句子不含假设表达 | 命中「如果 / 假如 / 假设 / 比如我用 / suppose / if I use / imagine / let's say」一律不保存 |

例如：「如果我以后用 Mac，应该怎么配置？」不会被存成「用户使用 Mac」；
而「我现在用 iPhone 和 Kelivo 做 AI 客户端。」可以存成「用户使用 iPhone，并在尝试用 Kelivo 作为 AI 客户端前端。」

提取模型输出非法 JSON 时不会影响聊天接口，只会留下一条 ignore 决策日志。

### Memory Resolver（解析落库）

保存前先和已有记忆比对：

1. 没有相似旧记忆，创建新记忆。
2. 内容完全相同（或已有更完整版本），忽略，不重复创建。
3. 新信息补充了旧记忆的细节，更新旧记忆，而不是新建。
4. 与旧记忆冲突时，只有用户明确表达的新事实（已通过上面的校验门槛）才覆盖更新；猜测、假设、玩笑到不了这一步。
5. 更新时保留旧记忆的 `created_at`，只刷新 `updated_at`。

相似判断优先用 embedding 余弦相似度（阈值 0.80），没有向量时退化为词重叠。

### 决策日志（memory_decision_logs）

每次提取后，无论最终是 `create`、`update` 还是 `ignore`，都会写一条决策日志，包含候选 JSON、决策和原因：

```
GET /memories/decision-logs?conversation_id=xxx&limit=100
```

聊天请求体里可附带 `conversation_id` 字段，会一并写入决策日志，方便按会话排查。

## 安装

需要 Python 3.12。

```bash
cd memory-gateway
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[dev]"
```

macOS 或 Linux 激活虚拟环境时使用：

```bash
source .venv/bin/activate
```

## 创建 .env

复制示例文件：

```bash
copy .env.example .env
```

macOS 或 Linux：

```bash
cp .env.example .env
```

然后编辑 `.env`：

```env
GATEWAY_API_KEY=给-iOS-客户端使用的网关密钥

# 聊天模型：智谱中国站点
UPSTREAM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
UPSTREAM_API_KEY=你的智谱API密钥
UPSTREAM_MODEL=glm-5.1

# Embedding：阿里云百炼中国站点
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_API_KEY=你的阿里云百炼API密钥
EMBEDDING_MODEL=text-embedding-v4
EMBEDDING_DIMENSIONS=1024

DATABASE_PATH=data/memory.db
```

注意：

- 聊天请求只使用 `UPSTREAM_BASE_URL`、`UPSTREAM_API_KEY`、`UPSTREAM_MODEL`。
- embedding 请求只使用 `EMBEDDING_BASE_URL`、`EMBEDDING_API_KEY`、`EMBEDDING_MODEL`、`EMBEDDING_DIMENSIONS`。
- 不要把智谱 API Key 和阿里云百炼 API Key 混用。
- 如果 `EMBEDDING_API_KEY` 没有配置，服务会自动退回关键词搜索 fallback。

## 运行服务

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl http://localhost:8000/health
```

聊天请求示例：

```bash
curl http://localhost:8000/v1/chat/completions ^
  -H "Authorization: Bearer 给-iOS-客户端使用的网关密钥" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"any-model\",\"messages\":[{\"role\":\"user\",\"content\":\"我喜欢黑咖啡，请记住。\"}],\"temperature\":0.7}"
```

macOS 或 Linux 可以把换行符 `^` 换成 `\`。

## Windows PowerShell 中文请求建议

在 Windows PowerShell 里测试中文请求时，建议先用 hashtable 构造对象，再用 `ConvertTo-Json` 和 `UTF8.GetBytes` 生成 UTF-8 请求体。不要直接把包含中文的 JSON 字符串写进 `-Body`，否则本地 shell 或终端编码可能先把请求体弄坏。

```powershell
$headers = @{
  Authorization = "Bearer 给-iOS-客户端使用的网关密钥"
  "Content-Type" = "application/json; charset=utf-8"
}

$body = @{
  model = "any-model"
  messages = @(
    @{
      role = "user"
      content = "我喜欢黑咖啡，请记住。"
    }
  )
  temperature = 0.7
} | ConvertTo-Json -Depth 10

$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)

Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/v1/chat/completions" `
  -Headers $headers `
  -Body $bytes
```

后端已经按 UTF-8 JSON 处理请求和响应；如果请求体本身没有被客户端或 shell 提前破坏，返回内容不应该出现中文乱码。

## 运行测试

```bash
pytest
```

测试不依赖任何外部服务（上游模型与向量服务都被替换为测试桩）：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_memory_extraction.py` | 保存门槛（低重要性 / 假设场景 / 编造引用不保存）、去重、补充更新、冲突处理、非法 JSON 容错、决策日志 |
| `tests/test_chat_gateway.py` | 聊天接口鉴权、记忆注入、reasoning 字段剥离、流式 501 |
| `tests/test_llm_client.py` | 上游调用的编码处理 |
| `tests/test_memory_store.py` / `tests/test_memory_search.py` | 存储与检索 |
| `tests/test_mcp_server.py` | MCP 端点鉴权、initialize、工具调用、保存门槛、去重、用户隔离 |
| `tests/test_response_charset.py` | 响应头必须带 `charset=utf-8`（兼容 Windows PowerShell 5.1 等旧客户端） |

## MCP 模式（推荐 Kelivo 使用）

MCP 端点：`http://你的服务器:8000/mcp`，传输方式为 Streamable HTTP（无状态 + JSON 响应，对移动端弱网最友好）。

### 工具一览

| 工具 | 作用 |
| --- | --- |
| `search_memory(query, limit)` | 按主题检索相关记忆，embedding 优先、关键词 fallback |
| `save_memory(memory, type, importance, confidence, source_quote, reason)` | 保存记忆。服务端硬校验 + 自动去重/更新，被拒时返回原因 |
| `list_memories(limit)` | 全量列出记忆，用于「你记住了我什么」 |
| `delete_memory(memory_id)` | 软删除，用于「忘掉这件事」 |

`save_memory` 在服务端保留与网关模式一致的「不乱记」门槛：`importance >= 6`、`confidence >= 0.8`、`source_quote` 非空且不含假设表达（如果 / 假如 / suppose / let's say…），未过门槛只写决策日志不落库。落库前仍走 Resolver 去重：内容相同忽略、同主题更新旧记忆。与网关模式的差别是 `source_quote` 的逐字校验无法执行（服务端看不到完整对话）。

每次 `save_memory` 无论结果如何都会写决策日志，`candidate_json` 中带 `"source": "mcp"` 标记，可与网关模式的自动提取区分开。

### Kelivo 配置

在 Kelivo 的 MCP 设置中添加服务器：

- URL：`http://你的服务器:8000/mcp`（公网部署建议套 HTTPS）
- 传输类型：Streamable HTTP
- 请求头：
  - `Authorization: Bearer <GATEWAY_API_KEY>`
  - `X-User-Id: 你的用户标识`（可选，不传按 `default` 用户）

然后在助手的系统提示词中加入记忆使用规范（模型是否主动调工具主要由提示词决定）。下面这份模板面向日常聊天场景——关键是让模型明白：闲聊中自然流露的信息也要记，不需要用户说「记住」：

```text
你可以使用长期记忆工具（search_memory / save_memory / list_memories / delete_memory）：

【检索】聊到与我有关的事——喜好、习惯、家人朋友、宠物、健康、计划安排、工作，或我们之前聊过的话题——先调用 search_memory 再回答，让对话自然延续。

【保存】我在闲聊中自然流露的长期信息也要保存，不需要我说「记住」。值得保存的例子：
- 喜好与雷点：口味、饮食禁忌、喜欢或讨厌的音乐 / 影视 / 游戏 / 事物
- 生活事实：所在城市、作息习惯、宠物、正在养成的习惯
- 重要的人：家人朋友的称呼、和我的关系、对他们重要的日子
- 目标与计划：在学的东西、健身目标、旅行计划
- 工作与项目：职业、正在做的事
这类对我长期成立的信息 importance 给 6-8，重大信息（家人、健康、原则性偏好）给 8-10；confidence 由我亲口说出的给 0.9；source_quote 摘抄我的原话片段。

【不要保存】当下情绪、玩笑反讽、一次性安排（「今晚吃火锅」）、假设话题（「如果我以后…」）、还没定下来的想法。保存被拒绝时按返回原因处理，不要换说法重试。

【删除】我要求忘记某件事时，先查到对应记忆的 id，再调用 delete_memory。

自然地运用记忆，不必每次向我汇报你查了或存了什么。
```

服务端也会通过 MCP 的 `instructions` 字段下发一份简版规范，支持该字段的客户端会自动注入。

### 选哪种模式

| | MCP 模式 | 网关模式 |
| --- | --- | --- |
| 流式输出 | 跟随客户端直连，天然支持 | 暂未实现 |
| 模型切换 | 任意，记忆功能跟着走 | 锁定 `UPSTREAM_MODEL` |
| 记忆召回/保存 | 模型主动调用工具，受提示词影响 | 服务端 100% 注入与提取 |
| 聊天流量 | 不经过本服务 | 全部经过本服务 |

## iOS 客户端配置（网关模式）

在 Kelivo 这类支持 OpenAI-compatible API 的 iOS 客户端中：

- Base URL: `http://你的服务器:8000/v1`
- API Key: `.env` 中的 `GATEWAY_API_KEY`
- Model: 任意模型名，服务端会映射到 `UPSTREAM_MODEL`

客户端请求头中的 `Authorization: Bearer <GATEWAY_API_KEY>` 只用于访问本项目。真正的 `UPSTREAM_API_KEY` 和 `EMBEDDING_API_KEY` 只在服务端使用。

如果需要区分用户，可以额外传入请求头：

```http
X-User-Id: user-123
```

不传时默认使用 `default` 用户。

## 当前限制

- 网关模式 `stream=true` 暂未实现（MCP 模式不受影响，流式由客户端直连模型完成）。
- 网关模式的记忆提取每轮对话会额外调用一次上游模型（temperature 为 0）。
- MCP 模式下记忆的召回与保存依赖模型主动调用工具，效果受助手系统提示词影响。
- embedding 存储仍使用 SQLite 中的 JSON 数组字符串，暂不引入向量数据库。
