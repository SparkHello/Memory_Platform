# 客户端协议：affinity 与 reasoning origin

Model Gateway 对外提供 OpenAI-compatible `/v1/chat/completions` 和 `/v1/embeddings`，但业务模型名是 route ID，不是某个 provider 的模型 ID。

## 鉴权与模型选择

客户端使用自己的本地 Bearer key：

```http
Authorization: Bearer ${MODEL_GATEWAY_CLIENT_KEY}
Content-Type: application/json
```

请求正文中的 `model` 通常是 route：

```json
{
  "model": "memory.chat",
  "messages": [{"role": "user", "content": "hello"}]
}
```

只有 client 配置了 `allow_direct_deployments=true` 时，才可请求 `deployment:<deployment-id>`。My_Memory 推荐只请求稳定 route，不直接绑定 deployment。

## 三种 deployment 选择方式

### 默认 route 顺序

不发送 affinity Header 时，网关按请求所需能力、route 的 targets 顺序、`fallback_scope`、`max_attempts`、启用状态、使用范围和 429 冷却选择 deployment。`none` 不自动切换目标，`same_channel` 只允许同 connection/channel，`any_channel` 才允许跨渠道。只有 provider/连接层可恢复错误才会尝试下一项；内容或策略拒绝不会借 fallback 绕过。

运行时能力由 `stream`、`tools`、`parallel_tool_calls`、多模态消息 part、推理控制和
`response_format` 推导。route 中没有兼容目标时返回
`422 model_gateway_capability_unavailable`，`required_capabilities` 列出缺少的声明能力。
推理已显式开启（或 deployment 默认开启）且 `tool_choice` 超出 deployment 的
`tool_choice_with_reasoning` 策略时，也使用同一稳定 422，并列出
`tool_choice_with_reasoning`；网关不会静默删除具体函数选择后继续付费调用。

磁盘硬保留量不足时，请求会在任何 provider send 前返回
`507 model_gateway_insufficient_storage`，响应与 Header 均声明 `attempts=0`。释放空间且
`/readyz` 恢复后可以安全重试。

### 软偏好

```http
X-Model-Gateway-Preferred-Deployment: chat-primary
```

如果该 deployment 属于当前 route，就把它移到本次尝试顺序的首位；不可用时仍可 fallback。它适合普通缓存命中或弱会话亲和，不保证一定使用该 deployment。

### 严格 affinity

```http
X-Model-Gateway-Require-Deployment: chat-primary
```

严格模式只允许这一 deployment：

- deployment 必须属于当前 route，并在 client 权限范围内；
- 禁用、冷却、缺密钥或使用范围不符时不切换；
- 上游出现可 fallback 的鉴权、余额、模型、网络、429 或 5xx 错误时也不尝试其他 deployment；
- 无法继续时返回 HTTP `409`，错误码为 `model_gateway_affinity_unavailable`。

严格 affinity 用于“继续使用某个 provider 私有状态比拿到任意回答更重要”的轮次，尤其是包含原生 reasoning 的工具调用链。调用方收到 409 后必须把当前操作视为失败，原请求不得自动重发。只有用户或上层编排显式开始一个不再依赖上一 provider 私有状态的新操作时，才可以删除私有 reasoning 后发起新的普通 route 请求；它不是原请求的 retry。

## reasoning origin

工具调用经常需要把上一条 assistant 消息连同 `tool_calls` 和原生 `reasoning_content` 送回模型。该推理状态可能只对产生它的 deployment 有效。

客户端应把产生这段 assistant reasoning 的 deployment 告诉网关：

```http
X-Model-Gateway-Reasoning-Origin-Deployment: chat-primary
```

网关把它与本次实际 target 比较：

- target 相同：保留 assistant 历史中的 `reasoning_content` / `reasoning`；
- target 不同：转发前从 assistant 历史中删除这些私有推理字段，保留可见内容、`tool_calls` 和工具结果；
- 未发送 origin：网关无法判断来源，不主动删除。

这个 Header 不会单独固定 deployment。需要“尽量同源”时配合 `Preferred`；需要“必须同源，否则稳定失败且不自动重发”时配合 `Require`。

## 推荐的工具轮次流程

1. 第一次请求 `memory.chat`，不带 affinity；
2. 从响应 Header 读取实际 `X-Model-Gateway-Deployment`；
3. 保存该 ID，作为下一腿的 reasoning origin；
4. 回传工具结果时同时发送：

   ```http
   X-Model-Gateway-Require-Deployment: <上一步实际 deployment>
   X-Model-Gateway-Reasoning-Origin-Deployment: <上一步实际 deployment>
   ```

5. 若成功，继续用新响应的 `X-Model-Gateway-Deployment`；
6. 若收到 `409 model_gateway_affinity_unavailable`，结束当前工具轮次并把错误交给用户或上层编排；不得自动清除 affinity 后重发。若用户明确选择重新开始，构造不含上一 provider 私有 reasoning 的独立新请求。

不要按 route 首项猜测实际 deployment；前面可能发生过 provider fallback。

## 响应 Header

成功上游响应包含：

