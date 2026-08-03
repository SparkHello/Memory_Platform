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

不发送 affinity Header 时，网关按 route 的 targets 顺序、`max_attempts`、启用状态、使用范围和 429 冷却选择 deployment。只有 provider/连接层可恢复错误才会尝试下一项；内容或策略拒绝不会借 fallback 绕过。

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

严格 affinity 用于“继续使用某个 provider 私有状态比拿到任意回答更重要”的轮次，尤其是包含原生 reasoning 的工具调用链。调用方收到 409 后，应删除不能跨 deployment 使用的私有 reasoning，再以普通 route 重试；不能悄悄把同一私有状态发给另一 provider。

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

这个 Header 不会单独固定 deployment。需要“尽量同源”时配合 `Preferred`；需要“必须同源，否则由客户端清理后重试”时配合 `Require`。

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
6. 若收到 `409 model_gateway_affinity_unavailable`，客户端清除上一 provider 的私有 reasoning，移除严格 affinity，再决定是否重试。

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
| `X-Model-Gateway-Embedding-Dimensions` | embedding 维度 |

响应没有单独的 `Reasoning-Origin` Header；客户端使用实际 `X-Model-Gateway-Deployment` 作为后续 origin。

## adapter 与 reasoning_default

请求选择 target 后，网关按以下顺序准备上游 JSON：

1. 深拷贝客户端 payload，并把 route 名换成 `upstream_model`；
2. 如果 reasoning origin 与实际 target 不同，清理 assistant 私有 reasoning；
3. 运行 connection 的 `generic`/`kimi`/`deepseek`/`mimo` adapter；
4. 应用 deployment 的 `remove`、`set_if_missing`、`force` 变换。

命名 adapter 判断推理设置时，客户端显式 `thinking.type` 优先于 `reasoning_effort`，二者都缺失时才使用 deployment 的 `reasoning_default`。`generic` 不解释推理字段，保持客户端协议。

adapter 只修改已明确实现的请求兼容字段；它不会重新构造成功响应。

## 透明响应与流式边界

- 未知请求 JSON 字段保留原结构；
- 成功非流响应正文按上游 bytes 返回；
- 成功 SSE chunk 按上游 bytes 返回，不转成内部 ModelResponse；
- usage-only chunk 与 `[DONE]` 不被网关重建；
- fallback 只允许发生在成功流的首字节交给下游之前；
- 首字节之后的断流原样表现为不完整流，不会拼接第二个 provider；
- 上游 redirect 不跟随，并转换成本地 502，避免凭据跨 origin。

网关会旁路解析 usage 用于元数据计量，但解析失败不会修改响应字节或把正文写入 `usage.db`。

## Embedding 客户端要求

Embedding 请求也使用 route，例如 `memory.embedding`。请求显式携带的 `dimensions` 必须等于 route 的声明，否则网关返回 `400 model_gateway_embedding_dimensions_mismatch`。客户端还必须读取响应中的实际 `X-Model-Gateway-Embedding-Space` 和 `X-Model-Gateway-Embedding-Dimensions`，核对每条向量的实际长度，再把向量缓存/数据库记录与该身份绑定。

同一 route 在配置层已经禁止混用不同空间或维度，但客户端仍不应按 route 首项推断实际 deployment。
