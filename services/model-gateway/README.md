# Model Gateway

独立的本地模型连接与路由服务。它把“模型是谁”和“从哪里调用、用哪个账号付费”分开管理，再向 My_Memory 或其他应用提供稳定的 OpenAI-compatible `/v1` 接口。

项目默认只监听 `127.0.0.1:2030`。上游密钥保存在项目目录外，代理不记录 prompt、回复、工具参数、embedding 输入或知识正文。

## 核心设计

配置分为五层，再附加独立的价格目录：

- `server`：本地监听地址、端口、请求体上限和磁盘软/硬保留量；
- `client`：调用网关的本地应用身份、Bearer key 与 route 权限；
- `connection`：真实渠道账号、Base URL、上游密钥引用、套餐范围和 adapter；
- `deployment`：该 connection 上的精确上游模型 ID、能力、推理默认值和可选渠道 profile；
- `route`：业务功能名与有序 fallback deployments；
- `pricing`：绑定 deployment 的官方价格快照，不用相似模型或第三方聚合价代替。

实际密钥不是 `config.json` 对象，只存在用户配置目录的 `secrets.env`。详细字段见[配置标准](docs/configuration.md)。

## 日常使用：只打开一个菜单

安装完成后，日常不需要记住 `connection`、`deployment`、`route` 等命令。直接运行：

```bash
modelgw
```

如果还没有安装到 PATH，也可以在项目目录运行：

```bash
.venv/bin/modelgw
```

终端会打开“本地模型服务”菜单。菜单使用下面这些用户概念：

- **渠道**：你实际购买或领取 API Key 的地方；
- **模型**：渠道页面显示的精确模型 ID；
- **用途**：日常聊天、整理记忆、查找知识、语义搜索等；
- **优先顺序**：第一个模型不可用时，依次尝试后面的备用模型；
- **用量与价格**：只记录 Token、实际渠道和人工核对过的官方价格，不记录聊天正文。

第一次使用通常只需依次选择：

1. `添加渠道和模型`；
2. 让第一个聊天模型承担全部文字工作；
3. 如有向量模型，再添加并安排给“语义搜索”；
4. `连接到记忆服务`；
5. 接受提示启动模型服务，并按需重启记忆服务。

“连接到记忆服务”会自动生成一枚独立的本地 client key，通过标准输入安全交给相邻项目的 `memgw`，不会把密钥放进命令参数、日志或项目文件。模型设置仍保存在独立 Model Gateway 中；My_Memory Web 控制台可以通过受控管理接口替换已有渠道密钥和调整已有 route，但不会把这些密钥复制进记忆项目。

已有的完整子命令继续保留，供自动化、精细配置和排障使用。显式运行 `modelgw menu` 也会打开同一菜单。

如果由 AI/Agent 帮忙，可让它生成一份不含密钥、符合仓库根 `docs/ai-quickstart.schema.json` 的 JSON 配置单，然后执行：

```bash
printf '%s\n' "$USER_PROVIDED_API_KEY" | \
  modelgw quickstart --config /tmp/memory-platform-quickstart.json --json
```

配置单会严格拒绝未知字段，因而不能把 `api_key` 或 `secret` 混入文件；供应商 API Key 只从标准输入读取。旧的 `quickstart --non-interactive --channel ...` 参数方式继续支持。

想先确认某个 key 当前能看到哪些模型，可只读发现，不保存渠道也不发送推理：

```bash
printf '%s\n' "$USER_PROVIDED_API_KEY" | \
  modelgw discover --preset deepseek --non-interactive --json
```

可用预设为 `deepseek`、`kimi-cn`、`mimo` 和 `dashscope-cn`；自定义渠道改用 `--base-url https://...`。交互式 `modelgw quickstart` 会自动执行同类 `/models` 读取并显示模型编号，失败时才回退为手工输入精确模型 ID。

