# Memory Platform 栈运维指南

**[中文](stack-operations.md)** · [English](stack-operations.en.md)

这份文档承接根 README 中不适合放在项目首页的日常运行、高级模型配置、安全边界、备份和迁移说明。首次安装请先看[根 README](../README.md#-快速开始)。

## 常用地址

| 用途 | URL |
| --- | --- |
| Web Console | `http://127.0.0.1:2026/ui/` |
| Memory Gateway 健康检查 | `http://127.0.0.1:2026/health` |
| MCP | `http://127.0.0.1:2026/mcp` |
| OpenAI 兼容 Memory base URL | `http://127.0.0.1:2026/v1` |
| Model Gateway base URL | `http://127.0.0.1:2030/v1` |

## 日常运行与检查

源码安装在仓库根目录运行：

```bash
scripts/memgw stack status
scripts/memgw stack doctor

scripts/memgw stack start
scripts/memgw stack restart
scripts/memgw stack stop
```

`stack doctor` 会同时检查两个服务、运行目录和 Memory Gateway 到 Model Gateway 的接线。只排查某个服务时，再进入对应服务文档：

- [Memory Gateway 完整说明](../services/memory-gateway/README.md)
- [Model Gateway 运行与检查](../services/model-gateway/docs/operations.md)

只下载了 `docker-compose.user.yml` 的 Docker 用户使用：

```bash
docker compose -f docker-compose.user.yml ps
docker compose -f docker-compose.user.yml exec memory-platform memgw stack doctor

docker compose -f docker-compose.user.yml restart
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml start
```

查看首次启动输出和后续日志：

```bash
docker compose -f docker-compose.user.yml logs memory-platform
docker compose -f docker-compose.user.yml logs -f memory-platform
```

## 密钥和身份

系统中的密钥用途不同，不应复用：

| 密钥 | 用途 | 保存位置 |
| --- | --- | --- |
| Memory Gateway API key | 客户端访问 `/v1`、MCP、REST 和 Web Console API | Memory Gateway 用户配置目录 |
| Model Gateway backend key | Memory Gateway 调用允许的 `memory.*`、`knowledge.*` route | 两端各自的仓库外密钥文件 |
| Model Gateway admin key | 修改渠道密钥和 route 配置 | 仅管理端临时使用，不由 Memory Gateway 持久化 |
| Provider API key | 调用真实上游渠道 | Model Gateway 仓库外 `secrets.env` |

首次安装会各打印一次客户端 `GATEWAY_API_KEY` 和 Model Gateway admin key。它们丢失后不能回显旧值，只能重设：

```bash
scripts/memgw secret set gateway
modelgw secret set memory-console-admin
```

如果 `modelgw` 尚未加入 PATH，源码环境可使用共享虚拟环境中的命令：

```bash
services/memory-gateway/.venv/bin/modelgw secret set memory-console-admin
```

命令默认以无回显方式读取新值。自动化场景优先使用各命令的 `--stdin`，不要把密钥写进命令行参数、配置 recipe、`.env` 示例或 shell 历史。

`GATEWAY_API_KEY` 默认绑定固定的 `GATEWAY_USER_ID`，调用方不能通过 `X-User-Id` 改写命名空间。`GATEWAY_ALLOW_USER_ID_HEADER=true` 只用于旧版共享 key 迁移，不建议用于不可信网络。

## 模型 quickstart 与进阶配置

源码安装会在 `scripts/setup.sh` 中进入模型 quickstart；之后可以单独运行：

```bash
services/memory-gateway/.venv/bin/modelgw quickstart
```

quickstart 内置 DeepSeek、Kimi 中国区、MiMo 和 DashScope 北京区预设，只询问渠道、API Key、聊天模型，以及是否配置可选的语义搜索。它通过只读 `/models` 请求展示当前 key 可见的精确模型 ID，不会自动发送推理请求。

| 页面名称 | 技术名称 | 含义 |
| --- | --- | --- |
| 渠道 | connection | 实际购买 API、持有账号和密钥的服务商 |
| 模型 | deployment | 该渠道上的精确模型 ID 与能力声明 |
| 用途 | route | 聊天、记忆提取、知识检索等稳定业务名称 |
| 优先顺序 | fallback | 当前模型不可用时依次尝试的备用模型 |
| 价格 | pricing | 与具体模型绑定、经人工核对的价格快照 |

已有安装只重新配置时使用：

```bash
scripts/setup.sh --configure-only --config <文件> --json
```

配置 recipe 必须符合 [`ai-quickstart.schema.json`](ai-quickstart.schema.json)，不得包含任何密钥；供应商 API Key 只通过标准输入传入。完整的 AI/非交互流程见[AI 安装指南](ai-install.md)。

已有多渠道备用顺序或精细用途配置时，不要用 quickstart 覆盖，改用 `modelgw` 独立子命令：

- [Model Gateway README](../services/model-gateway/README.md)
- [Model Gateway 配置标准](../services/model-gateway/docs/configuration.md)
- [Model Gateway 客户端协议](../services/model-gateway/docs/client-protocol.md)

确认当前 key 可见的模型时，优先使用免费的只读发现：

```bash
modelgw discover --preset <id> --non-interactive --json
```

该命令只读取 `/models`。只有你明确接受真实推理请求及潜在费用时才使用 `--live`。

## 敏感数据出站与部署边界

- `ALLOW_SENSITIVE_EGRESS=false` 默认阻止本地识别为 private/sensitive 的内容进入远程记忆提取、embedding、AI 体检和知识代理。
- 该开关不拦截用户主动通过 `/v1` 发给聊天上游的当前消息。
- `redact_sensitive=true` 只遮罩本次响应，不会改写 SQLite 原文，也不会让备份自动脱敏。
- 记忆与知识 embedding 必须携带可信且一致的空间 ID；缺失或不匹配时回退关键词/FTS，不猜测旧向量属于当前空间。
- 知识代理只编排本地索引并选择引用，不执行文档中的指令。

当前默认部署目标是个人电脑或可信家庭网络：

- SQLite、缓存、工具幂等和部分后台状态按单进程设计；
- 不应把默认部署直接当成公开互联网多租户 SaaS；
- Model Gateway 管理接口默认只监听回环地址，跨主机暴露必须置于 HTTPS 后；
- 需要强隔离时，为不同用户使用不同凭证和实例，不要依赖共享 key 加可变 `X-User-Id`。

更细的安全契约见 [Memory Gateway 安全边界](../services/memory-gateway/README.md#安全边界)和 [Model Gateway 核心边界](../services/model-gateway/AGENTS.md#核心边界)。安全漏洞请按[安全策略](../SECURITY.md)私密报告。

## 数据在哪里

仓库只保存源代码和非敏感示例。实际运行数据位于用户配置目录或 Docker 的 `memory-platform-data` 卷，主要包括：

- 长期记忆、近期上下文和分支节点；
- 独立知识库和文档版本；
- Model Gateway 配置、用量库和价格快照；
- 两个服务各自的密钥文件和日志。

不要把 `.env`、真实 SQLite、日志、评测快照或便携备份提交到 Git。

## 备份、恢复与迁移

### 源码安装

在仓库根目录创建便携备份：

```bash
scripts/memgw stack backup --output memory-stack.zip
```

备份包含允许迁移的记忆库、知识库、Model Gateway 脱敏配置、用量库和非密钥设置，但不包含 provider key、admin key、backend key 或 Memory Gateway API key。

备份虽然不含密钥，仍包含完整私人记忆和知识正文，必须按敏感文件保管。

恢复到已安装依赖的新设备：

```bash
git clone https://github.com/SparkHello/Memory_Platform.git
cd Memory_Platform
scripts/bootstrap.sh
scripts/memgw stack restore /path/to/memory-stack.zip --yes --start
```

恢复前会校验清单哈希、SQLite 和 JSON，停止两个服务，并为被替换的本机文件创建仓库外回滚副本。目标机没有相应密钥时，需要在恢复后重新输入未进入备份的各类密钥。

### Docker 安装

只下载了 Compose 文件的用户可以在容器内备份，再复制到当前目录：

```bash
docker compose -f docker-compose.user.yml exec memory-platform \
  memgw stack backup --output /data/memory-stack.zip
docker compose -f docker-compose.user.yml cp \
  memory-platform:/data/memory-stack.zip ./memory-stack.zip
```

恢复必须在主服务停止时进行。先把备份复制进容器并停止服务，再用一次性容器执行恢复：

```bash
docker compose -f docker-compose.user.yml cp \
  ./memory-stack.zip memory-platform:/data/memory-stack.zip
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml run --rm --entrypoint memgw \
  memory-platform stack restore /data/memory-stack.zip --yes
docker compose -f docker-compose.user.yml up -d
```

便携备份不会带入或覆盖目标机已有的 `GATEWAY_API_KEY`、admin key 和供应商 API Key；恢复命令只会在内部重新接线 backend key。继续使用目标机首次启动时保存的访问密钥；缺失时按[密钥和身份](#密钥和身份)重设，并按需重新输入供应商 API Key。不要在正在运行的主容器里用 `docker compose exec` 恢复，因为前台服务仍占用数据库和端口，恢复命令会安全拒绝覆盖。

### 从旧版双目录迁移

如果旧设备仍保留 `My_Memory` 与 `Model_Gateway` 两个独立项目，优先使用旧 `My_Memory` 已提供的统一栈备份，不要手工复制 `.env` 或数据库：

```bash
cd /path/to/My_Memory
scripts/memgw stack backup --output /safe/path/memory-stack.zip

cd /path/to/Memory_Platform
scripts/bootstrap.sh
scripts/memgw stack restore /safe/path/memory-stack.zip --yes --start
```

迁移后先把旧目录保留为只读回滚来源，不要让新旧两套服务同时占用相同端口或写同一数据库。确认新栈、Web Console、记忆数量和知识文档正常后，再决定是否归档旧目录。

## direct-provider 兼容模式

如果暂时不运行独立 Model Gateway，Memory Gateway 仍保留旧的 `UPSTREAM_*`、`LLM_*`、`memgw model`、`memgw route` 和 `memgw pricing` 路径。

新部署推荐使用 Model Gateway。只要 `MODEL_GATEWAY_BASE_URL` 与 `MODEL_GATEWAY_API_KEY` 成对配置，聊天、后台记忆任务、知识代理和 embedding 就只调用稳定 route，不会在中央路由失败时偷偷回退到旧 `.env` provider key。

兼容模式的完整配置见 [Memory Gateway 配置项](../services/memory-gateway/README.md#配置项)。

## 开发者入口

```bash
scripts/bootstrap.sh
scripts/test.sh
```

代码边界、定向测试和 PR 要求见[贡献指南](../CONTRIBUTING.md)与根 [`AGENTS.md`](../AGENTS.md)。
