# 配置标准

`config.json` 是网关的可审计标准文档。写入前会用 `GatewayConfig` 校验完整关系图，再原子替换并保留备份；服务热加载新配置失败时继续使用上一份有效快照，并在 `/health` 的 `reload_error` 中报告问题。

密钥不属于 `config.json`。配置只保存 `secret_ref`，实际值在权限为 `0600` 的 `secrets.env` 中，由 `modelgw secret` 管理。

可以随时打印当前版本的完整 JSON Schema：

```bash
modelgw schema
modelgw --json schema
```

## 存储位置

- macOS：`~/Library/Application Support/model-gateway/`
- Linux：`$XDG_CONFIG_HOME/model-gateway/` 或 `~/.config/model-gateway/`
- Windows：`%APPDATA%\model-gateway\`

主要文件：

| 文件 | 内容 |
| --- | --- |
| `config.json` | server/client/connection/deployment/route/pricing 配置 |
| `secrets.env` | 本地 client key 和上游 provider key |
| `usage.db` | 无正文的用量、实际命中信息与价格快照 |
| `service-state.json` | `modelgw start` 管理的后台进程身份 |
| `model-gateway.log` | 后台进程日志 |

使用全局 `--home DIR` 可以隔离另一套配置，例如测试环境；同一次操作的配置、状态、日志和用量都会落在该目录中。

## 五层对象与独立 pricing

### server

本地监听设置，包括 `host`、`port` 和请求体上限。当前安全模式只允许 `127.0.0.1`、`localhost` 或 `::1`，不会直接绑定局域网地址。命令行的 `run/start --host/--port` 只覆盖本次启动，不改写配置文件。

### client

调用本地网关的应用身份：

- `backend`：My_Memory 这类无人值守服务；
- `interactive`：用户直接操作的桌面/终端客户端；
- `admin`：管理用途，但不会因此绕过 connection 的使用范围；
- `allowed_routes`：允许的 route glob；
- `allow_direct_deployments`：是否允许请求 `deployment:<id>`，默认关闭。

本地 client key 与任何上游 key 完全分离。推荐让 My_Memory 只拥有所需路由：

```bash
modelgw client add memory-gateway \
  --kind backend \
  --route 'memory.*' \
  --route 'knowledge.*' \
  --set-secret
```

`--set-secret` 使用无回显输入。也可以先添加，再运行 `modelgw secret set memory-gateway`。

### connection

一个真实购买或调用渠道，而不是模型作者。相同模型的官方账号、转售平台账号和套餐端点必须是不同 connection，因为它们具有不同的 Base URL、密钥、计费、限流和使用条款。

主要字段包括：

- `channel_operator`：实际渠道运营方；
- `base_url` 与 chat/embedding/models 相对端点；
- `auth.type` 和 `auth.secret_ref`；
- `billing_plan` 与 `usage_scope`；
- `adapter`；
- 允许透传的请求 Header 白名单、超时和 429 冷却时间。

远程 Base URL 必须使用 HTTPS；HTTP 仅允许本机回环地址。代理不跟随上游 HTTP redirect，避免把凭据带到另一个 origin。

套餐类型为 `payg`、`subscription`、`free_tier`、`token_plan`、`coding_plan` 或 `custom`。其中 `token_plan` 和 `coding_plan` 不能配置为 `backend_allowed`，CLI 默认把它们设为 `interactive_only`。这条限制同时存在于：

1. 配置模型校验；
2. 运行时 client 路由；
3. 默认按 backend 用途执行的健康检查。

`--as-interactive` 只对当次检查生效，绝不会修改 connection，也不会让 My_Memory 使用交互套餐。

### deployment

一个 connection 上的精确上游模型 ID。deployment 声明：

- `upstream_model`、模型作者和家族；
- `chat` 或 `embedding`；
- streaming/tools/reasoning/multimodal/JSON 等能力；
- `reasoning_default`；
- 可选请求变换；
- embedding 向量空间和维度；
- 可选 pricing 引用。

能力字段是路由校验所用的声明，不会自动让 provider 获得该能力。填写前应以实际账号端点和官方文档为准。

`reasoning_default` 有三个值：

- `inherit`：客户端没有显式推理设置时不注入默认值；
- `enabled`：命名 adapter 在客户端未指定时默认开启；
- `disabled`：命名 adapter 在客户端未指定时默认关闭。

客户端显式 `thinking.type` 优先，其次是显式 `reasoning_effort`，最后才使用 `reasoning_default`。`generic` adapter 不解释这些字段，因而也不会根据 `reasoning_default` 猜测 provider 参数。

请求变换按 `remove`、`set_if_missing`、`force` 声明，并在 adapter 之后执行；它可用于某个账号或模型版本的已知差异，但不能触碰核心字段 `model`、`messages` 或 `input`。

Embedding deployment 必须声明 `embedding_space` 和 `dimensions`。同一 embedding route 的所有 targets 必须两者完全一致；配置校验会阻止跨向量空间 fallback。客户端若显式发送 `dimensions`，它必须与 route 声明完全相同，否则请求会在到达上游前被拒绝；deployment transform 也不能强制改成另一维度。

### route

业务功能别名与有序 deployment targets。只有 route 表达优先级：

```bash
modelgw route set memory.chat chat-primary chat-secondary chat-tertiary \
  --kind chat \
  --max-attempts 3