quickstart 默认拒绝覆盖已有的 `memory.` / `knowledge.` 文字用途 route。交互模式会显示现有 route 并要求确认；自动化必须在 recipe 中显式设置 `replace_existing_routes=true`，或在参数模式中加 `--replace-existing-routes`。已有多渠道 fallback 时优先使用精细 `modelgw route`，不要用 quickstart 收缩配置。

## 安装与 PATH

```bash
cd /path/to/Memory_Platform/services/model-gateway
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/modelgw init
.venv/bin/modelgw install-path
```

macOS/Linux 也把环境变量叫作 `PATH`。`install-path` 默认创建 `~/.local/bin/modelgw`；如果该目录尚未在 PATH，按命令输出把下面一行加入 `~/.zshrc`，再重新打开终端：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Windows 会创建用户级 `modelgw.cmd` 并提示需要加入用户 `Path` 的目录。`install-path` 不会擅自修改 shell 配置；也可以始终使用 `.venv/bin/modelgw`。

## 高级方式：使用完整子命令配置

下面的 `PROVIDER_NAME`、`PROVIDER_HTTPS_BASE_URL` 和 `UPSTREAM_MODEL_ID` 都是待替换的占位符，不是可直接调用的真实配置。命令不会包含或回显任何真实 API Key。

先创建 My_Memory 的本地客户端身份；`--set-secret` 会用无回显提示读取你自行生成并保存到密码管理器的本地 Bearer key。该 key 至少 32 字节且只含 URL-safe 字符，推荐用 `secrets.token_urlsafe(32)` 生成：

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

`--route` 支持 glob，但对 `memory-gateway` 使用通配权限属于用户显式自定义，不是推荐默认值；平台安装与 quickstart bootstrap 都只授予上面的八条精确 route。

再按供应商官方文档填写连接。普通 OpenAI-compatible 渠道使用 `generic`；只有确实需要已实现的推理参数兼容规则时才选 `kimi`、`deepseek`、`mimo` 或百炼 Qwen 使用的 `dashscope_openai`。后者把通用推理开关转换成 DashScope 的 `enable_thinking`，不会依赖用户手写 request transform：

```bash
modelgw connection add provider-account \
  --vendor PROVIDER_NAME \
  --base-url PROVIDER_HTTPS_BASE_URL \
  --secret-name PROVIDER_ACCOUNT_API_KEY \
  --adapter generic

modelgw secret set provider-account
```

`secret set` 默认只调用该 connection 的 `GET /models`，不会发起推理。添加 deployment 和第一条 route：

```bash
modelgw deployment add chat-primary \
  --connection provider-account \
  --model UPSTREAM_MODEL_ID \
  --kind chat \
  --reasoning-default inherit

modelgw route set memory.chat chat-primary --kind chat
modelgw doctor
modelgw start
modelgw status
```

从 schema v1 升级的短 client key 只作为显式兼容项继续可用，`modelgw doctor` 会警告对应 client ID。用 `modelgw secret set CLIENT_ID` 轮换后，兼容标记会与新 key 一起原子清除；新建 client 不接受短口令。

这里刻意没有填写价格。只有从该 deployment 对应渠道的官方价格页核对币种、计价单位、分档和生效时间后，才运行 `modelgw pricing set ...`；没有明确价格时保持未配置，不猜成免费。

也可以让一个明确指定的后台 chat deployment 从官方页面提取“待审候选”：

```bash
modelgw pricing research TARGET_DEPLOYMENT \
  --source-url 'https://OFFICIAL_CHANNEL_HOST/path/to/pricing' \
  --research-deployment RESEARCH_CHAT_DEPLOYMENT
```

默认只显示候选，不改 `config.json`。研究调用本身可能产生费用，所以会在 `usage.db` 记录 `pricing.research` 的 deployment/connection、状态、耗时、Token 与该研究 deployment 当时的价格快照；网页、提示词和回复不会入库。核对候选后再显式应用：

