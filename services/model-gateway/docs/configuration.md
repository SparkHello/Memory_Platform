# 配置标准

`config.json` 是网关的可审计标准文档。当前新写入格式为 `schema_version=2`；v1 会在内存中兼容迁移，下一次受控写入才落为 v2。所有 CLI/Web 写入共用跨进程文件锁、revision CAS 和不含密钥值的崩溃恢复日志；渠道 bundle 先验证候选 key，再按 secret-first/config-last 提交。服务热加载新配置失败时继续使用上一份有效快照，并在 `/health` 的 `reload_error` 中报告问题。

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

`.control-plane.lock` 与 `.control-plane-journal.json` 是控制面内部文件；后者只在未完成事务中短暂存在。进程在 secret/config 任一替换阶段崩溃时，下次初始化会把两者一起恢复到提交前版本。

使用全局 `--home DIR` 可以隔离另一套配置，例如测试环境；同一次操作的配置、状态、日志和用量都会落在该目录中。

## 五层对象与独立 pricing

### server

本地监听设置，包括 `host`、`port`、请求体上限和磁盘软/硬保留量。`disk_soft_reserve_bytes` / `disk_hard_reserve_bytes` 默认 64/16 MiB；小于 1 GiB 的测试或设备卷会按卷容量自动下调，单项设为 `0` 可关闭相应阈值。低于软阈值时 `/readyz` 返回安全的 `disk_low`；任何付费上游发送前都会确认 metadata-only ledger 写入后仍高于硬阈值，否则以 attempts=0 返回 507。持久化配置和后台 `start` 只允许 `127.0.0.1`、`localhost` 或 `::1`。双容器部署可让前台进程显式使用 `serve --host 0.0.0.0 --container-network`；这个例外不写入配置、不能换成其他非回环地址，也不应把 2030 端口发布到宿主机或公网。

### client

调用本地网关的应用身份：

- `backend`：My_Memory 这类无人值守服务；
- `interactive`：用户直接操作的桌面/终端客户端；
- `admin`：管理用途，但不会因此绕过 connection 的使用范围；
- `allowed_routes`：允许的 route glob；
- `allow_direct_deployments`：是否允许请求 `deployment:<id>`，默认关闭。

本地 client key 与任何上游 key 完全分离：两类对象不能复用 `secret_ref` 或实际密钥值。新建/轮换的 client key 必须至少 32 字节，且只含 URL-safe 字符（字母、数字、`_`、`-`）；推荐使用 `secrets.token_urlsafe(32)` 生成，不要使用口令或供应商 API Key。推荐让 My_Memory 只拥有所需路由：

```bash
modelgw client add memory-gateway \
  --kind backend \
  --route memory.chat \
  --route memory.extract \
  --route memory.compact \
  --route memory.core \
  --route memory.review \
  --route knowledge.fast \
  --route knowledge.pro \
  --route memory.embedding \
  --set-secret
```

`--set-secret` 使用无回显输入。也可以先添加，再运行 `modelgw secret set memory-gateway`。`--route` 支持 glob，但 `memory-gateway` 的通配权限仅用于用户明确决定的自定义策略，不是推荐默认值。

schema v1 升级时，原有短 client key 会以显式 `allow_legacy_weak_secret=true` 兼容到完成轮换为止；`modelgw doctor` 只报告对应 client ID，不显示密钥。执行 `modelgw secret set CLIENT_ID` 写入合格新 token 后，会把兼容标记与密钥一起原子更新。schema v2 新 client 不使用隐式弱密钥回退。

### connection

一个真实购买或调用渠道，而不是模型作者。相同模型的官方账号、转售平台账号和套餐端点必须是不同 connection，因为它们具有不同的 Base URL、密钥、计费、限流和使用条款。

主要字段包括：

- `channel_operator`：实际渠道运营方；
- `base_url` 与 chat/embedding/models 相对端点；
- `auth.type` 和 `auth.secret_ref`；
- `billing_plan` 与 `usage_scope`；
- `adapter`；
- 允许透传的请求 Header 白名单、`connect/read/write/pool` 四项超时、响应字节上限和 429 冷却时间。

远程 Base URL 必须使用 HTTPS；HTTP 默认仅允许本机回环地址。需要调用私有 IP 上游时，必须在 `allowed_private_networks` 写入最小 CIDR 后才允许该地址；RFC 2544 `198.18.0.0/15`（Clash/Surge TUN fake-ip 常用）仍默认拒绝，但可在渠道中**显式**写入 `/32`、子网或整个 `198.18.0.0/15`。未列入的私网地址继续拒绝。userinfo、query、fragment、异常端口、控制字符、dot segment、编码后的结构分隔符和危险鉴权/逐跳转发 Header 都会被拒绝。代理不继承环境 HTTP 代理，也不跟随上游 redirect，避免把凭据带到另一个 origin。v2 新 connection 的 connect/read/write/pool 默认分别为 10/120/60/10 秒，非流响应和流式累计响应上限默认 16 MiB；从 v1 迁移的显式旧超时与 64 MiB 上限保留原语义。discovery 另有 2 MiB、1,000 个模型和可打印模型 ID 上限。数据面会复用按超时配置分组的 `AsyncClient` 连接池，服务关闭时统一释放。

