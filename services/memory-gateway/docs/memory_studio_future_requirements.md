# 记忆工作室路线图、已完成范围与后续实施说明

更新时间：2026-06-16

## 1. 当前审核结论

`memory-gateway` 继续定位为本地优先的长期记忆服务，主线由 SQLite、REST API、MCP 和 Web UI 组成。项目目标不是迁移到 Ombre-Brain，而是在现有架构中持续吸收更好的“记忆工作室”体验：记忆空间、情绪坐标、自然浮现、遗忘曲线、可解释召回、Markdown/Obsidian 友好表达，以及更保守的隐私与删除策略。

截至本次审核，阶段一到阶段五、阶段六一期、阶段六二期、阶段六三期均已完成并通过验证。系统已经具备长期记忆保存、搜索、召回、治理、软删除、恢复、永久删除、导出、恢复导入、隐私遮罩、核心记忆、空间分类、记忆网络、Obsidian 单向镜像和数据库健康报告闭环。

当前最建议推进的下一步是阶段六四期：人工视觉验收和移动端微调。阶段六三期数据库健康检查已经落地，后续不需要再围绕“发现孤立 evidence”做基础建设，只需要在 UI 验收和后续可选增强中继续保持“只报告、不自动修复”的安全边界。

## 2. 状态总览

| 阶段 | 状态 | 核心交付 | 审核结论 |
| --- | --- | --- | --- |
| 阶段一：记忆治理中心一期 | 已完成 | `/memories/review`、治理动作、审计日志、AI 修改预览 | 可继续作为治理基础；普通删除保持软删除 |
| 阶段二：召回解释与 Breath 面板一期 | 已完成 | `score_breakdown`、上下文解释、召回反馈记录 | 解释用于调试和可读性，不自动调权 |
| 阶段三：遗忘曲线和自然浮现深化 | 已完成 | 多模式 `/memories/surface`、生命力、复核信号 | 运行时评分不持久化 |
| 阶段四：记忆空间和分类工作台一期 | 已完成 | `memory_spaces`、主题/实体、网络过滤、导出恢复 | 空间是轻量组织结构，不是知识图谱 |
| 阶段五：Markdown/Obsidian 镜像一期 | 已完成 | JSON/Markdown/Obsidian zip 导出 | Obsidian 是单向镜像，不做双向同步 |
| 阶段六一期：敏感遮罩 | 已完成 | `redact_sensitive=true` 浏览响应遮罩 | 只影响响应，不改写 SQLite 原文 |
| 阶段六二期：回收站永久删除 | 已完成 | `DELETE /memories/deleted/{memory_id}/purge`、先审计后删除 | 只允许 purge 回收站记忆，不进入 MCP |
| 阶段六三期：数据库健康检查 | 已完成 | `GET /memories/health`、记忆体检页数据库健康区块 | 只读报告工具，不自动修复 |
| 阶段六四期：人工视觉验收和移动端微调 | 待实施 | 桌面/移动端关键页面人工验收 | 当前最高优先级 |

## 3. 已完成能力清单

### 3.1 数据模型与存储

已完成：

- 记忆基础字段：`content`、`type`、`importance`、`confidence`、`stability`、`sensitivity`。
- 时间字段：`valid_until`、`review_after`、`created_at`、`updated_at`、`last_used_at`。
- 召回字段：`usage_count`、`activation_count`、`freshness_bonus`、`final_score`。
- 情绪坐标：`valence`、`arousal`。
- 核心证据关系：`evidence_memory_ids`。
- 分类字段：`topics`、`entities`、`space_ids`。
- 空间表：`memory_spaces`、`memory_space_links`。
- 删除状态：软删除、恢复、回收站列表、永久删除。
- 审计动作：`memory_decision_logs.decision = purge`。

审核说明：

