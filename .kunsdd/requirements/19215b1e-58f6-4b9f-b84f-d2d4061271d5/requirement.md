# 电脑端 Memory API — 记忆检索、注入与提交

## 背景

当前 memory-gateway 的记忆读写主要面向两个场景：

- **MCP 模式**：iOS 模型通过 `/mcp` 工具调用 17 个记忆工具（依赖模型判断何时调用、如何填参，不稳定）
- **网关模式**：客户端请求 `/v1/chat/completions`，服务端自动注入记忆 + 后台提取

存在两个问题：

1. 缺少面向**电脑端工具直接调用**的 REST API
2. **MCP 工具过多**，管理类操作依赖模型正确决策，在 iOS 端不稳定

## 架构决策：复杂度从 MCP 移到 REST API

```
              iOS 端（精简 MCP）              电脑端（完整 REST API）
              ┌──────────────┐              ┌──────────────────────┐
  用户 ──→ 模型 ──→ 4 个核心工具  ──→  memory-gateway  ←── 脚本/工具直接调 21 个端点
              │                              │
              │  只做模型必须决策的事:          │  做所有复杂的事:
              │  · 搜什么 (search)            │  · 精细保存 (POST /memories)
              │  · 该不该记 (submit)          │  · 批量遗忘 (POST /memories/forget)
              │  · 我是谁 (core)              │  · 一站式上下文 (POST /memories/context)
              │  · 上次聊了啥 (recent)        │  · 合并、体检、整理、导出...
              └───────────────────────────────┴──────────────────────┘
```

**原则**：模型只负责「判断」，服务端负责「执行」。模型判断「这段对话值得记住」→ 丢原文给 `submit_memory_text`，服务端 LLM 做提取、去重、落库。

---

## 记忆检索机制：为什么不需要模型生成关键词

网关模式（`/v1/chat/completions`）已证明：**用户消息原文直接作为 embedding 搜索 query，效果足够好**。

```
用户消息 ──→ embedding 向量 ──→ 与记忆库所有记忆做余弦相似度 ──→ 排序返回
```

embedding 模型本身就是语义匹配器。「我最近在学 Rust」和记忆中「用户正在学习 Rust 编程语言」在语义空间天然接近，不需要任何人（模型或用户）提炼关键词。核心记忆更是始终全量返回（6 个分区，每个几百字），完全不需要搜索。

**对比**：


| 方式      | 搜索 query                   | 依赖     | 准确性                    |
| ------- | -------------------------- | ------ | ---------------------- |
| MCP 模式  | 模型生成的关键词                   | 模型理解能力 | 不稳定（模型可能生成不准确的 query）  |
| 网关模式    | 用户最后一条消息原文                 | 无      | embedding 语义匹配，忠实于用户原意 |
| 电脑端 API | 用户消息原文（默认）/ 调用方可选传 `query` | 无      | 同上，且调用方可覆盖             |


---

## 记忆注入格式：三段式 System Prompt

电脑端工具从 API 拿到记忆后，需要在发给上游模型的请求中正确插入。标准格式（与网关模式 `_inject_memories` 一致）：

```
[System Message]
以下是关于当前用户的核心记忆。它们是从长期记忆中整理出的稳定背景，
优先级高于普通召回记忆；如果和用户最新消息冲突，以用户最新消息为准。

【稳定背景】
用户是自由职业者，居住在杭州。

【长期偏好与雷点】
- 偏好 Windows 开发环境
- 不喜欢喝咖啡，更爱喝茶
...

以下是近期会话摘要，仅用于延续最近话题；它不是长期记忆，也不代表稳定事实。
用户：我们上次讨论了 memory-gateway 的 API 设计
助手：建议新增 3 个 REST 端点...

以下是关于当前用户的长期记忆。请把它们当作上下文使用；
如果和本轮对话冲突，以用户最新消息为准。
1. 用户使用 Windows 11 + WSL2 作为开发环境
2. 用户正在用 Python FastAPI 构建后端服务
...

[User Message]
用户本轮的实际消息...

[Assistant Message]
模型的回复...
```