运行时熔断按故障作用域处理：401/402/429 暂停整个 connection；结构化 `model_not_found` 只暂停对应 deployment；连续三次 5xx 才短暂暂停该 deployment。每次真正发送前都会重新检查，渠道密钥轮换成功后会立即清除该 connection 的旧熔断。

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
- 可选的 deployment 级 `adapter_profile`；
- `reasoning_default`；
- 推理开启时允许的 `tool_choice` 策略；
- 可选请求变换；
- embedding 向量空间和维度；
- 可选 pricing 引用。

能力字段是路由校验所用的声明，不会自动让 provider 获得该能力。填写前应以实际账号端点和官方文档为准。

`adapter_profile` 默认为 `inherit`，继续使用 connection 的 adapter。当前唯一的显式
deployment profile 是 `dashscope_deepseek_v4`，只接受 `deepseek-v4-flash*` 和
`deepseek-v4-pro*`：它把通用 `thinking.type` 转成百炼 OpenAI-compatible 接口使用的
`enable_thinking`，保留该系列合法的 `low` / `medium` / `high` / `xhigh` / `max`
`reasoning_effort`，也不会删除工具请求的 `tool_choice`。该 profile 必须显式配置；网关
不会仅凭模型名称猜测渠道协议。

百炼托管的 Qwen connection 应显式使用 `adapter=dashscope_openai`。该 connection
adapter 把 Memory Gateway 的通用 `reasoning_effort=none|high` 转换为顶层
`enable_thinking=false|true`，并移除 Qwen 接口不使用的通用 effort 字段。百炼托管
DeepSeek V4 仍须在 deployment 上额外声明 `dashscope_deepseek_v4`，以保留该系列
官方支持的 effort 等级；deployment profile 优先于 connection adapter。

`reasoning_default` 有三个值：

- `inherit`：客户端没有显式推理设置时不注入默认值；
- `enabled`：命名 adapter 在客户端未指定时默认开启；
- `disabled`：命名 adapter 在客户端未指定时默认关闭。

客户端显式 `thinking.type` 优先，其次是显式 `reasoning_effort`，最后才使用 `reasoning_default`。`generic` adapter 不解释这些字段，因而也不会根据 `reasoning_default` 猜测 provider 参数。

`tool_choice_with_reasoning` 在 provider 请求发出前校验推理与工具选择的组合，默认 `auto_only`：允许省略、`none` 或 `auto`，拒绝 `required` 和具体函数对象。`any` 明确允许全部选择；`none` 只允许省略或显式 `none`，连 `auto` 也拒绝。客户端显式关闭推理时不应用该限制。这个策略用于表达实际账号/模型端点的协议能力；adapter 不会为了让请求“看起来成功”而静默删除或改写 `tool_choice`。

请求变换按 `remove`、`set_if_missing`、`force` 声明，并在 adapter 之后执行；它只用于某个账号或模型版本的非语义参数差异。自由变换不能触碰网关负责校验的字段：`model`、`messages`、`input`、`stream`、`dimensions`，推理字段 `thinking`、`enable_thinking`、`reasoning`、`reasoning_effort`，工具字段 `tools`、`functions`、`parallel_tool_calls`、`tool_choice`、`function_call`，以及 `response_format`。这些差异应通过明确的命名 adapter 和 deployment 能力表达。

控制面会拒绝新增或修改上述保留字段；删除旧规则仍然允许。为便于升级修复，历史配置中此前可写入的保留字段仍可加载，`modelgw doctor` 会仅报告 deployment 与字段名，不显示 transform 值。数据面不会执行这类旧规则，而是跳过该 target。adapter 和安全 transform 执行后，网关会按最终 payload 再做一次能力校验；单个无效 target 可跳过，全部无效时在未请求 provider 的情况下返回 `503 model_gateway_configuration_invalid` 和 `X-Model-Gateway-Attempts: 0`。客户端原始请求本身能力不足仍返回 `422 model_gateway_capability_unavailable`。

Embedding deployment 最终必须具有 `embedding_space` 和 `dimensions`。普通 quickstart、终端菜单、CLI deployment add 和管理 bundle 只需给出精确上游模型 ID 与维度；未显式覆盖时，网关按规范化渠道运营方、URL origin、精确模型 ID 和维度生成稳定、可打印且不跨渠道碰撞的空间 ID。`embedding_space` 显式值保留给确认过向量兼容性的专家迁移场景。

同一 embedding route 的所有 targets 必须让 `embedding_space` 和 `dimensions` 完全一致；配置校验会阻止跨向量空间 fallback。无论客户端是否携带或试图篡改 `dimensions`，代理发送上游前都会强制使用 deployment 声明值。成功非流响应只有在每一条 vector 长度都通过验证后才会返回空间归因 Header；任一长度不符会返回安全的 502，不把错误向量交给调用方。