| Header | 含义 |
| --- | --- |
| `X-Model-Gateway-Route` | 请求命中的业务 route；直接 deployment 请求时可能为空 |
| `X-Model-Gateway-Deployment` | 实际产生响应的 deployment，也是下一轮 reasoning origin 的权威值 |
| `X-Model-Gateway-Connection` | 实际使用的渠道连接 |
| `X-Model-Gateway-Channel-Operator` | 实际计费与提供接口的渠道运营方，例如官方、硅基流动或阿里云 |
| `X-Model-Gateway-Model-Author` | 模型作者；与渠道运营方分开，不能用于推断账单 |
| `X-Model-Gateway-Vendor` | 第一版兼容别名，值仍是 channel operator，不是 model author |
| `X-Model-Gateway-Upstream-Model` | 实际发送给 provider 的模型 ID |
| `X-Model-Gateway-Attempts` | 本次在下游响应开始前尝试的上游数量 |
| `X-Model-Gateway-Pricing` | 本次 deployment 绑定的 pricing ID；未绑定时不存在 |
| `X-Model-Gateway-Embedding-Space` | embedding deployment 的向量空间 ID |
| `X-Model-Gateway-Embedding-Dimensions` | 已逐条验证后的 embedding 维度 |
| `X-Model-Gateway-Usage-Event-ID` | 非流逻辑请求的中央 usage event ID；写库失败或流式响应开始时不存在 |
| `X-Model-Gateway-Usage-Ledger-Status` | 非流响应为 `complete` 或 `incomplete`；流式响应开始时为 `deferred`，不得据此宣称最终账本完整 |

响应没有单独的 `Reasoning-Origin` Header；客户端使用实际 `X-Model-Gateway-Deployment` 作为后续 origin。

## adapter 与 reasoning_default

请求选择 target 后，网关按以下顺序准备上游 JSON：

1. 深拷贝客户端 payload，并把 route 名换成 `upstream_model`；
2. 如果 reasoning origin 与实际 target 不同，清理 assistant 私有 reasoning；
3. 运行显式 deployment `adapter_profile`，否则运行 connection 的 `generic`/`kimi`/`deepseek`/`mimo`/`dashscope_openai` adapter；
4. 应用 deployment 的 `remove`、`set_if_missing`、`force` 变换。

命名 adapter 判断推理设置时，客户端显式 `thinking.type` 优先于 `reasoning_effort`，二者都缺失时才使用 deployment 的 `reasoning_default`。`generic` 不解释推理字段，保持客户端协议。

adapter 只修改已明确实现的请求兼容字段；它不会重新构造成功响应。

## 透明响应与流式边界

- 未知请求 JSON 字段保留原结构；
- 成功非流响应正文按上游 bytes 返回；
- 成功 SSE chunk 按上游 bytes 返回，不转成内部 ModelResponse；
- usage-only chunk 与 `[DONE]` 不被网关重建；
- fallback 只允许发生在成功流的首字节交给下游之前，并且仅限确定请求尚未发出的连接建立失败或明确的 408、429、5xx；401/402、redirect、404 和 model-not-found 不重发；
- 读/写超时和已收到成功响应头后的首字节前断流可能已经计费，返回 `502 model_gateway_ambiguous_upstream_error`，不会自动重发；
- 首字节之后的断流原样表现为不完整流，不会拼接第二个 provider；
- 上游 redirect 不跟随，并转换成本地 502，避免凭据跨 origin。

网关会旁路解析 usage 用于元数据计量，但解析失败不会修改响应字节或把正文写入 `usage.db`。`usage_events` 保留一次客户端逻辑请求；`attempt_events` 为每个实际上游 HTTP send 保存一条有限枚举的 metadata-only 记录。费用汇总按 attempt 和币种相加，缺 usage/价格的已发送 attempt 单列为未知，连接前失败则明确记为零成本；历史上没有 attempt 行的逻辑事件继续兼容汇总。

### 受信 backend 用量归因

只有 `kind=backend` 的已鉴权 client 可以在推理请求中发送以下 Header；interactive/admin 发送会得到 403：

```http
X-Model-Gateway-Correlation-ID: turn:abc-123
X-Model-Gateway-Operation: memory.chat.answer
X-Model-Gateway-User-Tag: user:opaque-7
```

每个值必须是 1–120 字符的 opaque ASCII ID，格式为 `[A-Za-z0-9][A-Za-z0-9._:-]{0,119}`。调用方必须先把邮箱、用户名等身份变成稳定的不透明 tag，不能把原始身份或正文塞入 Header。Model Gateway 不另收 HMAC secret；backend Bearer 身份本身就是这组 Header 的信任边界。

backend 只能查询自己的中央事实，admin 才能跨 client 查询：

- `GET /v1/usage/events?event_id=&correlation_id=&operation=&user_tag=&days=&limit=`：返回最近 90 天 metadata-only 原始事件、逐 attempt 已知币种费用与未知费用 attempt 数；
- `GET /v1/usage/summary?days=&operation=&user_tag=`：返回逻辑调用/Token、按 attempt 与币种求和的费用、未知费用、deployment 汇总和 retention 信息；admin 可额外给 `client_id`。

原始事件保留 90 天后原子滚入日汇总；日汇总保留 365 天。流式 usage event 在流结束后才落库，因此响应开始时只回显 correlation ID，调用方稍后按 correlation 查询。

## Embedding 客户端要求

Embedding 请求也使用 route，例如 `memory.embedding`。请求显式携带的 `dimensions` 必须等于 route 的声明，否则网关返回 `400 model_gateway_embedding_dimensions_mismatch`；省略时网关仍会向上游注入权威维度。网关会验证响应中每条向量的长度，全部通过后才返回实际 `X-Model-Gateway-Embedding-Space` 和 `X-Model-Gateway-Embedding-Dimensions`。客户端仍应把向量缓存/数据库记录与该身份绑定。

同一 route 在配置层已经禁止混用不同空间或维度，但客户端仍不应按 route 首项推断实际 deployment。
