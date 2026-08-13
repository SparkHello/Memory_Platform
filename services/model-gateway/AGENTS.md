# AGENTS.md

`Model_Gateway` 是独立于业务应用的本地模型接入网关。它管理客户端、连接、部署、功能路由、健康检查和无正文用量事件，并向调用方提供 OpenAI-compatible `/v1` 接口。

## 核心边界

- 配置必须区分 client、connection、deployment、route；模型作者不能代替接入渠道。
- 代理不得记录 prompt、回复、工具参数、embedding 输入或知识正文。
- 请求 JSON 的未知字段必须保留；成功上游响应正文与 SSE chunk 必须原样转发。
- 流式请求只允许在下游响应开始前故障切换；首个成功流建立后禁止重试，避免重复输出。
- embedding 路由中的 deployment 必须使用相同 `embedding_space` 和 `dimensions`，避免混用不兼容向量空间。
- 套餐类型不再强制 `interactive_only`；仅当连接显式 `usage_scope=interactive_only` 时 backend client 不可路由到它。提供商条款由使用者自行遵守。
- `pricing research` 只能读取用户明确给出的渠道官方 HTTPS 页面，并使用显式 `backend_allowed` chat deployment；页面是不可信资料，默认候选不写配置，应用必须明确确认。
- 价格研究的模型调用要记录 metadata-only `pricing.research` 用量和研究 deployment 的价格快照，但不得把页面、提示词、证据或回复写入 `usage.db`。
- 测试只能使用 `httpx.MockTransport` 和临时目录，不调用真实 provider。
- 密钥只存在用户配置目录的 `secrets.env`，不得写入 `config.json`、日志、测试或版本库。
- `/admin/configuration` 的只读视图必须按 client route 权限过滤且不返回 `secret_ref`；配置写入（含 `POST /admin/connections` 新建渠道、`POST /admin/deployments` 新建部署并指派路由）、渠道密钥替换和 discovery 检查只允许 `kind=admin`。所有写入继续使用 revision 冲突检测、完整 `GatewayConfig` 校验和原子替换，新建实体由服务端合并进现有配置后整图校验一次；请求体校验错误不得回显密钥值。管理接口默认只绑定回环地址；跨主机暴露时必须使用 HTTPS，My_Memory 不得把 admin key 转发到非回环明文 HTTP。
- 每个已配置 client 必须使用不同密钥；身份冲突会让鉴权拒绝请求，`modelgw doctor` 必须在不输出密钥值的情况下提前报告冲突 client ID。My_Memory 的 `memgw stack install` 可以安全轮换并同步 backend key，但不得覆盖或导出 admin/upstream key。

## 常用命令

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/modelgw --home /tmp/modelgw-test init
```

AI/Agent 首次配置优先使用不含密钥的 quickstart recipe：`modelgw quickstart --config <json> --json`。recipe 契约位于仓库根 `docs/ai-quickstart.schema.json`；供应商 API Key 只能从 stdin 读取，不得加入 JSON、命令参数、日志或测试夹具输出。
