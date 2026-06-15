# 需求 AI 对话记录

- 线程: thr_331gtu5l
- 更新时间: 2026-06-15T08:29:31.819Z

---

## 用户

我还有一个比较大的功能增加，就是依靠API，直接在电脑端进行记忆提交，核心记忆+RAG记忆命中，而不是依靠模型调用MCP，你觉得怎么样

## 需求 AI

让我先了解项目的现有架构和记忆相关的代码，再给出具体的建议。

> [工具] ls

> [工具] read

> [工具] ls

> [工具] read（error）

> [工具] read

> [工具] ls

> [工具] ls

> [工具] ls

> [工具] read

> [工具] read

> [工具] read

> [工具] read

> [工具] read

> [工具] read

> [工具] read

> [工具] read

> [工具] read

> [工具] read

> [工具] grep

> [工具] read

> [工具] read

## 需求 AI

现在我对整个项目有了清晰的了解。让我给出分析并写需求草稿。

核心发现：当前 REST API 已有 `POST /memories/ingest`（提交原文，LLM 提取）和 `POST /memories/search`（RAG 检索），但缺少两个关键能力：

1. **直接保存结构化记忆的 REST 端点**（MCP 有 `save_memory`，REST 没有对应物）
2. **一站式「核心记忆 + RAG 命中」聚合查询**（现在要分别调 3 个端点）

> [工具] write

## 需求 AI

需求草稿已写入。以下是我的分析总结：

---

## 核心判断：方向正确，且与现有架构高度契合

你的想法本质上是给 memory-gateway 补上**面向电脑端工具/脚本的 Local-first REST API**。好消息是基础设施全在，主要是补两个缺口：

### 现有能力对照

| 你想做的事 | 当前状态 |
|---|---|
| 提交原文让服务端自动提取记忆 | ✅ `POST /memories/ingest` 已支持 |
| **直接提交结构化记忆**（跳过 LLM） | ❌ MCP 有 `save_memory`，REST 没有 |
| RAG 检索 | ✅ `POST /memories/search` |
| 读核心记忆 | ✅ `GET /memories/core` |
| **一站式：核心记忆 + RAG + 近期上下文** | ❌ 需要分别调 3 个端点 |

### 建议新增的两个端点

1. **`POST /memories`** — 直接保存一条结构化记忆。行为与 MCP `save_memory` 完全一致（走同样的校验、去重、更新逻辑），但不经过 LLM 提取。适合 Alfred/Raycast/Obsidian/命令行等「明确知道要记什么」的场景。

2. **`POST /memories/context`** — 一站式上下文检索。电脑端 AI 工具在调模型之前，一次请求拿到核心记忆 + RAG 命中 + 近期摘要，直接拼入 system prompt。避免客户端自己串行调 3 个接口。

### 不做什么

- 不发明新的记忆存储模型，完全复用现有 `memories` 表
- 不改变 MCP 工具或网关行为
- 不做 GUI/浏览器插件，纯 HTTP API

---

草稿中有 3 个待澄清的问题，你觉得方向对吗？要不要我先调整草稿再开始实现？

---

## 用户

我原来的MCP方向有一些记忆管理机制你也可以看一下

> [工具] grep

> [工具] read

> [工具] read

> [工具] read

## 需求 AI

好的，让我仔细检查 MCP 侧所有工具与 REST 的对应关系。

> [工具] grep

## 需求 AI

发现一个重要遗漏。对照全部 17 个 MCP 工具，REST 侧缺的不止 `save_memory`，还缺 `forget_memories`（按自然语言搜索并批量软删除）。

现在更新草稿：

> [工具] write

## 需求 AI

需求草稿已更新。对照全部 17 个 MCP 工具后，发现两个关键点：

---

### 完整缺口

REST 侧实际只缺 **2 个已有 MCP 工具的对应端点**：

| MCP 工具 | 缺失的 REST 端点 | 核心行为 |
|---|---|---|
| `save_memory` | `POST /memories` | 跳过 LLM，直接保存已结构化的单条记忆 |
| `forget_memories` | `POST /memories/forget` | 按自然语言搜索 → 批量软删除 |

再加上你提出的聚合查询需求 `POST /memories/context`，总共 **3 个新端点**。

### 一个重要的校验差异

`save_memory`（MCP）和 `submit_memory_text`（MCP）走的是**不同的校验路径**，这一点在 REST 侧也需要保留：