三段内容（核心记忆 → 近期上下文 → RAG 搜索结果）按优先级排列，核心记忆最前（最稳定），RAG 结果最后（最细粒度）。

---

## MCP 工具精简方案

### 保留 4 个核心工具


| MCP 工具                       | 保留理由               | 模型负担        |
| ---------------------------- | ------------------ | ----------- |
| `search_memory`              | 模型需要根据当前话题决定搜什么    | 低：一句话描述搜索主题 |
| `submit_memory_text`         | 模型判断用户是否提供了值得记住的信息 | 低：丢用户原文     |
| `get_core_memory`            | 模型需要了解用户稳定背景       | 极低：无参调用     |
| `get_recent_context_summary` | 恢复对话上下文            | 极低：几乎无参     |


### 移除 13 个工具（走 REST API / Web UI）


| 移除的工具                     | 移除理由                               | 替代方式                                            |
| ------------------------- | ---------------------------------- | ----------------------------------------------- |
| `save_memory`             | 模型填 importance/confidence/type 不稳定 | REST `POST /memories` 或统一走 `submit_memory_text` |
| `why_remember`            | 调试用途                               | REST `GET /memories/{id}/why`                   |
| `merge_memories`          | 模型容易误判合并时机                         | REST `POST /memories/merge` / Web UI            |
| `consolidate_core_memory` | 高成本 LLM 操作，应定时/手动触发                | REST `POST /memories/core/consolidate`          |
| `review_memories`         | 只返回建议不自动执行                         | REST `POST /memories/review` / Web UI           |
| `memory_report`           | 管理操作                               | REST `GET /memories/report`                     |
| `export_memories`         | 管理操作                               | REST `GET /memories/export`                     |
| `list_memories`           | 日常用 `search_memory`                | REST `GET /memories`                            |
| `list_deleted_memories`   | 管理操作                               | REST `GET /memories/deleted`                    |
| `delete_memory`           | 按 id 删，应先 search 确认                | REST `DELETE /memories/{id}`                    |
| `restore_memory`          | 管理操作                               | REST `POST /memories/{id}/restore`              |
| `forget_memories`         | 批量遗忘风险高，模型可能误判范围                   | REST `POST /memories/forget` / Web UI           |
| `get_core_memory_history` | 调试/审计用途                            | REST `GET /memories/core/history`               |


---

## 新增端点详设

### 1. `POST /memories` — 直接保存结构化记忆

跳过 LLM 提取，直接提交已结构化的记忆。对齐 MCP `save_memory`。

**请求体**：

```json
{
  "content": "用户使用 Windows 11 + WSL2 作为开发环境",
  "type": "fact",
  "importance": 7,
  "confidence": 0.95,
  "stability": "stable",
  "sensitivity": "normal",
  "source_quote": "我平时用 Windows 11 + WSL2 开发",
  "valid_until": null,
  "review_after": null
}
```

**校验链路**：

- `CandidateMemory(action="create", ...)` → `validate_candidate_for_save(candidate)`（不传 `user_message`，`source_quote` 只需非空）
- `MemoryResolver.resolve()` embedding 去重 → 同主题更新 vs 新建
- 写决策日志

**返回**：`{action: "create"|"update"|"ignore", relation, reason, memory_id}`

**与 `POST /memories/ingest` 的定位**：


|                  | `POST /memories/ingest` | `POST /memories` |
| ---------------- | ----------------------- | ---------------- |
| 输入               | 原始文本（用户原话）              | 已结构化的一条记忆        |
| LLM 调用           | 有（拆分提取）                 | 无                |
| source\_quote 校验 | 必须出现在原文中                | 只需非空             |
| 适用场景             | 不确定该记什么                 | 明确知道要记什么         |


