# Memory Platform 兼容契约 v2

本文是当前仓库唯一的兼容性索引，记录升级时必须保留的接口和持久化格式，而不是另一份 API 说明。字段和命令选项仍以两个服务的 README 为准。

## 稳定外部接口

| 接口 | 兼容规则 |
| --- | --- |
| Memory REST 与 Web Console | 现有路径、方法、认证 scope 和响应结构继续可用；实验性 graph/review 调用改为用户主动触发，并未删除。 |
| MCP | 现有工具名称、参数和响应结构继续可用。 |
| OpenAI-compatible API | `/v1/models` 与 `/v1/chat/completions` 继续作为透明兼容接口，包括 SSE 原始字节和未知 provider 字段。 |
| Model 管理接口 | 现有 HTTP endpoint、请求字段及 CLI 别名继续由兼容适配层接受，并转换为规范的 control-plane DTO。 |
| Python Store | `from app.memory.store import MemoryStore` 与 `from app.knowledge.store import KnowledgeStore` 继续有效，公共方法集合和签名保持兼容。 |
| Model 配置类型 | `model_gateway.models` 继续是受支持的导入路径；portable config/backup 仍使用同一份 `GatewayConfig` schema 验证。 |

`BillingPlan` 继续序列化为展示和审计元数据，但不授权 backend 流量；运行时只由 `ConnectionConfig.usage_scope` 决定该策略。

## 对话分支

分支 API、软删除/恢复行为、响应结构以及 `X-Memory-Branch-State` 的取值保持不变：`root`、`matched`、`fork`、`conversation-fallback` 和 `off`。

- 存在真实 `X-Conversation-Id`/`conversation_id` 且请求没有可见父历史时，可以用已存分支作为 `conversation-fallback`；只有该后备路径可以注入或压缩滚动摘要。
- `matched` 请求已经包含可见父历史，因此不会再次注入同一历史。
- 没有 conversation ID 时，历史指纹仍用于保存分支树、重新生成/分叉行为和最近轮次，但不会猜测已经被客户端截断的上下文；新节点的 `compressed_summary` 通常为空。

## 知识检索

Knowledge 响应结构不变。本地检索是稳定 baseline：禁用出站时直接返回；启用出站时，prompt-injection 拒绝、敏感信息拒绝出站、非法远程引用或远端失败都只影响可选 Agent 步骤，并回退到同一份本地 baseline。

## 持久化格式与升级规则

| 格式 | 当前版本 | 兼容规则 |
| --- | ---: | --- |
| Memory SQLite | `PRAGMA user_version=7` | v6 原地迁移到 v7；旧程序必须按 future-schema 保护拒绝打开，回滚需恢复升级前备份。 |
| Knowledge SQLite | `PRAGMA user_version=2` | 受支持的旧 schema 原地迁移；future schema 被拒绝。 |
| Auth SQLite | `PRAGMA user_version=2` | 保留 token hash/scope；future schema 被拒绝。 |
| Model Gateway config | `schema_version=2` | v1 按文档迁移后加载；JSON 字段名和持久化后的 v2 结构不变。 |
| Portable stack backup | manifest v2 | 现有 v2 归档继续可以验证与恢复，且仍不包含 secret。 |
| Memory JSON export | version 3 | 现有 version-3 导出继续可以恢复。 |
| Knowledge export | `schema_version=3` | 现有 version-3 导出继续可以恢复。 |

Memory v7 是数据库文件级的单向迁移。升级前必须创建 stack backup；不要让旧版本程序尝试打开已迁移数据库。

## 错误与归因契约

- 客户端原始请求本身要求不受支持的能力时，继续返回现有 `422` capability 错误。
- 本地 adapter、transform 或 secret 配置导致 target 不安全时，在任何 provider 调用前跳过该 target；全部 target 都无效时，稳定返回 `503 model_gateway_configuration_invalid`，attempts 为 0。
- Model usage attribution header，以及 Memory 调用 Model 时的 operation、correlation 和 user-tag header 名称保持不变。
- Usage 记录只保存元数据和有界 usage，不保存请求正文、provider secret、上游错误正文或 pricing 证据页面。

兼容并不要求冻结内部实现。只要上述契约及其 characterization tests 继续通过，repository、executor、control-plane service、安装器和运行镜像都可以替换。