- **ingest 路径** (`require_quote_in_user_message=True`)：`source_quote` 必须在用户原文中出现，防止模型编造
- **save 路径** (`require_quote_in_user_message=False`)：`source_quote` 只需非空，信任调用方自行保证真实性

电脑端 `POST /memories` 应该走 save 路径——你从 Alfred/Obsidian/命令行提交时，source_quote 就是你自己的原话，不需要再验证"是否出现在更大的原文中"。

---

3 个待澄清问题在草稿末尾，你看看方向对不对？

---

## 用户

按你的建议就行。到时候我需要保留MCP工具吗，或者是只留下重要的，其他的的给API转发过程中自行处理？API端需要整合怎样的记忆机制很重要，最好把复杂的都留给API这端，我IOS端MCP依赖模型调用，可能会不稳定，很依赖模型本身的理解能力

## 需求 AI

这是一个很重要的架构决策。让我分析清楚后再更新草稿。

核心矛盾：**模型调用 MCP 工具不稳定**，因为依赖模型正确理解「何时调用哪个工具、怎么填参数」。`save_memory` 就是个典型例子——模型自己填 importance/confidence/type 经常出错。

解决方案：**MCP 精简到只保留模型必须主动决策的 4 个工具，其余复杂度全部移到 REST API 端**。

> [工具] write

> [工具] read

## 需求 AI

草稿已更新。核心变化是新增了一个**架构决策**章节。总结关键点：

---

### MCP 从 17 个工具精简到 4 个

保留的 4 个原则是「模型只需要做判断，不需要做执行」：

| 保留 | 模型做的事 | 为什么安全 |
|---|---|---|
| `search_memory` | 判断「这个话题需要搜什么」 | 只需一句话描述，不涉及填字段 |
| `submit_memory_text` | 判断「这段话值不值得记」 | 只需丢原文，服务端 LLM 负责提取 |
| `get_core_memory` | 了解用户背景 | 无参调用 |
| `get_recent_context_summary` | 恢复上下文 | 几乎无参 |

移除的 13 个里最关键的三个：
- **`save_memory`** → 模型自己填 importance/confidence/type 经常翻车，统一走 `submit_memory_text` 让服务端提取
- **`forget_memories`** → 模型可能误判遗忘范围，批量删错风险大，走 Web UI 手动操作
- **`consolidate_core_memory`** → 高成本 LLM 调用，应定时触发而非模型决策

### Instructions 也会大幅精简

从 ~540 字 17 个工具的使用规则 → ~180 字 4 个工具，模型指令遵循负担降低 2/3。

### 实施顺序

1. 先补 REST API 缺口（3 个新端点）
2. 再精简 MCP（移除 13 个工具 + 更新 instructions + 更新测试）
3. 可选增强后续迭代

---

你觉得这个精简方案合理吗？有没有哪个 MCP 工具你觉得 iOS 端还是需要保留的？

---

## 用户

可以的，那API这边记忆机制是什么呢，因为要向我的提供商那边转发添加记忆的请求，按标准插入正确位置，插入哪些都需要考虑。现在分为核心记忆和记忆库，核心记忆是iOS端调用工具请求的，记忆库是模型提交查询内容，由电脑这边自动选择并返回的。那我们现在不依赖模型调用MCP提供搜索关键词了，怎么确保获得准确记忆呢？

## 需求 AI

这是一个关键问题。让我先看清楚当前网关模式是怎么做自动检索的——它其实就是用用户消息直接搜，不依赖模型生成关键词。

> [工具] read

> [工具] read

## 需求 AI

现在完全清楚了。关键发现：**网关模式就是用用户原文直接做 embedding 搜索，不依赖任何关键词提炼**。因为 embedding 是语义匹配，用户说「我最近在学 Rust」和记忆库里「用户正在学习 Rust 编程语言」在语义空间里天然接近，不需要中间人提炼关键词。

现在更新草稿，补充记忆检索机制和注入格式这两个核心设计：

> [工具] write

> [工具] read

## 需求 AI

草稿已更新，核心补充了三块内容：

---

### 1. 记忆检索机制：不需要关键词

网关模式已经证明了这个方案有效 — 用**用户消息原文直接做 embedding 语义搜索**。embedding 模型本身就是语义匹配器，「我最近在学 Rust」和记忆中「用户正在学习 Rust」在向量空间里天然接近，不需要任何人提炼关键词。