### 2. `POST /memories/forget` — 按自然语言批量软删除

对齐 MCP `forget_memories`。

**请求体**：`{ "query": "关于咖啡的偏好", "limit": 5 }`

**行为**：`MemorySearchService.search(record_usage=False)` → 逐条 `archive_memory()`。`limit` 默认 5，最大 10。

### 3. `POST /memories/context` — 一站式上下文检索 + 格式化注入

电脑端 AI 工具的核心端点：一次请求拿到全部上下文，且可直接拼入 system prompt。

**请求体**：

```json
{
  "query": "用户最近的开发环境偏好",
  "include_core_memory": true,
  "include_recent_context": true,
  "search_limit": 5,
  "conversation_id": "optional-conv-id",
  "format": "json"
}
```

**`format: "json"` 响应**（结构化数据，调用方自行拼接）：

```json
{
  "core_memory": [{ "section": "preferences", "content": "..." }],
  "search_results": [{ "id": "...", "content": "...", "type": "fact", "importance": 7 }],
  "recent_context": { "found": true, "summary": "..." }
}
```

**`format: "markdown"` 响应**（可直接作为 system message 的 content 字段发给上游模型）：

```markdown
以下是关于当前用户的核心记忆...

【稳定背景】
用户是自由职业者，居住在杭州。

【长期偏好与雷点】
- 偏好 Windows 开发环境
...

以下是近期会话摘要...
用户：我们上次讨论了 memory-gateway
...

以下是关于当前用户的长期记忆...
1. 用户使用 Windows 11 + WSL2 作为开发环境
2. 用户正在用 Python FastAPI 构建后端服务
```

**行为**：

- 并行执行：核心记忆读取 + RAG 检索 + 近期上下文读取
- **RAG 检索**：`query` 不为空时用 query 做 embedding 搜索；`query` 为空但 `conversation_id` 对应的近期上下文存在时，用近期摘要的最后一条用户消息做搜索；都没有时跳过 RAG
- 不标记 `usage_count`（`record_usage=False`），由后续实际使用时的 `POST /memories/search` 负责
- `format: "markdown"` 复用 `render_core_memory_context()` + `render_memory_context()` + `render_recent_context_summary_context()`，与网关模式注入格式完全一致

---

## 电脑端完整交互流程

```
┌─────────────────────────────────────────────────────────────────────┐
│ 电脑端 AI 工具（如 Chatbox / 自建客户端 / 脚本）                       │
│                                                                     │
│  1. 用户输入消息 ──→                                                 │
│  2. POST /memories/context  { query: 用户消息, format: "markdown" } │
│     ←── 返回可直接注入的 system prompt 片段                            │
│  3. 构造上游请求:                                                     │
│     [system: 注入片段] + [user: 用户消息] + [history...]              │
│  4. 发送给上游模型 ──→ ←── 模型回复                                   │
│  5. POST /memories/ingest { text: 用户消息 }  // 后台提取+保存        │
│  6. 返回模型回复给用户                                                 │
└─────────────────────────────────────────────────────────────────────┘
```

步骤 2 和 5 是 memory-gateway 的价值所在：

- **步骤 2（读路径）**：核心记忆全量 + RAG 语义搜索，不依赖关键词提炼
- **步骤 5（写路径）**：提交用户原文，服务端 LLM 提取记忆（过滤、去重、落库）

电脑端工具不需要理解记忆结构，只需在调模型前调一次 `/memories/context`，模型回复后调一次 `/memories/ingest`。

---

## MCP Instructions 精简

**精简前**（\~540 字，17 个工具规则）→ **精简后**（\~180 字，4 个工具规则）：