- 当前数据模型足够支撑已完成阶段，不需要为了短期展示能力新增主表字段。
- 空间采用轻量表，主题和实体采用 JSON 字段，符合“可过滤、可导出、低复杂度”的一期目标。
- 永久删除不新增数据库表或列，复用 `memory_decision_logs` 记录安全审计信息。
- 后续短期功能应优先复用现有字段；不要为了单一 UI 展示持久化运行时评分。

### 3.2 REST 与 MCP

已完成 REST 能力：

- 记忆列表、搜索、自然浮现、网络图、保存、忘记、上下文、召回解释、召回反馈、体检、治理动作、核心记忆、近期上下文、空间分类、导出恢复。
- `/memories/surface` 支持 `mode=balanced|important|emotional|stale|review_due`。
- `/memories/network` 支持按空间、类型、敏感度、情绪正向度和唤起度过滤。
- `/memories/export?format=json|markdown|obsidian_markdown` 支持 JSON、Markdown 和 Obsidian zip 导出。
- `redact_sensitive=true` 已进入列表、回收站、空间详情、单条读取、来源解释、搜索、浮现、网络图和召回解释。
- `DELETE /memories/deleted/{memory_id}/purge` 支持回收站永久删除。
- `GET /memories/health` 支持只读数据库健康检查，返回 `ok|warning|error`、汇总计数和结构化 issue 列表。

已完成 MCP 工具：

- `search_memory`
- `surface_memories`
- `submit_memory_text`
- `get_core_memory`
- `get_recent_context_summary`
- `update_recent_context_summary`

审核说明：

- REST 继续作为复杂管理、调试和 UI 入口。
- MCP 只暴露稳定、必要的 AI 客户端能力；敏感遮罩、永久删除和数据库健康检查不进入 MCP。
- 分类变更、治理动作和永久删除会写入审计日志；召回反馈只记录，不自动改变排序权重。

### 3.3 Web UI

已完成页面：

- 记忆工作室首页。
- 记忆库。
- 核心记忆。
- 记忆体检。
- 召回解释。
- 近期上下文。
- 报告与备份。
- 决策日志。
- 设置和接入信息。

已完成体验：

- 首页显示浮现记忆、情绪分布、空间概览和记忆网络。
- 浮现记忆支持模式切换，并展示可读原因、浮现分、生命力、最近活跃状态和复核信号。
- 记忆详情显示类型、重要度、置信度、情绪坐标、时间字段、敏感级别、证据 ID、主题、实体、空间、最近活跃状态和来源解释。
- 记忆详情支持编辑主题、实体和空间标签。
- 记忆库支持按类型、敏感级别、稳定性、空间、主题、实体、重要度、置信度、有效期和复核时间过滤。
- 回收站支持恢复和永久删除；永久删除使用独立危险按钮和确认弹窗。
- 永久删除确认弹窗展示完整 ID 和 8 位确认码，用户必须输入 `memory.id.slice(0, 8)` 才能提交。
- 记忆网络支持按空间、类型、敏感级别和情绪范围过滤。
- 报告与备份页支持下载 JSON、Markdown 和 Obsidian zip，恢复导入仍只接受 JSON。
- 记忆库、首页和召回解释默认使用遮罩视图；活跃记忆详情页可显式查看完整内容后再编辑。
- 报告与备份页明确提示导出文件包含完整私密/敏感正文。
- 决策日志页支持过滤 `purge` 永久删除记录。
- 记忆体检页显示数据库健康检查结果，默认聚焦错误和警告，提示项可手动展开查看。

审核说明：

- UI 已经从“后台管理”推进到“记忆工作室”形态。
- 后续阶段应继续保持密度和效率，不建议改成营销式 landing page 或重装饰页面。
- 永久删除入口已与恢复入口分离，符合高风险操作的交互边界。
- 仍需要人工视觉验收，重点检查移动端抽屉、标签编辑器、网络过滤器、空间概览、遮罩提示、导出按钮组和永久删除确认弹窗。

## 4. 本次验收记录

本次审核覆盖阶段六三期“数据库健康检查”，并回看阶段六二期“回收站永久删除”的兼容边界。

验证记录：