对比：
- MCP 模式：依赖模型生成 query → 不稳定
- 电脑端 API：用户消息原文 → embedding 搜索 → 语义匹配，忠实原意

核心记忆更是**始终全量返回**（6 个分区总量很小），完全不需要搜索。

### 2. 记忆注入格式：三段式 System Prompt

与网关模式 `_inject_memories()` 完全一致的格式：

```
[核心记忆] → [近期上下文] → [RAG 搜索结果] → [用户消息] → [模型回复]
```

`POST /memories/context` 的 `format: "markdown"` 直接返回可注入的文本，电脑端不需要自己拼接。内部复用 `render_core_memory_context()` + `render_memory_context()` + `render_recent_context_summary_context()` 三个已有函数。

### 3. 电脑端完整闭环

```
用户消息 → POST /memories/context → 注入 system prompt → 发上游模型
                                                              ↓
用户看到回复 ←────────────────────────────── 模型回复
                 POST /memories/ingest（后台提取保存）
```

两步 API 调用，电脑端工具不需要理解记忆结构。

---

你觉得这个检索机制和注入格式对吗？要我直接开始实现 Phase 1？

---

## 用户

还有一个，就是如果要让用户端这边看得到到底命中了那些记忆，可以在向iOS端返回的内容里插入伪装的工具调用，参数就是返回的记忆。但是以后我准备做流式输出的，怎么完善还得考虑。另外很重要的一点就是，优化命中缓存机制

> [工具] grep

> [工具] read

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] edit

## 需求 AI

已更新。新增了两个重要章节：

---

### 记忆命中可见性

核心思路是在模型回复正文前插入一个**伪装的工具调用结果块**：

```
【记忆命中】
🔍 search_memory("用户的开发环境偏好") → 3 条
1. 用户使用 Windows 11 + WSL2 作为开发环境 (相关度 0.94)
...
---
[模型正文回复...]
```

流式兼容方案：首个 SSE chunk 发 `event: memory_hit`（携带命中摘要），后续发正常的 `event: delta`。客户端渲染为可折叠引用块。检索在首个 chunk 前就完成了，不影响流式体验。

### 命中缓存机制

当前 `search()` 每次都要调 embedding API（最贵的操作，100-500ms），但连续对话中 query 高度相似。方案是两层内存缓存：

| 层 | 缓存什么 | TTL | 省什么 |
|---|---|---|---|
| L1 | query → embedding 向量 | 5分钟 | 省 embedding API 调用 |
| L2 | query → 记忆 id 列表 | 2分钟 | 省相似度计算 + SQLite 加载 |

L2 用 `memory_version` 做失效：每次记忆变更（创建/更新/删除/合并）时版本号递增，缓存自动失效。核心记忆变更不影响搜索缓存。

嵌入到 `MemorySearchService` 内部，对 MCP、REST、网关全部透明生效。

---

实施顺序也调整了：缓存提到 Phase 2（因为它让所有模式受益），MCP 精简挪到 Phase 3。

你看看这两块设计有没有要调整的？

---

## 用户

目前看可以的。你有什么修正吗，可能就是这个方案就是有问题的，毕竟我也是第一次做这个系统

## 需求 AI

让我仔细审视整个方案，发现了一个**严重的设计缺陷**和几个计数/细节问题。

> [工具] read

## 需求 AI

确认了问题。`get_memory_search_service` 每次请求都 `return MemorySearchService(...)` 创建新实例——文档中的两层缓存如果放在实例内部，每次请求都会清空，**缓存完全无效**。

同时已有端点数量也不对（文档写 15 个，实际 18 个）。还有其他几个细节问题。一起修正：

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] edit

> [工具] read

> [工具] grep

> [工具] grep

> [工具] grep

> [工具] read

> [工具] read

## 需求 AI

我发现了几个问题——之前的编辑中 `REST API 完整端点` 表格意外丢失了，而且架构图里写的 20 也不对（实际是 18+3=21）。让我做最终修正，同时补充 MCP 遗漏和同步/异步的注意事项。

> [工具] edit

> [工具] read

> [工具] edit

## 需求 AI

完成。总结我发现并修正的 4 个问题：

---

### P0：缓存实例化缺陷（已修正）