> 这是用户的长期记忆服务。你只有 4 个工具：
>
> - **search\_memory**：回答问题前，如果话题涉及用户的喜好、习惯、家人、健康、计划或过去聊过的事，先搜索再回答。
> - **submit\_memory\_text**：如果用户本轮提供了值得长期记住的信息，把用户原文放入 text 提交。不要自己整理或改写，服务端会自动提取、去重和保存。
> - **get\_core\_memory**：需要了解用户稳定背景时调用。
> - **get\_recent\_context\_summary**：需要恢复上一轮上下文时调用。
> 不要保存假设、玩笑、一次性安排。同一轮可以先 search 再 submit，两者不冲突。保存被拒绝时不要重试。

---

## 技术约束

- 鉴权：`X-User-Id` + `Authorization: Bearer <GATEWAY_API_KEY>`
- `POST /memories` 复用 `validate_candidate_for_save()` + `MemoryResolver.resolve()`
- `POST /memories/forget` 复用 `MemorySearchService.search()` + `store.archive_memory()`
- `POST /memories/context` 复用 `render_*_context()` 三个 prompt 函数 + `MemorySearchService.search()`
- 数据库：不新增表
- MCP 精简：从 server.py 移除 13 个工具注册 + 对应函数 + 精简 instructions
- 测试：新增 `tests/test_direct_memory_api.py`；更新 `tests/test_mcp_server.py` 中 `EXPECTED_TOOLS` 从 17 → 4

---

## 记忆命中可见性：内联展示 MCP 工具命中

用户需要看到「模型用了哪些记忆来回答」。当前 MCP 模式下，模型调用 `search_memory` 后用户看不到实际命中了什么。

### 方案：在响应中插入内联记忆命中块

模型回复中，在正文前插入记忆来源引用块：

```
【记忆命中】
🔍 search_memory("用户的开发环境偏好") → 3 条
1. 用户使用 Windows 11 + WSL2 作为开发环境 (相关度 0.94)
2. 用户偏好深色主题的代码编辑器 (相关度 0.87)
3. 用户正在用 Python FastAPI 构建后端 (相关度 0.82)
---
[模型正文回复...]
```

**实现方式**：

- 非流式：直接在 assistant content 前拼接记忆命中块
- 流式（后续）：第一个 SSE chunk 作为 `event: memory_hit` 事件发送，后续 chunk 正常流式输出模型回复。客户端渲染为可折叠引用块

```
// SSE 流
event: memory_hit
data: {"tool": "search_memory", "query": "...", "count": 3, "results": [...]}

event: delta
data: {"content": "根据你的开发环境..."}
```

### 流式兼容设计

当前 `stream=true` 返回 501。后续实现流式输出时：

1. 记忆检索在首个 chunk 发出前完成（与当前非流式一致）
2. 首个 SSE 事件为 `memory_hit`，携带命中的记忆摘要
3. 后续 `delta` 事件正常流式输出模型回复
4. 客户端可将 `memory_hit` 渲染为可折叠引用块，不影响正文阅读体验

**适用范围**：

- 网关模式：自动注入的记忆在响应中展示命中块
- 电脑端 REST API：`POST /memories/context` 返回结构化数据，调用方自行决定展示方式
- MCP 模式：不强行改变（MCP 本身就能看到 tool result）

---

## 命中缓存机制

### 现状问题

`MemorySearchService.search()` 每次调用都：

1. 调 embedding API 生成 query 向量（网络调用，100-500ms）
2. 从 SQLite 加载全部 200 条记忆
3. 逐条解析 `embedding_json` + 计算余弦相似度

连续多轮对话中，同一话题的搜索 query 相似，embedding 结果高度重复。**无任何缓存**。

### 方案：两层模块级缓存

**关键约束**：`MemorySearchService` 由 FastAPI dependency 每次请求创建新实例（`app/api/deps.py:79`），缓存不能放在实例内部，必须用**模块级变量**（`app/memory/search.py` 顶层 dict），否则每次请求缓存都会被清空。

#### L1：Query Embedding 缓存（模块级）

```
位置:   app/memory/search.py 模块级 _EMBEDDING_CACHE dict
key:    (user_id, normalized_query)
value:  (expires_at, list[float])
max:    512 entries 全局
ttl:    5 分钟
```