- `.\.venv\Scripts\python.exe -m pytest tests/test_memory_health.py`：9 passed。
- `.\.venv\Scripts\python.exe -m pytest tests/test_memory_health.py tests/test_memory_management.py tests/test_memory_store.py`：43 passed。
- `.\.venv\Scripts\python.exe -m pytest`：168 passed。
- `ui` 目录下 `npm.cmd run build`：通过。
- `git diff --check`：通过，仅有 Windows 行尾转换提示。

审核判断：

- REST 已支持敏感遮罩、永久删除和数据库健康报告；MCP 继续保持克制，不扩展高风险管理能力。
- 遮罩是响应期策略，不改写 SQLite 原文，不影响 JSON restore，不改变导出备份语义。
- 永久删除是不可恢复操作，但只允许作用于已软删除记忆，并在删除前写入审计日志。
- 核心记忆 evidence 引用不阻止永久删除，也不自动修复；孤立 evidence 引用由数据库健康检查显式报告。
- 数据库健康检查是只读报告工具，不静默删除、不自动修复空间链接、不自动重写核心记忆。

## 5. 阶段验收摘要

### 阶段一：记忆治理中心一期

状态：已完成。

已完成：

- `/memories/review` 保持旧字段兼容，并扩展 `risk_tags`、`severity`、`next_action_options`、`core_memory_sections`。
- `/memories/review/actions` 支持 `confirm_valid`、`snooze`、`lower_importance`、`move_to_trash`、`merge`。
- 手动治理操作写入 `memory_decision_logs`。
- AI 修改流程支持相关记忆查找、用户勾选范围、preview token 校验和审计记录。

边界：

- `time_uncertain` 仍是启发式规则。
- 普通删除保持软删除语义；真正删除由回收站永久删除入口处理。

### 阶段二：召回解释与 Breath 面板一期

状态：已完成。

已完成：

- 搜索结果增加 `score_breakdown`。
- `/memories/search` 保持 `data` 数组结构，并在每条命中上附加召回解释字段。
- 搜索缓存保存并恢复分数拆解。
- `POST /memories/context/explain` 用于解释一次上下文构建。
- `POST /memories/search-feedback` 用于记录召回反馈。
- 前端“召回解释”页面展示上下文包、搜索命中、候选池、被排除候选和分数条。

边界：

- `score_breakdown` 是用于解释和调试的可读拆解，不承诺等同于精确数学归因。
- 召回反馈只做审计记录，不自动影响排序权重。

### 阶段三：遗忘曲线和自然浮现深化

状态：已完成。

已完成：

- `/memories/surface` 增加 `mode=balanced|important|emotional|stale|review_due`。
- MCP `surface_memories(limit=8, mode="balanced")` 与 REST 行为对齐。
- 新增返回字段：`surface_score`、`surface_mode`、`surface_reason_text`、`life_score`、`days_since_last_active`、`review_signals`。
- `review_signals` 覆盖 `expired`、`review_due`、`near_expiry`、`sensitive`、`stale`、`emotion_uncertain`、`low_life`。
- 体检建议新增高唤起低置信、长期未活跃但仍重要、低生命力低重要度等信号。

边界：

- `surface_score` 和 `life_score` 都是运行时计算，不写入数据库。
- `review_signals` 是复核提示，不代表系统自动判定事实真假。

### 阶段四：记忆空间和分类工作台一期

状态：已完成。

已完成：

- 新增 `memory_spaces` 和 `memory_space_links`。
- `memories` 新增 `topics_json`、`entities_json`。
- `MemoryRecord` 对外返回 `topics`、`entities`、`space_ids`。
- 主题、实体和空间名会 trim、折叠连续空白、去重并保持顺序。
- REST 支持空间列表、空间详情、记忆空间绑定、主题实体更新、网络图过滤。
- 分类变化写入 `memory_decision_logs`。
- JSON export version 3 在 version 2 的 `memory_spaces`、`topics`、`entities`、`space_ids` 基础上加入 `conversation_branch_nodes`；restore 继续兼容旧版本。
- Web UI 展示并编辑主题、实体、空间，支持按分类过滤和网络图筛选。