原方案把缓存放在 `MemorySearchService` 实例内部，但 FastAPI dependency 每次请求创建新实例 → **缓存永远为空**。修正为模块级变量（`app/memory/search.py` 顶层 `_EMBEDDING_CACHE` / `_SEARCH_CACHE` dict），L2 用 `SELECT MAX(updated_at)` 做版本比对而不是维护单独的 version 字段。

### P1：端点计数错误（已修正）

架构图写"20 个端点"，但实际已有 18 个 + 新增 3 个 = 21。

### P2：MCP 遗忘能力缺失（已补充）

移除 `forget_memories` 和 `delete_memory` 后 iOS 端完全无法执行遗忘。在 instructions 中加了引导语，并在「已知限制」中明确记录。

### P3：电脑端步骤 5 同步延迟（已补充）

`POST /memories/ingest` 是同步调用（等待 LLM 提取），会拖慢用户体验。建议 fire-and-forget 或改为后台任务。

---

方案整体是合理的，这些修正让它更可落地。要开始 Phase 1 实现吗？

---

## 用户

好的

## 需求 AI

需求文档已定稿。开始 Phase 1 实现。

> [工具] todo_write

## 需求 AI

先实现 `POST /memories` — 对齐 MCP `save_memory` 的 REST 版本。核心是把 MCP `save_memory` 的逻辑（构造 CandidateMemory → 校验 → resolve）搬到 REST 路由中。

> [工具] read

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] read

> [工具] read

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] edit

## 需求 AI

Now add the 3 new request models after `MemoryIngestRequest`:

> [工具] read

> [工具] read

> [工具] edit

## 需求 AI

Now add the 3 endpoint functions between review and patch:

> [工具] read

> [工具] read

> [工具] edit

## 需求 AI

有个小 bug：`search_results_raw` 在 markdown 分支中可能未定义。修正：

> [工具] read

> [工具] edit

## 需求 AI

验证没有导入错误：

> [工具] bash（error）

> [工具] bash（error）

> [工具] edit（error）

> [工具] read

> [工具] edit

## 需求 AI

需要把 `RecentContextSummary` 的 inline import 移到顶部：

> [工具] edit（error）

> [工具] read

> [工具] edit

> [工具] read

> [工具] edit

> [工具] bash

## 需求 AI

21 个路由全部注册成功。跑现有测试确认没有破坏已有功能：

> [工具] bash

## 需求 AI

现有测试全部通过。现在编写新端点的测试：

> [工具] read

> [工具] grep

> [工具] read

> [工具] write

> [工具] read

> [工具] read

> [工具] bash（error）

## 需求 AI

`MemoryForgetRequest` 的 `query` 有 `min_length=1`，但 MCP 的 `forget_memories` 对空 query 是返回空结果的。修正模型：

> [工具] read

> [工具] edit（error）

> [工具] edit（error）

> [工具] read

> [工具] edit（error）

> [工具] edit

> [工具] bash

## 需求 AI

18 个测试全部通过。跑完整测试套件确认没有回归：

> [工具] bash

> [工具] bash

## 需求 AI

全部 134 个测试通过，零回归。

> [工具] todo_write

## 需求 AI

Phase 1 完成。总结：

### 改动清单

**`app/api/memories.py`** — 新增 3 个 REST 端点：
| 端点 | 对齐 MCP | 功能 |
|---|---|---|
| `POST /memories` | `save_memory` | 跳过 LLM，直接保存结构化记忆 |
| `POST /memories/forget` | `forget_memories` | 按自然语言搜索 → 批量软删除 |
| `POST /memories/context` | 新聚合端点 | 核心记忆 + RAG 检索 + 近期上下文，支持 `format: json/markdown` |

**`tests/test_direct_memory_api.py`** — 18 个测试
全部通过，完整测试套件 134 个测试零回归。

### 快速验证

重启服务后可以用 PowerShell 测试：

```powershell
# 保存记忆
Invoke-RestMethod -Method Post -Uri http://localhost:2026/memories `
  -Headers @{Authorization="Bearer change-me"} `
  -Body '{"content":"用户使用Windows开发","type":"fact","importance":7,"confidence":0.95,"source_quote":"我用Windows开发"}' `
  -ContentType "application/json"

