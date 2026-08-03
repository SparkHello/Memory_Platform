# 模型目录、功能路由与价格目录

`memory-gateway` 把可变的模型元数据和价格从 Python 逻辑中拆成标准 JSON 文档。内置文件位于 `app/catalog/`；`memgw init` 会把它们复制到用户配置目录，日常编辑不会污染仓库。

## 文件与优先级

| 文件 | 环境变量 | 作用 |
| --- | --- | --- |
| `models.json` | `MODEL_CATALOG_PATH` | 模型 ID、provider 适配器、真实 API model 名、能力和官方页面。 |
| `routes.json` | `MODEL_ROUTES_PATH` | 每项功能的模型顺序和故障切换。 |
| `pricing.json` | `PRICING_CATALOG_PATH` | 官方公开 API 原价、币种、Token 分档、来源和核对日期。 |

外部目录按 ID/key 覆盖内置目录。未设置 `MODEL_ROUTES_PATH` 时，运行时完整沿用旧 `LLM_PROVIDER_PRIORITY=MKD`；因此升级不会强制迁移现有部署。

## 模型文档

模型 ID 使用 `provider/model` 形式。`provider` 决定复用哪组 base URL 和 API Key，也选择少量必要的协议兼容逻辑。

```json
{
  "$schema": "./models.schema.json",
  "version": 1,
  "models": [
    {
      "id": "kimi/kimi-k2.7-code",
      "provider": "kimi",
      "model": "kimi-k2.7-code",
      "kind": "chat",
      "capabilities": ["streaming", "tools", "reasoning"],
      "official_url": "https://platform.kimi.com/docs/pricing/chat-k27-code"
    }
  ]
}
```

当前支持的适配器：

- `mimo`：读取 `LLM_MIMO_*`。
- `kimi`：读取 `LLM_KIMI_*`。
- `deepseek`：读取 `LLM_DEEPSEEK_*`。
- `upstream`：读取 `UPSTREAM_*`，用于一个自定义 OpenAI-compatible 上游。
- `embedding`：读取 `EMBEDDING_*`，用于当前 OpenAI-compatible embedding 上游。

模型目录表达能力和选择，不保存 API Key。新增同一适配器下的模型通常不需要修改 Python；新增鉴权、端点、推理、工具或流式协议不同的 provider 时，仍要实现 adapter 并补透明转发测试。

## 功能路由

路由值是严格有序的模型 ID 数组。运行时跳过没有 API Key 的模型，并对 provider 级故障按顺序切换。

```json
{
  "$schema": "./routes.schema.json",
  "version": 1,
  "routes": {
    "chat": ["mimo/mimo-v2.5-pro-ultraspeed", "deepseek/deepseek-v4-flash"],
    "memory.extract": ["deepseek/deepseek-v4-flash"],
    "memory.review": ["kimi/kimi-k2.7-code-highspeed"],
    "knowledge.pro": ["deepseek/deepseek-v4-pro"]
  }
}
```

`knowledge.pro` 当前只能使用 `deepseek` 或 `upstream` 适配器。429 冷却仍按实际 provider/key/base URL 在当前进程共享，不会动态重写用户路由。

CLI 支持首字母简写，`M`、`K`、`D` 分别表示默认 MiMo、Kimi、DeepSeek 模型；多个字母的顺序就是优先级。`knowledge.pro` 中的 `D` 指向 DeepSeek Pro，其余路由的 `D` 指向 Flash。也可以使用完整模型 ID，或省略模型参数进入编号选择：

```bash
memgw route guide
memgw route set chat MKD
memgw route set memory.review K D
memgw route set memory.review kimi/kimi-k2.7-code-highspeed D
memgw route set memory.core
```

八个路由的含义可通过 `memgw route guide` 查看。`chat` 用于透明代理，`memory.*` 分别服务提取、压缩、核心整理和体检，`knowledge.fast/pro` 用于两阶段知识检索，`pricing.research` 用于官方价格候选提取。

## 模型连接检查

保存远程 provider 的 API Key 后，`memgw secret set` 会自动请求该 provider 的 `GET /models`，检查网络、鉴权以及目录内模型是否可见，不发送聊天或 embedding 推理。provider 不支持 `/models` 时会显示警告，不会把它误报为鉴权失败。

```bash
memgw model check
memgw model check --provider kimi
memgw model check --live
```

前两个命令不发起推理。`--live` 会先确认，再对每个已配置模型发送一个最小聊天或 embedding 请求，因此可能产生少量费用；自动化中明确接受费用时可加 `--yes`。检查结果和日志不会打印 API Key。

## 价格文档

金额字段统一表示“每百万 Token 的官方公开 API 原价”，使用十进制字符串避免浮点误差：

```json
{
  "$schema": "./pricing.schema.json",
  "version": 1,
  "as_of": "2026-08-02",
  "currency": "CNY",
  "models": [
    {
      "key": "kimi:kimi-k2.7-code",
      "provider": "kimi",
      "provider_label": "Kimi",
      "model": "kimi-k2.7-code",
      "kind": "chat",
      "input_cache_hit_per_million": "1.30",
      "input_cache_miss_per_million": "6.50",
      "output_per_million": "27",
      "source_url": "https://platform.kimi.com/docs/pricing/chat-k27-code"
    }
  ]
}
```

阶梯价通过 `input_token_min`、`input_token_max` 和不同的唯一 `key` 表达。`input_token_max` 是不包含的上界。缺少上游 usage 或无法精确匹配官方模型 ID 时，事件必须保持未计价，不能显示为免费。

每个成功调用会把当时命中的单价、来源和日期复制进用量事件。以后修改价格目录只影响新事件，不重写历史金额。

## 安全的价格研究流程

`memgw pricing research` 只接受用户提供或模型目录记录的 HTTPS 官方页面：

1. 本地读取页面可见文本，不读取记忆库或知识库。
2. 把页面作为不可信资料交给 `pricing.research` 路由中的模型。
3. 模型只返回结构化候选和简短依据。
4. CLI 先显示完整候选；没有 `--apply` 时绝不写入。
5. 即使带 `--apply`，默认仍要求用户对照官方页面确认。

搜索摘要、第三方聚合价格、相似模型价格、套餐、赠金和限时折扣都不能作为价格目录的权威来源。

## 常用命令

```bash
memgw model list
memgw model check
memgw route list
memgw route guide
memgw pricing list
memgw doctor
```

所有用户目录写入都使用临时文件加原子替换，旧版本保留为同目录 `.bak`。API Key 不会显示在 `config show`、日志或模型/价格文档中。
