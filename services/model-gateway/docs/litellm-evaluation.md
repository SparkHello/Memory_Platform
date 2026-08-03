# LiteLLM 采用评估

评估日期：2026-08-02。证据仅使用 LiteLLM 官方文档与官方 GitHub 仓库；链接指向持续更新的官方页面，升级前仍需重新执行本项目的协议契约测试。

## 结论

LiteLLM 能用，而且非常适合一般的多 provider 统一网关。但本项目不把 LiteLLM Proxy 的统一 chat completion 路径放在要求严格透明的 My_Memory 数据面中。

采用方式是：

- 借鉴其 connection/deployment、优先级、fallback、冷却和健康检查设计；
- 继续使用本项目自己的 OpenAI-compatible 原始 HTTP/SSE 薄代理；
- 将来遇到非 OpenAI-compatible 原生协议时，可把 LiteLLM SDK 作为某个 deployment 的可选“规范化 adapter”；
- 任何 LiteLLM adapter route 都必须明确标成允许规范化，不能假装成字节透明 route。

## LiteLLM 做得很好的部分

官方路由文档支持同一逻辑模型的多个 deployments、模型组 alias、优先级和多种负载策略：[Routing & Load Balancing](https://docs.litellm.ai/docs/proxy/load_balancing)。

官方可靠性文档提供重试、一般 fallback、context-window fallback 与 content-policy fallback：[Fallbacks / Reliability](https://docs.litellm.ai/docs/proxy/reliability)。本项目借鉴“provider 失败才 fallback”的方向，但不会配置 content-policy fallback 去绕过策略拒绝。

自定义 OpenAI-compatible Base URL、chat 和 embeddings 是一等能力：[OpenAI-compatible providers](https://docs.litellm.ai/docs/providers/openai_compatible)。工具调用也有统一支持：[Function calling](https://docs.litellm.ai/docs/completion/function_call)。

因此，如果调用方只要求“统一 OpenAI 形状”，LiteLLM 往往比自建网关更合适。

## 为什么统一 completion 路径不满足这里的契约

My_Memory 不只是要求 API 大致兼容，还依赖以下更严格的属性：

- 成功上游响应正文和 SSE data bytes 不重建；
- 未知扩展字段保持原位置，而不是移动到另一个容器；
- `tools`、`tool_calls`、多模态 part 和 usage-only chunk 不丢失；
- `reasoning_content` 在工具轮次中按实际 deployment 来源保留或清除；
- 流开始后绝不拼接另一 provider；
- 上游 Header 与实际 provider/model 归因可审计。

LiteLLM 的统一 `/v1/chat/completions` 路径把 provider 输出转换为内部响应对象，再序列化成 JSON/SSE。官方源码中的 streaming serializer 会调用模型序列化并重新组成 `data:` frame，同时公开模型名也可能被重新标记为客户端 alias：[proxy_server.py](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/proxy_server.py)。这属于“规范化兼容”，不是字节透明。

官方响应转换源码会把非标准 message/choice 字段收进 `provider_specific_fields`；embedding 分支则只复制其已识别字段：[convert_dict_to_response.py](https://github.com/BerriAI/litellm/blob/main/litellm/litellm_core_utils/llm_response_utils/convert_dict_to_response.py)。这对统一 SDK 很有价值，但不满足“未知字段仍在原位置”的契约。

LiteLLM 官方将 reasoning 描述为跨 provider 的标准化能力，而非原样通道，并明确记录某些 extended-thinking/tool-call 组合的兼容条件：[Reasoning content](https://docs.litellm.ai/docs/reasoning_content)。因此不能只因响应里出现 `reasoning_content` 就推断其多轮私有状态可跨 provider 回放。

统一路径的上游 Header 也会经过重新命名和补充；官方实现将许多 Header 暴露为 `llm_provider-*` 并加入 LiteLLM 元数据：[get_headers.py](https://github.com/BerriAI/litellm/blob/main/litellm/litellm_core_utils/llm_response_utils/get_headers.py)。本项目则需要自己声明实际 route/deployment/connection/model，并保留一小组安全上游 Header。

## 为什么不直接使用 LiteLLM pass-through

LiteLLM 也提供通用 pass-through，可以把一条 path 映射到任意固定 target：[Pass-through endpoints](https://docs.litellm.ai/docs/proxy/pass_through)。其 stream handler 在默认路径上能够直接 yield 上游 bytes：[streaming_handler.py](https://github.com/BerriAI/litellm/blob/main/litellm/proxy/pass_through_endpoints/streaming_handler.py)。

但这个机制解决的是“固定 path → 固定 target”，不是在同一透明路径中同时提供本项目需要的：

- route 到多个 deployments 的有序选择；
- client/套餐使用范围校验；
- 首字节前 fallback 与首字节后禁止重试；
- strict affinity；
- reasoning origin 跨 deployment 清理；
- embedding space/dimensions 一致性；
- 官方 pricing 快照和无正文 usage 归因。

通用 pass-through 仍会解析请求、处理 Header，并为日志/计量收集流内容。若在它前面再写一层完整路由和安全逻辑，部署复杂度会高于当前的小型 FastAPI/httpx 数据面，收益有限。

## Health、usage 与 pricing 的差异

LiteLLM 的 `/health/liveliness` 和 `/health/readiness` 检查进程/数据库；检查真实模型的 `/health` 会发送 provider 请求并可能消耗 Token：[Health checks](https://docs.litellm.ai/docs/proxy/health)。

本项目把两类检查明确分开：

- discovery：最多一次 `GET /models`，不推理；
- `--live`：只有用户显式指定时发送最小真实请求。

LiteLLM 可统一 usage 并计算成本：[Usage](https://docs.litellm.ai/docs/completion/usage)、[Token usage and cost](https://docs.litellm.ai/docs/completion/token_usage)。其模型价格目录适合运营参考，但本项目的审计规则更严格：只接受实际渠道的官方 HTTPS 来源、缺失即 incomplete、每次调用保存价格快照、后续改价不重算历史。因此不会把社区维护目录直接当计费真相。

## 隐私、日志与许可证

LiteLLM 官方说明 self-host 不向 LiteLLM 服务发送遥测：[Data security](https://docs.litellm.ai/docs/data_security)。但启用 callback/logging 后，prompt/response 是否被发送给日志系统取决于显式配置；官方提供 `turn_off_message_logging` 等开关：[Proxy logging](https://docs.litellm.ai/docs/proxy/logging)。

若未来启用 LiteLLM adapter，至少应：

- 不配置正文 logging callback；
- 显式关闭 message/raw request-response logging；
- 开启异常信息脱敏；
- 不使用 detailed debug 处理私人流量；
- pin 精确、已通过本项目契约测试的版本。

官方许可证说明 `enterprise/` 之外的内容使用 MIT，enterprise 内容另有许可：[LICENSE](https://github.com/BerriAI/litellm/blob/main/LICENSE)。若借鉴源码，需要保留相应版权和许可声明，不能把 enterprise 代码当作 MIT。

LiteLLM Proxy 的可选依赖覆盖多种云 SDK、数据库、遥测和管理能力，功能面远大于本项目个人本地网关：[pyproject.toml](https://github.com/BerriAI/litellm/blob/main/pyproject.toml)。这不是缺点，但意味着在这里只为三类 OpenAI-compatible adapter 引入整个 proxy 栈并不经济。

## 决策表

| 场景 | 选择 |
| --- | --- |
| 普通应用只要求统一 OpenAI-compatible JSON | 可直接使用 LiteLLM |
| 需要大量非 OpenAI provider adapter | 优先评估 LiteLLM |
| My_Memory 严格透明 chat/stream/tools/reasoning | 使用 Model Gateway 原始数据面 |
| 固定单个第三方 HTTP API，不需要 route/fallback | LiteLLM pass-through 可用 |
| 将来新增非 OpenAI-compatible provider | 把 LiteLLM 作为可选 normalized adapter，并单独测试 |

## 升级前契约测试

任何 LiteLLM 接入都至少验证：

1. 非流未知顶层和嵌套字段；
2. SSE 原始 frame、usage-only chunk 与 `[DONE]`；
3. tools/tool_calls/tool result；
4. 多轮 `reasoning_content` 与 reasoning origin；
5. 429 `Retry-After`、鉴权、余额、5xx 和中途断流；
6. strict affinity 不 fallback；
7. embedding 响应扩展字段、向量空间和维度；
8. Header、usage 与实际 deployment 归因；
9. 日志和数据库中不存在 prompt、回复、工具参数或 embedding 输入。