```

targets 从左到右尝试。可用多个 `--require` 要求所有 deployment 声明相应能力，例如 `tools`、`reasoning` 或 `json_schema`。Model Gateway 使用明确 deployment ID，不把 `M`、`K`、`D` 解释成供应商缩写。

普通 fallback 只处理 provider/连接层失败；流式响应只能在首个上游成功流返回首字节之前切换。一旦开始向客户端发送 SSE，后续中断不会拼接另一 deployment 的输出。

### pricing

pricing 是与 deployment 绑定的独立审计记录，而不是模型作者的全局属性。不同渠道、地区、套餐或生效日期需要不同 pricing ID。

`per_token` 记录必须包含：

- 三位币种代码和 `unit_tokens`；
- 至少一个 input/cached input/output 单价；
- 对应渠道的官方 `source_url`；
- 人工核对时间 `checked_at`；
- 需要时用多个 `--tier` 表达按输入 Token 上限递增的分档。

不要从相似模型、搜索摘要或第三方聚合站复制价格。文档不提供任何“当前价格”示例；请先从实际 deployment 的官方价格页核对，再按 `modelgw pricing set --help` 录入。

缺少官方价格、上游 usage 或某类必要单价时，费用保持不完整，不显示成免费。每次成功调用保存当时的 pricing 快照；以后改价不会重写历史金额。

#### 官方价格候选研究

`modelgw pricing research` 可以把人工查找后的官方页面交给一个显式指定的研究 deployment，但它不是自动改价器：

```bash
modelgw pricing research TARGET_DEPLOYMENT \
  --source-url 'https://OFFICIAL_CHANNEL_HOST/path/to/pricing' \
  --research-deployment RESEARCH_CHAT_DEPLOYMENT
```

安全约束如下：

- `TARGET_DEPLOYMENT` 是精确 deployment；候选证据必须逐字包含它的 `upstream_model`，不能拿相似模型替代；
- 研究 deployment 必须是已启用的 `chat`，且 connection 为 `backend_allowed`；`token_plan`、`coding_plan`、`direct_tool_only` 和 `interactive_only` 均不可用；
- 页面必须是该目标 connection/channel 的官方 HTTPS 页面；默认要求与 API 属于同一组织域，跨域官方文档需用 `--official-host` 明确确认最终 hostname；不得用它认可第三方聚合站；
- 页面抓取不携带任何 API Key，不跟随 redirect，拒绝本机/私网 DNS、非 HTML/纯文本、隐藏脚本和疑似提示注入内容；
- 页面被当作不可信资料。研究模型只能返回严格 JSON；本地还会逐项核对页面摘要、精确模型、原文 evidence、币种、Token 单位、分档上限和每个单价；任一项不足即为 `unknown`；
- 默认不写 pricing 或 deployment。`--apply` 需要完整交互确认；非交互自动化必须同时显式使用 `--yes`；
- 研究调用的 metadata-only 用量以 `pricing.research` 写入 `usage.db`，包含实际研究 deployment/connection、状态、耗时、usage 和它自己的价格快照；页面、提示词、证据和回复不入库。

确认候选后，可以指定 ID 并应用：

```bash
modelgw pricing research TARGET_DEPLOYMENT \
  --source-url 'https://OFFICIAL_CHANNEL_HOST/path/to/pricing' \
  --research-deployment RESEARCH_CHAT_DEPLOYMENT \
  --pricing-id TARGET_PRICE_SNAPSHOT \
  --apply