边界：

- 本期空间是“标签式空间”，不是完整空间管理台。
- 暂无空间改名、颜色、排序、归档和空间详情页的管理能力。
- 分类信息是用户组织结构，不应被用来自动推断事实真假。

### 阶段五：Markdown/Obsidian 镜像一期

状态：已完成。

已完成：

- `GET /memories/export?format=obsidian_markdown` 返回 `application/zip`。
- zip 文件名形如 `memory-obsidian-export-{user_id}.zip`，用户 ID 会做安全化处理。
- zip 采用“单正文 + 索引链接”结构，包含：
  - `Memories/notes/{type}-{short_id}.md`
  - `Memories/by-type/{type}.md`
  - `Memories/by-space/{space_name}.md`
  - `Core Memory/{section}.md`
  - `Review/review-due.md`
  - `Review/deleted-memories.md`
  - `Reports/memory-report.md`
  - `Reports/export-summary.md`
- 每条记忆 Markdown frontmatter 包含 ID、类型、重要度、置信度、稳定性、敏感级别、情绪坐标、主题、实体、空间、复核时间和更新时间等元信息。
- 文件名稳定、可读，并避免把敏感正文写入文件名。
- 前端“报告与备份”页支持下载 Obsidian zip。

边界：

- Obsidian zip 只服务于查看、备份和迁移，不是主数据库。
- 不做 Markdown 双向同步，不从 Markdown 导入覆盖 SQLite。

### 阶段六一期：敏感遮罩和 `redact_sensitive=true`

状态：已完成。

已完成：

- 新增响应期遮罩 helper，集中处理 `sensitivity=private|sensitive`。
- REST 浏览响应支持 `redact_sensitive=true`：
  - `GET /memories`
  - `GET /memories/deleted`
  - `GET /memories/spaces/{space_id}`
  - `GET /memories/{memory_id}`
  - `GET /memories/{memory_id}/why`
  - `POST /memories/search`
  - `POST /memories/surface`
  - `POST /memories/network`
  - `POST /memories/context/explain`
- 被遮罩的 payload 保持原字段形状，同时增加 `redacted`、`redaction_reason`、`redacted_fields`。
- `content`、`source_message`、`source_excerpt` 在遮罩视图中替换为可读占位文案。
- 网络图过滤、相似度计算和边生成仍使用真实内容，只在最终 node payload 上遮罩。
- Web UI 默认使用遮罩视图；遮罩状态下禁止直接编辑正文，显式查看完整内容后再允许编辑。

边界：

- 遮罩策略只影响响应，不改变数据库原文。
- 遮罩不影响 JSON restore，也不改变 JSON、Markdown、Obsidian zip 的完整备份语义。
- REST 默认保持兼容，不传 `redact_sensitive` 时仍返回完整内容。
- MCP 本期不改，避免改变 AI 客户端拿到的上下文。

### 阶段六二期：回收站永久删除

状态：已完成。

已完成：

- 新增 `DELETE /memories/deleted/{memory_id}/purge`。
- 请求体为 `{ "confirm_memory_id": "<完整 memory_id>" }`。
- 只允许永久删除已在回收站中的记忆。
- 缺失或不匹配确认 ID 返回 `422`。
- 不存在、非回收站、重复删除、跨用户目标统一返回 `404`。
- 新增 `DecisionLogAction = create|update|ignore|purge`，只用于审计日志。
- 保持 `MemoryAction = create|update|ignore`，避免 LLM 候选记忆动作被扩展成可 purge。
- 存储层新增事务型 `purge_archived_memory`：读取已归档记忆、写入审计日志、删除空间链接、删除记忆记录。
- 审计日志 `candidate_json` 只保存安全元数据，不保存完整正文或来源原文。
- 核心记忆 evidence 引用不阻止永久删除；受影响核心分区会写入响应和审计日志。
- Web UI 在回收站行操作和详情抽屉中新增独立“永久删除”危险按钮。
- Web UI 使用专用确认弹窗，展示完整 ID 和 8 位确认码，输入确认码后才启用按钮。
- 决策日志页可过滤永久删除记录。

