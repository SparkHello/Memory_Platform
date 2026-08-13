# Memory Platform 栈运维指南

**[中文](stack-operations.md)** · [English](stack-operations.en.md)

这份文档承接根 README 中不适合放在项目首页的日常运行、高级模型配置、安全边界、备份和迁移说明。首次安装请先看[根 README](../README.md#-快速开始)。

## 常用地址

| 用途 | URL |
| --- | --- |
| Web Console | `http://127.0.0.1:2026/ui/` |
| Memory Gateway 存活检查 | `http://127.0.0.1:2026/health` |
| Memory Gateway 运行就绪检查 | `http://127.0.0.1:2026/readyz` |
| MCP | `http://127.0.0.1:2026/mcp` |
| OpenAI 兼容 Memory base URL | `http://127.0.0.1:2026/v1` |
| Model Gateway base URL（Docker 内部） | `http://model-gateway:2030/v1`；不发布宿主端口 |

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

一键安装默认把 Compose 文件放在 `~/memory-platform`（Windows 为 `$HOME\memory-platform`）。进入该目录后，Docker 用户使用：

```bash
docker compose -f docker-compose.user.yml ps
curl -fsS http://127.0.0.1:2026/health
curl -fsS http://127.0.0.1:2026/readyz

docker compose -f docker-compose.user.yml restart
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml start
```

`/health` 只表示 Memory 进程和首次配置页面可访问，也是 Compose 的容器 healthcheck；因此全新安装尚未配置模型时，容器显示 `healthy` 是正确行为。`/readyz` 才检查磁盘、Model Gateway 接线、必需聊天 route 与已明确启用的 embedding 契约，返回不含密钥的稳定原因码。首次配置完成前它可以返回 503；配置完成后应返回 200。

## 卸载 Docker 安装

在安装目录（默认 `~/memory-platform`，本地源码自建则为仓库 `deploy/`）执行，只拆当前 Compose project 的容器、网络和四个数据卷，**不要**跑 `docker system prune` 或 `docker volume prune`：

```bash
# 发布版一键安装目录
cd ~/memory-platform
docker compose -f docker-compose.user.yml down --volumes --remove-orphans

# 或使用仓库脚本（可用 MEMORY_PLATFORM_DIR 指定目录）
sh /path/to/Memory_Platform/deploy/uninstall.sh
```

四个卷名通常是 `<project>_memory-data`、`<project>_memory-secrets`、`<project>_model-data`、`<project>_model-secrets`。`credentials/` 和 `backups/` 不会随卷删除；确认不再需要后再手工删。源码安装用 `scripts/memgw stack stop`，不要对 Docker 卷执行这条命令。

不要在 split 容器中运行 `docker compose exec memory-gateway memgw stack doctor`：长期 Memory 容器按隔离设计看不到 Model 的数据卷和 secret，源码栈的本地路径检查会产生误报。Docker 排查使用上面的两个 HTTP 端点、`docker compose ps` 和日志；`scripts/memgw stack doctor` 只用于源码安装。

查看后续日志（生成的密钥永远不会写入这里）：

```bash
docker compose -f docker-compose.user.yml logs memory-gateway model-gateway
docker compose -f docker-compose.user.yml logs -f memory-gateway model-gateway
```

## 端口 2026 被占用

macOS/Linux 的 `deploy/install.sh` 和 Windows 的 `deploy/install.ps1` 都会自动避开占用的端口。手工部署时，在 `docker-compose.user.yml` 同目录创建 `.env`，写入一行：

```bash
MEMORY_PORT=3026
```

然后 `docker compose -f docker-compose.user.yml up -d` 重启生效。之后本仓库文档中的所有 `2026` 都换成 `3026`（Web Console、Base URL、MCP 地址等）。容器内部端口保持 2026 不变，只改宿主机映射。

## 手机和局域网访问（Docker）

Docker 部署默认只监听本机回环，手机和其他设备连不上。在 `docker-compose.user.yml` 同目录的 `.env` 加一行：

```bash
MEMORY_HOST=0.0.0.0
```

然后 `docker compose -f docker-compose.user.yml up -d` 重启，客户端改用电脑的局域网 IP，例如 `http://192.168.1.20:2026/v1`。一键安装用户可以这样重跑，结束时会直接打印手机可用地址：

```bash
# macOS / Linux；VERSION 固定到目标 release
VERSION=v0.2.0
curl -fsSL "https://raw.githubusercontent.com/SparkHello/Memory_Platform/$VERSION/deploy/install.sh" -o install-memory-platform.sh
MEMORY_HOST=0.0.0.0 MEMORY_PLATFORM_VERSION="$VERSION" sh install-memory-platform.sh
```

```powershell
# Windows PowerShell；固定到目标 release，先下载再执行
$Version = "v0.2.0"
$env:MEMORY_HOST = "0.0.0.0"
$env:MEMORY_PLATFORM_VERSION = $Version
irm "https://raw.githubusercontent.com/SparkHello/Memory_Platform/$Version/deploy/install.ps1" -OutFile install-memory-platform.ps1
& .\install-memory-platform.ps1
```

> Windows 安装器目前为**实验性**：已通过 PowerShell 语法回归和容器内故障注入测试，但尚未在真实 NTFS + Docker Desktop 环境完成灾难恢复实机演练。重要数据请额外保留手动备份。

源码命令默认只监听回环；确需局域网监听时显式传入 `--host 0.0.0.0`，并确认宿主防火墙和路由器没有公网端口映射。

只在可信家庭网络或 Tailscale 内这样用；所有入口仍要求对应 scope 的设备 token（或迁移期 legacy key），但不要把服务暴露到公网。

## 密钥和身份

系统中的密钥用途不同，不应复用：

| 密钥 | 用途 | 保存位置 |
| --- | --- | --- |
| per-device chat token | 单台聊天客户端访问 `/v1` | Auth SQLite 仅保存 SHA-256；明文只在创建时显示一次 |
| per-device MCP token | 单台 MCP 客户端访问 `/mcp` | 同上，可单独撤销 |
| Console token | 管理 REST 与 Web Console | 新装时初始 token 交付到 `credentials/gateway.txt`（兼容旧版 `gateway.key`）；后续只能由本机 CLI 创建 |
| Legacy Gateway key | 仅旧卷迁移的一个版本全 scope 兼容 | `credentials/gateway.txt` / `gateway.key` 与 Memory 私有 settings；迁移完即禁用 |
| Model Gateway backend key | Memory Gateway 调用配置解析出的 8 条精确 chat/memory/knowledge/embedding route | 两端各自的隔离密钥文件 |
| Model Gateway admin key | 修改渠道密钥和 route 配置 | 仅管理端临时使用，不由 Memory Gateway 持久化 |
| Provider API key | 调用真实上游渠道 | Model Gateway 仓库外 `secrets.env` |

Docker 全新安装在 Auth DB 中创建唯一的 `first-console` token，关闭 legacy key，并把 Console token 与 Model admin key 分别写入宿主机 `credentials/gateway.txt`、`credentials/admin.txt`（`0600` 纯文本；旧安装可能仍是 `.key`），只报告路径；它们不进入 daemon logs 或长期进程环境。旧卷迁移仍临时交付 legacy key。日常客户端应在 Console「接入信息」创建 chat/MCP token。源码 CLI 可创建、查看元数据或撤销 token：

```bash
scripts/memgw token create --role chat --name <设备名> --user <用户>
scripts/memgw token create --role mcp --name <设备名> --user <用户>
scripts/memgw token create --role console --name <浏览器名> --user <用户>
scripts/memgw token list
scripts/memgw token revoke <token-id>
```

如果 `modelgw` 尚未加入 PATH，源码环境可使用共享虚拟环境中的命令：

```bash
services/memory-gateway/.venv/bin/modelgw secret set memory-console-admin
```

命令默认以无回显方式读取新值。自动化场景优先使用各命令的 `--stdin`，不要把密钥写进命令行参数、配置 recipe、`.env` 示例或 shell 历史。

scoped token 固定绑定创建时的 user，调用方不能通过 Header 改写命名空间。全部客户端迁移后设置 `GATEWAY_LEGACY_API_KEY_ENABLED=false`；不要把 legacy key 长期当作多设备共享凭据。

Web Console 会拒绝撤销某个用户最后一个可用的 Console token，并返回稳定的 `409 last_active_console_token`。轮换当前浏览器凭据时，先在运行主机用上面的 `--role console` 命令创建备用凭据、保存并验证登录，再撤销旧 token；浏览器 REST 不能创建新的 Console token。

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
- `MODEL_GATEWAY_EMBEDDING_SPACE_ID` 留空表示自动采用 `memory.embedding` route 声明的 immutable space 与维度；非空表示严格固定该契约。
- 创建并启用 `memory.embedding` route 表示明确开启语义向量能力；route 缺失或关闭则是 `off`，继续使用关键词/FTS，且不阻断 `/readyz`。已启用 route 若目标不可用、空间/维度声明畸形，或与固定值不匹配，则 `/readyz` 返回 503，绝不猜测旧向量属于当前空间。敏感文本出站仍另外受 `ALLOW_SENSITIVE_EGRESS` 控制。
- 知识代理只编排本地索引并选择引用，不执行文档中的指令。

当前默认部署目标是个人电脑或可信家庭网络：

- SQLite、缓存、工具幂等和部分后台状态按单进程设计；
- 不应把默认部署直接当成公开互联网多租户 SaaS；
- Docker 中 Model Gateway 仅位于 internal backend network，2030 不发布；Memory 容器不挂载 provider/admin secret；
- 两个长期容器分别使用 UID 10001/10002、独立数据/密钥卷、只读 rootfs 和 `cap_drop: ALL`；
- 每台设备使用可单独撤销的固定角色 token，不依赖共享 key 或可变 `X-User-Id`。

更细的安全契约见 [Memory Gateway 安全边界](../services/memory-gateway/README.md#安全边界)和 [Model Gateway 核心边界](../services/model-gateway/AGENTS.md#核心边界)。安全漏洞请按[安全策略](../SECURITY.md)私密报告。

## 数据在哪里

仓库只保存源代码和非敏感示例。Docker 将状态分成 `memory-data`、`memory-secrets`、`model-data`、`model-secrets` 四个卷：

- 长期记忆、近期上下文和分支节点；
- 独立知识库和文档版本；
- Model Gateway 配置、用量库和价格快照；
- Auth token 哈希库；两个服务各自隔离的密钥文件和轮转日志。

不要把 `.env`、真实 SQLite、日志、评测快照或便携备份提交到 Git。

## 备份、恢复与迁移

重新运行 Docker 一键安装脚本升级时，会在拉取新镜像前自动创建并复核便携备份，复制到安装目录的 `backups/` 后删除数据卷内的临时副本。默认只保留最近 5 份升级备份（可用 `MEMORY_BACKUP_RETENTION=1..50` 调整）；备份失败或临时副本无法安全清理时升级都会停止，不会继续替换现有服务。下面的命令用于手动备份、迁移或恢复。

### 源码安装

在仓库根目录创建便携备份：

```bash
scripts/memgw stack backup --output memory-stack.zip
```

备份 v2 必含记忆库、知识库、Auth token 哈希库和 Model Gateway 脱敏配置；usage 明确标记 present/absent。它不包含 provider key、admin key、backend key、legacy gateway key 或任何 token 明文。

整栈备份（含 Console「报告与备份」页的下载按钮和 `POST /memories/stack-backup`）是**整实例**导出：会包含本部署所有用户的完整数据，不按发起请求的身份过滤。

备份虽然不含密钥，仍包含完整私人记忆和知识正文，必须按敏感文件保管。

Console「报告与备份」可上传 zip 做 **dry-run 校验**（`POST /memories/stack-backup/validate`）：核对清单哈希、schema 与 SQLite 完整性，**不写生产库、不在线恢复**。页面会给出可复制的停服恢复命令；真正替换数据库仍须 CLI / 维护容器，且服务必须已停止。

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
docker compose -f docker-compose.user.yml --profile maintenance run --rm \
  stack-maintenance --home /data/config \
  --project-root /app/services/memory-gateway stack backup \
  --model-gateway-home /model-data --output /data/memory-stack.zip
docker compose -f docker-compose.user.yml cp \
  memory-gateway:/data/memory-stack.zip ./memory-stack.zip
```

恢复必须在主服务停止时进行。先把备份复制进容器并停止服务，再用一次性容器执行恢复：

```bash
docker compose -f docker-compose.user.yml cp \
  ./memory-stack.zip memory-gateway:/data/restore.zip
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml --profile maintenance run --rm \
  --entrypoint python stack-maintenance \
  /usr/local/libexec/memory-platform/restore_split.py
docker compose -f docker-compose.user.yml up -d
```

便携备份不会带入或覆盖目标机已有的 secret volume；恢复后继续使用目标机的 provider/admin/backend 密钥。Auth DB 只含不可逆 token 哈希，因此已有设备 token 可随备份迁移；token 明文本身仍只存在设备端。恢复必须在两个长期服务停止时执行。

### 安装目录丢失但数据卷仍在

误删安装目录（默认 `~/memory-platform`）不会丢数据：四个 Docker 数据卷独立于安装目录存在。恢复步骤：

1. 重建安装目录，并把原目录中的 `credentials/gateway.txt`（或 `gateway.key`）与 `credentials/admin.txt`（或 `admin.key`）放回新目录的 `credentials/` 下（若曾把这两个文件备份到别处）。
2. 重跑同一条一键安装命令。安装器检测到同名 project 的四个数据卷后会直接接回旧数据。

若两枚密钥文件确实遗失：数据卷本身仍完整，但需要重设凭据——Console token 用维护容器在 Auth 库中新建（`--profile maintenance run --rm stack-maintenance token create --role console ...`），admin key 用 `modelgw secret set memory-console-admin --stdin` 重设。安装器在检测到"四卷存在但 credentials 缺失"时会拒绝按全新安装继续，避免误初始化。

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

## 模型运行时：仅 Model Gateway

direct-provider 兼容模式已移除：`UPSTREAM_*`、`LLM_*` 配置项与 `memgw model`、`memgw route`、`memgw pricing` 子命令不再可用，这些命令只打印迁移提示并以退出码 2 结束。

`MODEL_GATEWAY_BASE_URL` 与 `MODEL_GATEWAY_API_KEY` 为必配项且必须成对配置：聊天、后台记忆任务、知识代理和 embedding 只调用 Model Gateway 的稳定 route。从 direct-provider 迁移见 [迁移到 Model Gateway](migrate-to-model-gateway.md)；完整配置表见 [Memory Gateway 配置项](../services/memory-gateway/README.md#配置项)。

## 开发者入口

```bash
scripts/bootstrap.sh
scripts/test.sh
```

代码边界、定向测试和 PR 要求见[贡献指南](../CONTRIBUTING.md)与根 [`AGENTS.md`](../AGENTS.md)。