```

已有同名 pricing 默认不会覆盖；需要替换时还要显式加 `--replace`。`--apply` 会再次构造并验证 `PricingConfig` 及完整 `GatewayConfig` 关系图，然后才原子写入并绑定目标 deployment。

## connection adapter

adapter 是少量、显式的 OpenAI-compatible 参数兼容规则，不是完整 provider SDK，也不负责业务路由。

| Adapter | 当前职责 |
| --- | --- |
| `generic` | 不解释推理参数；保留客户端字段，仅应用通用 model/auth 与 deployment transform |
| `kimi` | 在已支持的 Kimi 模型上转换 thinking/reasoning 控制，并处理工具轮次需要的推理字段 |
| `deepseek` | 转换 thinking/reasoning effort；在已知不兼容的“推理 + tools”组合中调整 `tool_choice` |
| `mimo` | 转换 thinking 控制，并补足工具历史需要的 `reasoning_content` 形状 |

命名 adapter 只处理代码中已经明确实现的规则。模型 ID、provider 行为或官方协议改变后，应先更新契约测试，不能依名称猜测。

示例只展示结构，不代表任何真实模型当前可用：

```bash
modelgw connection add provider-account \
  --vendor PROVIDER_NAME \
  --base-url PROVIDER_HTTPS_BASE_URL \
  --adapter kimi

modelgw deployment add chat-primary \
  --connection provider-account \
  --model UPSTREAM_MODEL_ID \
  --kind chat \
  --capability reasoning \
  --capability tools \
  --reasoning-default enabled
```

## My_Memory 推荐路由

My_Memory 的 Model Gateway 接入使用下面八个稳定功能名：

| Route | Kind | 用途 |
| --- | --- | --- |
| `memory.chat` | chat | 透明聊天代理 |
| `memory.extract` | chat | 长期记忆提取与 ingest |
| `memory.compact` | chat | 较早会话上下文压缩 |
| `memory.core` | chat | 核心记忆整理 |
| `memory.review` | chat | AI 记忆体检与修改建议 |
| `knowledge.fast` | chat | 知识代理快速阶段 |
| `knowledge.pro` | chat | 复杂知识检索升级阶段 |
| `memory.embedding` | embedding | 记忆与知识向量化 |

先创建真实 deployments，再用它们的 ID 替换下列示例中的占位名称：

```bash
modelgw route set memory.chat chat-primary chat-fallback
modelgw route set memory.extract memory-worker-primary memory-worker-fallback
modelgw route set memory.compact memory-worker-primary memory-worker-fallback
modelgw route set memory.core memory-worker-primary memory-worker-fallback
modelgw route set memory.review review-primary memory-worker-fallback
modelgw route set knowledge.fast knowledge-fast-primary memory-worker-fallback
modelgw route set knowledge.pro knowledge-pro-primary
modelgw route set memory.embedding embedding-primary --kind embedding
```

这只是推荐的功能边界，不指定供应商，也不声称任何模型适合某项工作。路由顺序、能力要求和价格必须以你的实际 deployments 为准。

对于 `memory.embedding`：

- 所有 fallback deployment 必须使用同一 `embedding_space` 和 `dimensions`；
- 请求中的 `dimensions` 若存在，必须与 route 声明完全一致；
- 响应 Header 会返回实际 `X-Model-Gateway-Embedding-Space` 和 `X-Model-Gateway-Embedding-Dimensions`；
- My_Memory 应把实际向量空间身份与缓存/数据库记录绑定，不能按 route 首项猜测。

## 更新与删除

`add` 命令默认拒绝覆盖已有对象，确认替换时使用 `--replace`。删除命令会保护引用关系：仍被 deployment 引用的 connection、仍被 route 引用的 deployment、仍被 deployment 引用的 pricing 都不能先删除。

```bash
modelgw client remove CLIENT_ID
modelgw connection remove CONNECTION_ID
modelgw deployment remove DEPLOYMENT_ID
modelgw route remove ROUTE_ID
modelgw pricing remove PRICING_ID
```

密钥删除是独立操作，不会自动删除配置对象：

```bash
modelgw secret list
modelgw secret delete CONNECTION_OR_SECRET_REF
```

列表只显示 secret_ref、引用和 configured/missing，不显示密钥值。

## 透明数据面边界

默认 `openai_compatible` connection 使用薄代理：

- 请求深拷贝后只替换上游模型 ID、鉴权，以及 adapter/transform 明确声明的参数；
- `tools`、`tool_calls`、多模态 part、`reasoning_content` 和未知 JSON 字段保持原有结构；
- 成功非流响应正文与 SSE chunk 不经模型对象重建，按上游字节转发；
- 本地 Bearer key、Cookie、Host 和 hop-by-hop Header 不会发给上游；
- 上游密钥不会跨 redirect；
- 用量记录接口不接受 prompt 或 response 正文。

严格 deployment affinity 和跨 deployment reasoning 清理另见[客户端协议](client-protocol.md)。