边界：

- 删除不清理 `memory_decision_logs`、核心记忆、核心历史。
- 核心记忆内容和 evidence 列表不自动改写。
- 永久删除入口保持 REST 和 Web UI 范围，不进入 MCP。

### 阶段六三期：数据库健康检查

状态：已完成。

已完成：

- 新增 `GET /memories/health`，返回 `status`、`checked_at`、`summary` 和结构化 `issues`。
- 健康检查只读扫描当前用户的活跃记忆、回收站记忆、核心记忆、空间链接、核心历史、决策日志、搜索缓存和导出结构。
- 覆盖 issue 类型：`orphan_core_evidence`、`archived_core_evidence`、`orphan_space_link_memory`、`orphan_space_link_space`、`embedding_missing`、`embedding_invalid`、`embedding_dimension_mismatch`、`export_consistency_error`、`export_space_reference_missing`、`stale_search_cache_reference`、`orphan_core_history_evidence`、`orphan_decision_log_reference`、`invalid_decision_log_json`。
- 状态规则固定为：存在 `error` 则整体 `error`；否则存在 `warning` 则整体 `warning`；只有 `info` 或无 issue 时整体 `ok`。
- embedding 缺失只检查活跃记忆；未配置 embedding key 时为 `info`，已配置时为 `warning`；非法 JSON 或维度不匹配为 `warning`。
- Web UI 在“记忆体检”页新增“数据库健康”区块，展示状态、检查时间、错误/警告/提示计数，默认隐藏 `info` 提示项。

验收覆盖：

- 健康数据库返回 `ok`。
- 核心 evidence 指向回收站记忆会报告 `archived_core_evidence`。
- 永久删除后留下的核心 evidence 会报告 `orphan_core_evidence`，但不自动改写核心记忆。
- `memory_space_links` 指向不存在的记忆或空间会报告错误，健康检查本身不修改链接。
- embedding 缺失、非法 JSON 和维度不匹配均有测试覆盖。
- 导出构建异常会转成 `export_consistency_error`，导出中的缺失空间引用会被报告。
- 搜索缓存、核心历史和决策日志中的历史孤立引用只作为 `info` 报告。
- REST 鉴权和 `X-User-Id` 隔离已覆盖。

边界：

- 健康检查是报告工具，不是自动修复工具。
- 不静默删除、不自动修复空间链接、不自动重写核心记忆 evidence。
- 该能力保持 REST 和 Web UI 范围，不进入 MCP。

## 6. 当前后续步骤

### 6.1 阶段六四期：人工视觉验收和移动端微调

优先级：高。

原因：

- 自动视觉验证曾被 Windows 沙箱阻断，错误为 `CreateProcessAsUserW failed: 5`。
- 阶段六二期新增高风险确认弹窗，阶段六三期新增数据库健康区块，需要人工确认桌面端和移动端可读、可点、不会误触。
- 该阶段不需要新增复杂业务能力，主要目标是把已完成能力验收成稳定可用的 UI。

建议检查清单：

- 回收站表格的“恢复”和“永久删除”按钮是否视觉分离。
- 回收站详情抽屉的危险按钮是否清晰但不抢占恢复操作。
- 永久删除确认弹窗在桌面端和移动端是否不溢出、不遮挡、按钮状态明确。
- 短 ID 输入确认是否能阻止误操作。
- 记忆体检页数据库健康区块是否能清晰区分 `error`、`warning`、`info`。
- 记忆库移动端抽屉。
- 遮罩提示与“查看完整内容”按钮。
- 标签编辑器。
- 网络过滤器和网络详情。
- 空间概览。
- 报告与备份页导出提示。

验收标准：

