# Kelivo / iOS Client Integration

This guide is for AI clients that connect to memory-gateway through MCP or REST.
The default recommendation for Kelivo is MCP: let the model call the six memory
tools, and let the server extract structured memory fields.

## MCP Client Rules

Use the `/mcp` endpoint with the shared gateway key:

```http
Authorization: Bearer <GATEWAY_API_KEY>
X-User-Id: default
```

Recommended system-prompt policy:

```text
你可以使用 memory-gateway 的长期记忆 MCP 工具：search_memory、surface_memories、submit_memory_text、get_core_memory、get_recent_context_summary、update_recent_context_summary、digest_memories。

- 当用户问题涉及个人背景、偏好、习惯、长期项目、关系、健康、计划、过去对话，或回答需要个性化上下文时，先调用 search_memory，再结合结果回答。
- 新对话开始、用户让你主动回顾近况，或没有明确检索词但需要唤起重要长期事项时，调用 surface_memories。mode 可选 balanced、important、emotional、stale、review_due；敏感记忆默认不浮现。
- 需要了解用户稳定背景时，调用 get_core_memory；需要接续最近对话上下文时，调用 get_recent_context_summary。
- 对话推进几轮或话题收束时，可调用 update_recent_context_summary 提交短期摘要；它不是长期记忆，也不会进入核心记忆。
- 积累一定新记忆或新对话开始需要自省整理时，先调用 digest_memories 获取未消化记忆；形成 reflection/feel 后，再次调用并传入 source_ids、reflection、feel、resolved_ids。source_ids 必须来自第一阶段结果，resolved_ids 必须是其子集。
- 用户本轮自然流露了长期有用、未来可能反复有帮助的信息时，调用 submit_memory_text，把用户原文完整传给 text。
- 不要拆分、改写、总结用户原文，也不要自行猜 type、importance、confidence、valid_from、temporal_subject 或 temporal_predicate。服务端会自动提取、去重和保存。
- 用户明确说“记住”“别忘了”“以后记得”时，优先调用 submit_memory_text。
- 检索旧记忆和提交新记忆是两个独立判断；同一轮都需要时，先 search_memory，再 submit_memory_text。
- 当下情绪、玩笑、一次性安排、假设场景、无长期价值的信息不要提交记忆。敏感信息只有在用户明确希望保留且未来明显有用时才提交。
- 只有用户本轮明确要求读取相关敏感信息时，才可给 search_memory/surface_memories 传 include_sensitive=true。
- submit_memory_text 返回 retryable=true 时说明上游暂时失败，可稍后重试一次；规则拒绝或无长期价值不应重试。
- 服务端默认 `ALLOW_SENSITIVE_EGRESS=false`，本地检测到敏感原文时不会发送给远程提取或 embedding provider。
- 服务端会验证候选的逐字引用、事实锚点和否定一致性；敏感保存授权只作用于敏感事实所在句子或子句。
- 搜索/浮现结果里的 activation_count 只表示活跃度，不是精确搜索次数；Time Ripple 是默认关闭的实验能力，普通客户端不需要启用。
- 用户要求忘记、删除或管理记忆时，你没有删除或遗忘的工具；引导用户在 Web 管理台（/ui/）操作。
- 除非记忆操作失败或用户明确询问，不主动提及工具调用过程。
```

## Temporal Key Rules

MCP clients normally do not fill temporal fields. Submit the user's original text
through `submit_memory_text`; the server-side extraction prompt decides whether a
temporal key is safe. The server also applies a small whitelist post-processor so
obvious profile facts can activate Temporal KG without letting the model invent
arbitrary predicates.

Only direct REST saves or upstream structured-save integrations should fill these fields:

- `valid_from`: the date or datetime when the fact starts being true.
- `temporal_subject`: the stable subject that can have the same property replaced later.
- `temporal_predicate`: the replaceable property name. For extraction-driven saves,
  this must be one of `current_employer`, `current_city`, `primary_ai_client`,
  `primary_device`, or `preferred_name`.

Use temporal keys only when all of these are true:

- The fact is explicit in the user's words.
- The fact is a current or state-like property that a later fact can replace.
- The subject and predicate are stable enough for exact matching.
- The predicate is on the profile whitelist above.

When uncertain, set `valid_from`, `temporal_subject`, and `temporal_predicate` to `null`.

Good temporal-key examples:

| User statement | Memory shape |
| --- | --- |
| “我从 2026 年开始在 Acme 工作。” | `temporal_subject="用户"`, `temporal_predicate="current_employer"`, `valid_from="2026-01-01"` if the start date is explicit enough. |
| “我现在主要用 Kelivo 当 AI 客户端。” | `temporal_subject="用户"`, `temporal_predicate="primary_ai_client"`, `valid_from=null` unless the user gives a start time. |
| “以后叫我阿澈。” | `temporal_subject="用户"`, `temporal_predicate="preferred_name"`, `valid_from=null`. |

Do not use temporal keys for:

- Preferences: “用户喜欢黑咖啡。”
- One-off or historical events: “用户去年去过京都。”
- Open-ended experiences or reflections: “用户发现长文档最好先做提纲。”
- Vague or inferred facts: anything the model is guessing from context.

## Activation Count

The physical SQLite column is `usage_count`, but client-facing UI and copy should call it
`activation_count`. It measures how active a memory has been in retrieval/surfacing flows,
not an exact number of user-visible searches.

By default `TIME_RIPPLE_DELTA=0.0`, so no neighbor activation is added. Time Ripple is an
experimental compatibility feature; ordinary clients should leave it disabled. If it is
enabled for testing, search or explicit touch can add fractional activation to nearby
memories that share a space/topic and are close in time. Do not present this value as a
precise hit count.

## Read-Only Data Audit

Before or after real-data migrations, run the read-only audit script:

```powershell
.\.venv\Scripts\python.exe scripts\audit_memory_db.py --database data\memory.db --env-file .env
```

For machine-readable output:

```powershell
.\.venv\Scripts\python.exe scripts\audit_memory_db.py --database data\memory.db --json
```

The audit script opens SQLite in read-only mode, checks P0/P1 columns, reports legacy type
residue, invalid lifecycle states, JSON-column parse errors, temporal/supersession counts,
and Time Ripple configuration. It prints only `TIME_RIPPLE_*` values from configuration and
never prints gateway or provider keys.