```bash
modelgw pricing research TARGET_DEPLOYMENT \
  --source-url 'https://OFFICIAL_CHANNEL_HOST/path/to/pricing' \
  --research-deployment RESEARCH_CHAT_DEPLOYMENT \
  --pricing-id TARGET_PRICE_SNAPSHOT \
  --apply
```

`--apply` 会要求输入包含 pricing ID 和目标 deployment 的完整确认短语；自动化中可显式加 `--yes`。如果官方文档域与 connection API 域不同，先人工确认它确属同一实际渠道，再加 `--official-host OFFICIAL_CHANNEL_HOST`；这个参数不能用来认可第三方聚合价。

## 启动与日常操作

```bash
modelgw run             # 前台运行；serve 是同一命令
modelgw start           # 后台运行
modelgw status          # 后台进程 + HTTP 健康状态
modelgw logs --lines 100
modelgw logs -f
modelgw stop
```

持久化配置与 `start` 始终只允许回环监听。仅双容器私有网络入口可显式运行 `modelgw serve --host 0.0.0.0 --container-network`；不要把该容器端口发布到宿主机或公网。

后台状态、日志和配置都属于同一个用户配置目录。`stop` 只会停止由该目录的 `modelgw start` 创建且身份匹配的进程；超时后只有显式 `--force` 才会强制终止。完整说明见[运行与检查](docs/operations.md)。

## 上游检查

```bash
modelgw check
modelgw check --connection provider-account
modelgw connection check provider-account
modelgw check --live
modelgw check --connection interactive-plan --as-interactive
modelgw check --connection interactive-plan --as-interactive --live
```

- 默认 discovery 检查最多对每个 connection 请求一次 `GET /models`，不发送推理；
- 模型没出现在列表中只记为 `connected_unlisted`，不等于模型已废弃；
- `--live` 才会对每个启用的 deployment 发送最小真实请求，可能产生费用；
- `--as-interactive` 只改变本次健康检查的用途身份，不会修改连接上的 `usage_scope`。

套餐类型（含 `token_plan` / `coding_plan`）默认允许 `backend_allowed`：提供商条款由使用者自行遵守。若你**显式**把连接设为 `interactive_only`，backend 客户端仍不会路由到它。

## 推荐给 My_Memory 的八条 route

| Route | 用途 |
| --- | --- |
| `memory.chat` | `/v1` 透明聊天代理 |
| `memory.extract` | 长期记忆提取 |
| `memory.compact` | 较早会话上下文压缩 |
| `memory.core` | 核心记忆整理 |
| `memory.review` | 记忆体检与修改建议 |
| `knowledge.fast` | 知识检索快速阶段 |
| `knowledge.pro` | 复杂知识检索升级阶段 |
| `memory.embedding` | 记忆与知识 embedding |