- 关键页面桌面端和移动端无明显重叠、溢出、按钮文字截断。
- 永久删除确认弹窗在小屏上仍能完成输入、取消和提交。
- 恢复、软删除、永久删除三类操作的视觉层级清楚。
- 数据库健康检查的错误、警告和提示不会和治理建议混淆。

### 6.2 可选增强：空间管理增强

优先级：中，建议排在阶段六四期之后。

建议范围：

- 空间改名、颜色、描述、排序。
- 空间详情页。
- 空间归档。
- 按空间导出报告。

风险与边界：

- 容易从“轻量组织”膨胀成复杂知识库。
- 不应在没有明确使用需求前引入空间权限、继承、自动聚类或复杂知识图谱。

### 6.3 可选增强：近期对话批量导入

优先级：中低，建议排在阶段六四期和必要的空间管理微调之后。

建议范围：

- 支持 JSON、Markdown、纯文本。
- 支持按会话切分。
- 支持导入预览。
- 导入后进入候选区或复核队列，不直接污染长期记忆。

风险与边界：

- 容易制造大量低质量记忆。
- 必须依赖治理中心、召回解释和浮现复核，而不是直接写入大量长期记忆。

### 6.4 更远期：多用户和多客户端

优先级：低，建议排在隐私安全和本地部署体验稳定之后。

建议范围：

- 更清晰的 user profile 管理。
- 每个 user 独立 API key 或 token。
- 客户端来源标识。
- 按客户端控制可读/可写范围。

风险与边界：

- 当前系统定位是可信本地或私有网络部署，多用户会显著增加权限复杂度。
- 权限边界不清晰时，不应开放跨用户共享或批量管理能力。

## 7. 暂不建议推进的方向

- 不做 Markdown 双向同步；Obsidian zip 继续只作为查看、备份和迁移镜像。
- 不做复杂 ACL 或多用户权限模型；系统定位仍是可信本地或私有网络部署。
- 不根据召回反馈自动调权；先保留审计记录。
- 不自动级联重写核心记忆；核心记忆仍应由可解释预览和用户确认驱动。
- 不持久化 `surface_score`、`life_score`、`decay_score` 等运行时评分。
- 不把空间分类升级为复杂知识图谱。
- 不新增独立任务系统。
- 不把健康检查升级成自动修复工具。

## 8. 后续可考虑的数据模型

已落地：

- `memories.topics_json`
- `memories.entities_json`
- `memory_spaces`
- `memory_space_links`
- `memory_decision_logs.decision = purge`

可考虑但不急于落地：

- `memory_versions`：保存修改历史。
- `memory_feedback`：保存召回反馈和未来调权数据。
- `last_reviewed_at`：明确记录最近确认有效时间。
- `last_surfaced_at`：如未来需要分析自然浮现历史，可考虑记录。

暂不建议新增：

- 独立任务系统。
- 复杂权限 ACL。
- 全文 Markdown 双向同步。
- 自动聚类和自动级联重写核心记忆。
- 持久化 `surface_score`、`life_score`、`decay_score` 等运行时评分。

## 9. 建议实施顺序

1. 阶段六四期：人工视觉验收和移动端微调。
2. 可选增强：空间管理增强。
3. 可选增强：近期对话批量导入。
4. 更远期：多用户和多客户端。

## 10. 每阶段通用完成标准

- 后端接口有测试覆盖。
- 旧导出文件仍可恢复。
- MCP、REST、搜索、体检、回收站、核心记忆不回退。
- 前端 `npm run build` 通过。
- 本地启动后检查桌面和移动端关键页面。
- 新能力有明确失败提示，不静默失败。
- 任何 AI 操作都必须有可解释的预览或审计记录。
- 涉及删除、敏感、核心记忆证据的能力必须保守处理。
- 涉及空间、主题、实体的能力必须保持用户可编辑、可导出、可恢复。
- 涉及永久删除的能力必须先审计、再删除，且不能把敏感正文复制进审计日志。
- 涉及数据库健康检查的能力必须只读报告，不自动修复。