# 一站式上下文
Invoke-RestMethod -Method Post -Uri http://localhost:2026/memories/context `
  -Headers @{Authorization="Bearer change-me"} `
  -Body '{"query":"开发环境","format":"markdown"}' `
  -ContentType "application/json"
```

继续 Phase 2 命中缓存，还是先重启服务验证一下？

---

## 用户

重启过了

---

## 用户

生成实施计划

> [工具] Create plan

> [工具] read

## 需求 AI

计划已保存。总结：

### Phase 1 ✅ 已完成
3 个新端点 + 18 个测试，134 全量测试零回归。

### Phase 2-4 实施计划

| Phase | 内容 | 关键文件 | 步骤数 |
|---|---|---|---|
| **2** | 两层模块级缓存 | `app/memory/search.py` | 8 步 |
| **3** | MCP 17→4 工具精简 | `app/mcp_server/server.py` | 5 步 |
| **4** | 记忆命中可见性（非流式） | `app/openai_compat/chat.py` | 5 步 |

**Phase 2 核心**：在 `search.py` 顶层加 `_EMBEDDING_CACHE` 和 `_SEARCH_CACHE` 两个模块级 dict，改写 `search()` 方法，用 `max_updated_at` 做 L2 失效比对。

**Phase 3 核心**：保留 `search_memory`/`submit_memory_text`/`get_core_memory`/`get_recent_context_summary` 四个工具，删除其余 13 个的函数和注册，instructions 从 ~540 字精简到 ~180 字。

**Phase 4 核心**：需要先在 `MemorySearchService` 中增加带 score 的搜索方法，然后网关模式在 assistant content 前拼接「记忆命中」引用块。

要开始 Phase 2 吗？

---

## 用户

构建: .kunsdd/plan/sdd-19215b1e-58f6-4b9f-b84f-d2d4061271d5.md

> [工具] todo_write

## 需求 AI

开始 Phase 2。先读当前 `search.py` 完整内容：

> [工具] read

## 需求 AI

Phase 2 — 两步完成：先在 `MemoryStore` 加 `max_updated_at` 辅助查询，再重写 `search.py` 加入两层缓存。

> [工具] read

> [工具] grep

> [工具] read

> [工具] read

> [工具] edit

## 需求 AI

现在实现 `search.py` 缓存。需要重写 `search()` 方法并添加模块级缓存：

> [工具] edit（error）

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] edit（error）

> [工具] write

> [工具] bash

> [工具] read

> [工具] read

## 需求 AI

代码看起来正确。先跑现有测试确认无回归：

> [工具] bash

> [工具] bash

## 需求 AI

134 全部通过。现在写缓存测试 `tests/test_search_cache.py`：

> [工具] write

## 需求 AI

需要修正测试 —— limit 不同时 L1 embedding 缓存仍然命中：

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] edit

> [工具] bash（error）

## 需求 AI

模块级缓存在测试间残留导致失败。需要清理 fixture：

> [工具] read

> [工具] grep

> [工具] read

> [工具] edit

> [工具] bash（error）

## 需求 AI

L2 失效但 L1 embedding 缓存仍有效 —— 这是正确行为。修正测试断言：

> [工具] read

> [工具] edit

> [工具] edit（error）

> [工具] read

> [工具] edit（error）

> [工具] edit

> [工具] bash（error）

## 需求 AI

新记忆话题不匹配，搜「咖啡」当然不返回「喝茶」。修正测试：

> [工具] edit（error）

> [工具] read

> [工具] edit

> [工具] edit（error）

> [工具] bash（error）

> [工具] read

> [工具] edit

> [工具] bash

## 需求 AI

全部 11 个缓存测试通过。跑完整测试套件：

> [工具] bash

> [工具] bash

## 需求 AI

145 全通过。Phase 2 完成。进入 Phase 3 — MCP 精简。

> [工具] todo_write

> [工具] read

> [工具] edit

## 需求 AI

Now replace `SERVER_INSTRUCTIONS` and delete all 13 removed tool functions. Let me read the full file to write a clean version:

> [工具] read

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] edit

> [工具] grep

> [工具] read

> [工具] read

> [工具] edit（error）

> [工具] read

> [工具] write

> [工具] read

> [工具] bash

## 需求 AI

文件被 write 工具损坏了。从 git 恢复：

> [工具] bash

## 需求 AI

用 Python 脚本一次性完成所有 MCP 精简：

> [工具] bash（error）