route 的 targets 顺序就是优先级。Model Gateway 接受明确的 deployment ID，不解释旧项目里的 `MKD` 首字母缩写。完整示例与 embedding 向量空间约束见[配置标准](docs/configuration.md#my_memory-推荐路由)。

八条 route 配好后，在 My_Memory 中输入创建 backend client 时使用的同一枚本地 key：

```bash
memgw init --no-import-env
memgw secret set gateway
memgw secret set model-gateway
memgw doctor
memgw start
```

`memgw secret set model-gateway` 默认连接本机 `http://127.0.0.1:2030/v1` 并免费读取 `/models`。若启用了 `memory.embedding`，还要把 deployment 的精确 `embedding_space` 写入 My_Memory；可用 `modelgw --json deployment list` 查看：

```bash
memgw config set MODEL_GATEWAY_EMBEDDING_SPACE_ID '<exact-embedding-space>'
```

该空间为空、响应 Header 缺失或 route 后续换成另一空间时，My_Memory 会停用旧向量并安全回退关键词/FTS，不会把不同模型的向量混在一起。

## 客户端 API

- `GET /health`、`GET /healthz`：进程和配置热加载状态；
- `GET /readyz`：至少有一个带有效本地 key 的 backend/interactive client 能使用一条命中有效 provider key 的 enabled route，配置最近一次 reload 成功，且 config/secrets/usage 存储可写并高于软保留量，才返回 200；不发 provider 请求，失败只返回 `disk_low` / `disk_unavailable` 等安全 reason code；
- `GET /v1/models`：当前 client 可使用的 route；
- `POST /v1/chat/completions`；
- `POST /v1/embeddings`。

配置控制面使用独立路径：

- `GET /admin/configuration`：任何已鉴权 client 只能查看其 route 权限覆盖到的脱敏 connection/deployment/route；admin 可查看全部；
- `POST /admin/routes/validate`：仅 admin，按当前 revision 校验已有 route 的 targets/enable 草稿；
- `PUT /admin/routes`：仅 admin，revision 未变化且完整 `GatewayConfig` 校验通过后原子应用，服务自动热加载；
- `PUT /admin/connections/{id}/secret`：仅 admin，单向替换 connection 引用的渠道密钥，响应永不回显；
- `POST /admin/connections/{id}/check`：仅 admin，只执行 discovery `/models` 检查，不发起推理。
- `POST /admin/channels/discover`：既可检查已有 connection 的候选 key，也可提交尚未落盘的 flat channel draft（渠道、Base URL、adapter/dialect、鉴权与私网声明）做一次只读 discovery；绝不落盘；
- `POST /admin/channel-bundles/validate`：完整校验 connection、候选 secret、deployments、pricing 和 route operations，但不写入；
- `POST /admin/channel-bundles/apply`：再次 discovery 后按 revision CAS 一次提交；route operation 支持 `keep`（默认）、`prepend`、`append`、`replace`；
- `PATCH /admin/connections/{id}`、`PATCH /admin/deployments/{id}`：启用或禁用对象；`DELETE /admin/{connections|deployments|pricing}/{id}` 只删除未引用对象。

这些写入口与 CLI 使用同一跨进程锁和崩溃恢复事务。渠道 key 替换会先验证候选 key；失败返回非 2xx，旧 key 与配置保持不变。admin 配置视图会显示未被 route 引用的对象，非 admin 仍只看到授权 route 可达的脱敏子图。

统一运行栈安装（`memgw stack install`、`scripts/setup.sh`、容器首启）会自动创建 `memory-console-admin` 身份，并将新 admin key 只写入宿主私有凭据目录中的 `0600` 文件；终端与服务日志仅报告文件路径，不回显密钥。需要手工重建或重设时：

```bash
modelgw client add memory-console-admin \
  --kind admin \
  --set-secret
# 已存在、只需更换密钥时：
modelgw secret set memory-console-admin
```

该 admin key 只在 Web 页面当前内存中使用，不应与 My_Memory backend client key、`GATEWAY_API_KEY` 或任何上游渠道 key 复用。Model Gateway 应绑定本机回环地址；如果必须跨主机开放管理接口，应放在 HTTPS 之后。My_Memory 会拒绝把 admin key 转发到非回环的明文 HTTP 地址。

面向 My_Memory 用户，推荐不再分别操作两个 CLI，而由统一运行栈安装、接线和迁移：

```bash
memgw stack install --model-gateway-source /path/to/Memory_Platform/services/model-gateway --start
memgw stack status
memgw stack backup --output memory-stack.zip
```

安装命令会把 Model Gateway 复制到 My_Memory 的 Python 运行环境，并为 backend client 生成独立密钥；Model Gateway 的配置与进程仍保持物理隔离。便携备份包含 `config.json` 和 `usage.db`，但绝不包含 `secrets.env`。`modelgw doctor` 会把多个 client 复用同一密钥判为错误，且只报告 client ID，不显示密钥值。

成功上游响应会分别给出实际命中的 route、deployment、connection、`Channel-Operator`、`Model-Author` 和上游模型 Header；因此“DeepSeek 的模型经硅基流动调用”不会被误记成 DeepSeek 官方账单。工具轮次需要同一 deployment 时，客户端可发送严格亲和 Header：

```text
X-Model-Gateway-Require-Deployment: chat-primary
X-Model-Gateway-Reasoning-Origin-Deployment: chat-primary
```

严格亲和失败返回 `409 model_gateway_affinity_unavailable`，不会悄悄换 provider 并误用另一家的私有推理状态。Header 的完整生命周期见[客户端协议](docs/client-protocol.md)。

## 透明转发与用量

- 请求只替换上游 `model`、鉴权，以及 adapter/deployment 明确声明的兼容参数；未知 JSON 字段继续保留；
- 请求声明的流式、工具、并行工具、多模态、推理和 JSON 输出能力会在发送前过滤 deployment；无兼容目标时返回稳定 422；推理开启时默认只允许省略/`none`/`auto` 的 `tool_choice`，`required` 或具体函数对象会在付费调用前被拒绝，adapter 不静默改写；
- 成功上游响应正文和 SSE chunk 按原始字节转发；
- 流式请求只在下游收到首字节前 fallback，开始输出后不拼接另一家响应；
- 只有连接建立失败会自动切换；读/写超时可能已经计费，网关不会自动重发；
- 上游重定向不会携带凭据跟随；
- `usage.db` 用 `usage_events` 记录逻辑请求、用 `attempt_events` 记录每次真实上游发送的有限元数据与逐渠道费用；受信 backend 可附加 correlation/operation/opaque user tag 并查询中央事实，不保存正文或上游错误原文；
- 401/402/429 按 connection 熔断，结构化模型不存在和连续 5xx 按 deployment 熔断，并在每个 attempt 前复查；
- 上游 URL、敏感转发 Header、环境代理、discovery 数量和响应字节都有统一安全边界；私有或 RFC 2544（Clash/Surge fake-ip 的 `198.18.0.0/15`）地址必须显式声明允许 CIDR，默认拒绝；
- 缺 usage、官方价格或某类单价时，费用保持不完整；embedding 只要求输入 Token 与输入单价。
- 原始 usage 保留 90 天后滚入日汇总，日汇总保留 365 天；`modelgw usage prune --vacuum` 可在低峰期回收空间。
- 付费上游发送前会为逐 attempt ledger 保留磁盘空间；低于硬保留量时返回 `507 model_gateway_insufficient_storage`、`attempts=0`，不会先调用 provider。上游已调用后才发生的罕见写盘竞态优先返回原上游结果，将 ledger Header 标为 `incomplete`，并令服务至少一次 not-ready。
- `pricing research` 只接受官方 HTTPS 可见文本且不跟随 redirect；精确模型、币种、单位或单价缺少逐字证据时结果保持 `unknown`。

```bash
modelgw usage summary --days 30
modelgw --json usage summary --days 30
modelgw usage prune
```

## 为什么不直接使用 LiteLLM

LiteLLM 的多 deployment、fallback、health 和 provider adapter 很有参考价值；但其统一 completion 路径会把响应转换为内部对象并重新序列化流。这里需要保留未知字段位置、`reasoning_content`、usage-only chunk 与成功 SSE 字节，因此默认数据面使用自己的薄透明代理。

LiteLLM 仍可作为未来“允许规范化”的可选 adapter，而不是严格透明 route 的必经层。证据和官方链接见 [LiteLLM 采用评估](docs/litellm-evaluation.md)。

## 进一步阅读

- [运行、后台服务、PATH 与健康检查](docs/operations.md)
- [配置对象、adapter、pricing 与 My_Memory routes](docs/configuration.md)
- [客户端 Header、严格 affinity 与 reasoning origin](docs/client-protocol.md)
- [LiteLLM 采用评估](docs/litellm-evaluation.md)

运行测试：

```bash
.venv/bin/pytest
```