### route

业务功能别名与有序 deployment targets。只有 route 表达优先级：

```bash
modelgw route set memory.chat chat-primary chat-secondary chat-tertiary \
  --kind chat \
  --fallback-scope any_channel \
  --max-attempts 3
```

targets 从左到右排列，但 v2 新 route 的 `fallback_scope` 默认是 `none`，只使用第一目标；这避免用户仅因填写了多个模型就意外跨渠道计费。显式选择 `same_channel` 才在同一渠道内兜底，选择 `any_channel` 才允许跨渠道；v1 多目标 route 会迁移成 `any_channel` 以保持既有行为。可用多个 `--require` 要求所有 deployment 声明相应能力，例如 `tools`、`reasoning` 或 `json_schema`。Model Gateway 使用明确 deployment ID，不把 `M`、`K`、`D` 解释成供应商缩写。

普通 fallback 只处理 provider/连接层失败；流式响应只能在首个上游成功流返回首字节之前切换。一旦开始向客户端发送 SSE，后续中断不会拼接另一 deployment 的输出。

每个请求还会从 `stream`、`tools`、`parallel_tool_calls`、多模态消息、推理控制和
`response_format` 推导运行时能力要求，并在 route targets 中只保留声明满足要求的
deployment。没有任何目标能满足时返回稳定的
`422 model_gateway_capability_unavailable`，不会把明显不兼容的请求先发给上游。

自动 fallback 只把连接建立失败视为“请求确定尚未发出”。读超时、写超时、已收到
成功响应头但首个流字节前中断等情况可能已经产生计费，因此返回
`502 model_gateway_ambiguous_upstream_error`，不自动向下一 deployment 重发。HTTP 只对明确
的 408、429 和 5xx 自动 fallback；401/402、redirect、404 及结构化 model-not-found 仅更新
对应 breaker 并原样返回，绝不把同一正文静默重发给另一目标。HTTP 400 正文也不再按自由
文本猜测“模型不存在”。

### pricing

pricing 是与 deployment 绑定的独立审计记录，而不是模型作者的全局属性。不同渠道、地区、套餐或生效日期需要不同 pricing ID。

`per_token` 记录必须包含：

- 三位币种代码和 `unit_tokens`；
- 至少一个 input/cached input/output 单价；
- 对应渠道的官方 `source_url`；
- 人工核对时间 `checked_at`；
- 需要时用多个 `--tier` 表达按输入 Token 上限递增的分档。

不要从相似模型、搜索摘要或第三方聚合站复制价格。文档不提供任何“当前价格”示例；请先从实际 deployment 的官方价格页核对，再按 `modelgw pricing set --help` 录入。

`source_url` 必须是用户人工确认过的官方 HTTPS 地址，且不能包含 userinfo、query、fragment、外围空白、控制字符或异常端口；网关不维护“官方域名白名单”，页面归属仍由用户确认。这样可避免把 URL 中的 token 扩散到价格快照、usage 或备份。

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
| `deepseek` | 转换 thinking/reasoning effort；不删除或改写 `tool_choice` |
| `mimo` | 转换 thinking 控制，并补足工具历史需要的 `reasoning_content` 形状 |

命名 adapter 只处理代码中已经明确实现的规则。模型 ID、provider 行为或官方协议改变后，应先更新契约测试，不能依名称猜测。

Kimi adapter 会区分官方开放平台模型与 Kimi Code 套餐模型：`k3` / `k3-256k` 使用
原生 `reasoning_effort`（Code 端点默认 `high`），`kimi-for-coding*` 按 K2.7 Code 的
thinking 形状处理。不要把这些套餐模型开放给 backend client。

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

百炼托管 DeepSeek V4 的显式 profile 示例：

```bash
modelgw deployment add deepseek-v4-flash \
  --connection dashscope-payg \
  --model deepseek-v4-flash \
  --author deepseek \
  --adapter-profile dashscope_deepseek_v4 \
  --tool-choice-with-reasoning auto_only \
  --capability reasoning \
  --capability tools
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

上述含两个 targets 的命令在 v2 默认只启用第一项；确实需要自动兜底时，请逐条显式追加 `--fallback-scope same_channel` 或 `--fallback-scope any_channel`，不要让示例替你决定是否跨渠道计费。

这只是推荐的功能边界，不指定供应商，也不声称任何模型适合某项工作。路由顺序、能力要求和价格必须以你的实际 deployments 为准。

对于 `memory.embedding`：

- 所有 fallback deployment 必须使用同一 `embedding_space` 和 `dimensions`；
- 请求中的 `dimensions` 若存在，必须与 route 声明完全一致；
- 网关无条件把 deployment 的 `dimensions` 写入上游请求，并验证返回的每一条向量；
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