- 相同 query 不重复调 embedding API（省去最贵的网络调用）
- `normalize_query` = 去空格、小写、截断至前 200 字符

#### L2：搜索结果缓存（模块级）

```
位置:   app/memory/search.py 模块级 _SEARCH_CACHE dict
key:    (user_id, normalized_query, search_limit)
value:  (expires_at, max_updated_at, [memory_id, ...])
max:    256 entries 全局
ttl:    2 分钟
```

- 相同 query + limit 直接返回缓存 id 列表
- 用 memory\_id 从 SQLite 重新加载完整 MemoryRecord（确保数据最新）
- `max_updated_at`：缓存时该用户所有记忆的最新 `updated_at`。每次命中时重新查询 `SELECT MAX(updated_at) FROM memories WHERE user_id=? AND archived=0`，与缓存值比对，不一致则失效

#### 失效策略


| 触发事件       | 失效范围                                             |
| ---------- | ------------------------------------------------ |
| 该用户任何记忆写操作 | L2 中该用户 entry 在下次搜索时自动失效（`max_updated_at` 比对不通过） |
| 核心记忆变更     | 不影响搜索缓存                                          |
| TTL 到期     | 自然淘汰，懒清理（搜索时发现过期即删除）                             |


**不需要额外 version 字段或 SQLite 表**。每次搜索时做一次轻量 `SELECT MAX(updated_at)` 即可。这比维护 version 计数器更简单且不需要修改 `MemoryStore` 的写路径。

**对调用方完全透明**：MCP、REST、网关模式全部自动受益。

---

## 已知限制与注意事项

### MCP 精简后的遗忘能力

移除 `forget_memories` 和 `delete_memory` 后，iOS 端模型无法执行遗忘操作。当用户说「忘掉关于咖啡的事」时，模型应引导用户到 Web UI 操作。精简后的 instructions 中应加上一句：

> 用户要求忘记某条信息时，请引导用户在 Web 管理台（`/ui/`）操作，你没有删除或遗忘的工具。

### 电脑端步骤 5 的同步延迟

当前草稿中电脑端流程的步骤 5（`POST /memories/ingest`）是同步调用，会等待 LLM 提取完成才返回，增加用户感知延迟。建议：

- 电脑端工具以 **fire-and-forget** 方式调用（发请求后不等待响应）
- 或者 memory-gateway 端改为后台任务模式（与网关模式的 `background_tasks.add_task` 一致）

### 单用户部署假设

两级缓存使用模块级 dict，在单进程部署下工作正常。如果将来用多 worker 部署（如 `uvicorn --workers 4`），每个 worker 有独立内存空间，缓存不共享。当前单用户场景不是问题。

---

## 已确认不在此范围

- 不改变 `/v1/chat/completions` 网关逻辑
- 不引入新 LLM 调用路径（`POST /memories` 和 `POST /memories/forget` 不调 LLM）
- 不做浏览器插件或桌面 GUI

---

## 可选增强（后续迭代）

- **增量核心记忆整理**：`POST /memories` 创建/更新后触发增量整理
- **`POST /memories` 支持批量**：一次提交多条已结构化记忆
- **流式输出 + memory\_hit 事件**：`stream=true` 时首个 SSE chunk 携带记忆命中

## 实施顺序

1. **Phase 1** ✅：新增 3 个 REST 端点 + 测试（已完成，18 tests pass）
2. **Phase 2** ✅：命中缓存（L1 embedding + L2 搜索结果）+ 测试（已完成，10 tests pass）
3. **Phase 3**：MCP 精简（17 → 4 工具 + 精简 instructions）+ 更新 MCP 测试
4. **Phase 4**：记忆命中可见性（非流式先做，流式后续）
5. **Phase 5**（可选）：流式输出、增量核心记忆整理、批量提交

