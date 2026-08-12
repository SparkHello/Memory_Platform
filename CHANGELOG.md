# Changelog

本项目遵循语义化版本。发布前的改动记录在 `Unreleased`。

## Unreleased

### Fixed

- 渠道向导「复制客户端配置」不再误用 Console token：apply 成功后自动创建 chat token，仅复制该设备密钥。
- Model Gateway 上游地址校验失败时返回可读中文原因（含 fake-ip / 198.18 提示），而不再只显示裸 `ConnectError` 类型名。
- 显式 `allowed_private_networks` 现可写入整个 RFC 2544 段 `198.18.0.0/15`（Clash/Surge TUN fake-ip），不再强制只能 `/32`。
- 多意图消息（「新事实 + 提问」混在一条）现按句多路召回取并集，不再因整句语义稀释而漏召相关记忆。
- 检索与聊天注入的记忆激活加上上限（每次最多 5 条头部命中），「被检索曝光就自增激活度」的正反馈回路被消除；聊天召回本身不再计入使用。
- mcp/console token 请求 `memory_access="read"` 不再被静默改写为 read-write，而是返回 422。
- 偏好软路径只在 `source_quote` 内匹配偏好标记，英文标记要求词边界；无关候选不再被误降门槛。
- 提取候选的实体列表清洗复合名称碎片（如「Dark Mode」不再拆出「Dark」），提示词同步约束实体完整性与中性配置偏好的 semantic 归类。
- 整栈备份的 schema 版本常量收敛为单一事实源（`app/schema_versions.py`），恢复时校验支持区间并拒绝未知版本；错误路径不再留下半写文件。
- 聊天 finalize outbox 补齐状态机：`done` 为终态不可回退、stale claim 清理、周期 drainer 兜底重试、完成后清除 payload。
- `POST /providers/live-probe` 尊重 60s 缓存并支持显式 `force`，并发探测去重；该端点不再被误分类为不可逆操作而占用破坏性操作限流预算。

### Added

- Console「报告与备份」支持下载**整栈便携备份**（`POST /memories/stack-backup`）；Docker 拆分部署可附带 `X-Model-Gateway-Admin-Key` 拉取 Model 脱敏配置。
- Model Gateway `GET /admin/portable-config`：admin 导出完整配置 JSON（仍不含 secrets.env）。
- 工作室展示「最近未写入记忆」及原因（来自决策日志 ignore）。
- 第一人称偏好句（如「我喜欢…」）对 semantic 等类型启用略低的 importance 门槛（soft path），假设/敏感门控不变。
- `GET /providers/status?live_probe=true` 与 `POST /providers/live-probe`：探测 memory.chat 上游连通性（约 60s 缓存）；Console「模型与路由」可一键探测。
- chat token 支持 `memory_access=read|read-write`：只读 token 可召回但禁止自动提取写入；接入页可选择。
- 聊天 finalize **outbox**（`chat_finalize_jobs`）：提取意图先落库，进程重启后可恢复未完成的记忆写入。
- 大库（≥2000 条）纯关键词检索改用 SQLite FTS5 索引生成候选再精排，打分与解释字段不变；小库、embedding 查询与单字/类别查询自动回退全表扫描。
- 记忆详情在正文明显偏离原始来源时显示「可能经过编辑，原文仅供追溯」提示。
- 工作台在「模型已就绪但还没有 chat token」时显示接入引导卡片：一键生成 token 并复制三行客户端配置。
- 安装器支持 `MEMORY_IMAGE_REGISTRY` 指定 GHCR 镜像加速站，GitHub 直连失败时给出代理/镜像指引；回滚校验对任意 registry 的 digest 固定引用一视同仁。
- usage 事件保留策略：Memory / Model Gateway 每日删除超过 365 天的用量明细（汇总不受影响）。

### Changed

- Memory / Model Gateway 默认关闭 `/docs`、`/redoc`、`/openapi.json`；开发可用 `MEMGW_ENABLE_OPENAPI=1` 或 `MODEL_GATEWAY_ENABLE_OPENAPI=1` 打开。
- 渠道向导增加「TUN fake-ip 代理」勾选；知识库空态说明导入文档不会自动进入普通聊天；移动底栏主导航改为含「接入信息」。
- 工作室与导航在 legacy 共享密钥仍启用时显示醒目迁移提示。
- Docker 拓扑简化：`memory-gateway` 直接发布宿主端口，Model Gateway 保持私网不发布端口。
- 安装器减负：Sigstore/Cosign 验证改为 `MEMORY_VERIFY_SIGNATURES=1` 显式开启（digest 固定不变）；Compose 拓扑校验移入 CI；升级备份收敛为停旧栈后的单次静默备份并做真实复验（ZIP CRC + SQLite quick_check）；`MEMORY_HOST` 允许任意本机 IPv4；端口被占自动顺延时显式提醒替换文档中的端口。
- README 快速开始改写为三步凭据导览；Windows PowerShell 安装器标注为实验性；文档补充安装目录丢失后的重挂载恢复步骤。
- Console token 展示统一默认掩码、显式「显示 token」切换；开发者页新增关闭 legacy key 的分步指引。

### Removed

- 删除 Model Gateway 前的 ingress TCP relay（`deploy/ingress_relay.py`）及其入口脚本接线；连接路径少一跳，安全边界由网络隔离与鉴权承担（见 `docs/security-audit-2026-08.md` 附录）。

## 0.3.0 - 2026-08-10

### Changed

- Memory Gateway **仅支持 Model Gateway** 作为模型运行时：移除 `UPSTREAM_*` / `LLM_*` direct-provider 聊天、embedding 与 Knowledge Agent 第二实现；未配置中央网关时启动路径与 `/readyz` 失败并给出迁移指引。见 [docs/migrate-to-model-gateway.md](docs/migrate-to-model-gateway.md)。
- `/v1/chat/completions` 使用显式 route 模型名（非 `memory-auto`）时保留并透传客户端自带的 `reasoning_content`；`memory-auto` 仍剥离无法证明来源的旧推理原文。
- 默认测试沙箱与 Console 简洁导航继续以双网关金路径为准；高级 UI 仍可通过专家模式显式开启，API 不删减。
- `Settings` 去掉 direct-provider 死字段（`UPSTREAM_*`、`LLM_*`、本地 model/routes catalog 路径、`pricing_catalog_path`、独立 embedding base/key/model）；`PRICING_CATALOG_PATH` overlay 一并移除，deploy 不再写入；保留中央 `MODEL_GATEWAY_*` 与 `EMBEDDING_DIMENSIONS`。
- `memgw init` / `cli_config` 不再种子化本地 models/routes 目录，导入 settings 时剥离已退役 direct-provider 环境变量；`CliPaths` 仍保留 legacy 路径供 stack backup。
- Knowledge Agent 仅通过 `ModelRuntime` 的 `knowledge.fast` / `knowledge.pro` route 调用中央网关。
- `app/memory/store` 改为 package（对外 `app.memory.store` 契约不变，类方法薄委托）：
  - `schema.py` / `schema_ensure.py` / `migrations.py` — 建表、列补齐、版本迁移
  - `crud.py` / `merge.py` — 读写合并
  - `temporal.py` — 时态链与 time-ripple
  - `export_import.py` — 导出导入恢复
  - `purge_ops.py` / `lifecycle_purge.py` — 永久删除
  - `core_memory.py` / `conversation.py` / `spaces.py` / `digest.py` / `decision_logs.py`
  - `helpers.py` / `constants.py` / `errors.py`
  - `_monolith.py` 收束为编排壳（init/connect/side-effect claim + 委托）
- `/memories` HTTP API 拆为 `app/api/memories/` 包（crud/search/core/conversation/export/graph/review/evaluation/purge/item），URL 不变；`/{memory_id}` 最后注册以免遮蔽静态路径。
- 离线 legacy 迁移不再依赖 `model_catalog`；本地 models/routes 仅可选拷贝，pricing 仍用于历史模型本地账本展示。

### Removed

- 透明聊天与内部任务对本地 `providers`/`model_catalog` 直连 failover 的运行时路径。
- Memory 进程内对直连上游的本地用量记账（中央响应改由 Model Gateway 归因；本地 `app/catalog/pricing.json` 仅作已知历史模型展示兜底）。
- `memgw model` / `route` / `pricing` 与 `memgw secret set/delete mimo|kimi|deepseek|upstream|embedding`：只打印迁移提示并以退出码 2 退出，原实现已删除；模型、路由与价格请改用 `modelgw` 或 Console「模型与路由」。
- `app/model_probe.py`、`app/model_catalog.py`、`app/providers/`、`app/catalog/models.json` / `routes.json` 及其 schema（**保留** `pricing.json` 供 usage 目录展示）。

## 0.2.0 - 2026-08-10

### Added

- Model Gateway 配置 schema v2：请求能力感知路由、显式 fallback scope、Qwen/DeepSeek V4 deployment profile、逐 attempt 账本、进程内 breaker、配置 revision/CAS/crash journal，以及零落盘渠道发现和原子 channel bundle API。
- Memory Gateway 统一中央运行时：透明聊天、八条后台 route、Knowledge Agent 与 Embedding 共用 Model Gateway，并校验完整归因、deployment 亲和、向量空间和维度；新增可操作的 `/readyz`。
- 按设备、用途隔离的 `chat`、`mcp`、`console` token，独立签名密钥、提前鉴权、限流和并发门禁；Console 可一次性创建 chat/MCP token。
- Memory/Core revision 与条件更新、持久化聊天副作用 claim、原子 restore、50K keyset 召回、所选导出，以及永久删除 preview/commit 事务。
- split Docker 栈：Memory UID 10001 与 Model UID 10002 使用独立数据/secret 卷，Model 仅在内部网络提供 2030；新增离线初始化、旧单卷迁移、portable backup v2 和自动回滚。

### Changed

- 默认 UI 收敛为面向普通用户的简洁导航，高级诊断按设备显式开启；渠道向导改为 discover → validate → apply 的单次原子提交，现有 route 默认保留。
- 发布版本进入 0.2.x：三个 runtime/init 镜像使用固定 semver，完整 hash lock、固定基础镜像 digest、SBOM、provenance、签名和 HIGH/CRITICAL 扫描。
- Linux/macOS/Windows 安装器都先用旧版本创建并校验备份，再拉取 digest 固定的新镜像；凭据只写宿主机私有文件，不再从 daemon log 读取。

### Fixed

- 修复中央 Model Gateway 配置存在时透明聊天、Knowledge Agent 和 Embedding 仍读取旧 provider 目录而不可用的问题。
- 修复并发 ingest/Core 更新、restore 半提交、10K 召回截断、批量选择导出越界和危险批量删除确认不足。
- 修复移动端路由滚动、当前底栏重复点击、LAN HTTP clipboard、危险对话框默认焦点及首次渠道配置遗留孤儿。
- 修复 Embedding input-only 费用漏计、可变维请求与声明 header 不一致、Retry-After 无限冷却、gzip 原始字节损坏和失败 fallback 账本缺失。

### Security

- Client/provider secret ref 与值域强制隔离，token 使用高熵 ASCII/UTF-8 bytes 常量时间比较；provider/admin/backend key 不再进入日志、Compose 展开、命令参数或长期容器环境。
- 所有带凭据的出站请求禁用 ambient proxy 和重定向；远端仅 HTTPS，loopback/private HTTP 需显式、受限启用；限制 discovery、请求/响应、上传、解析和内部模型输出大小。
- Memory 与 Model 长期容器互相不能读取对方 secret；rootfs 只读、capabilities 全部移除，provider egress 仅授予 Model。
- 入口 relay 增加首字节截止时间（默认 10 秒）与按源 IP 并发上限（默认 32），长期空闲 TCP 连接不能再占满入口槽位。
- `/readyz` 增加 3 秒结果缓存与 single-flight，未鉴权高频探测不再放大 SQLite quick_check 与 Model 控制面调用。

## 0.1.2 - 2026-08-08

### Fixed

- 容器日志里的「接入信息」现在打印宿主机端口，而不是容器内固定的 2026。用户版 compose 通过新的 `MEMORY_PUBLIC_PORT` 把实际映射端口告诉容器。此前只要端口不是 2026（包括一键脚本在 2026 被占用时自动顺延的情况），日志里给出的就是一份连不上的地址——而文档恰恰引导用户回到日志里找密钥和地址。
- 「接入信息」里的 Model Gateway base URL 补上「内部接线地址，不要填进客户端」标注，与 Web Console 横幅的说法保持一致。

## 0.1.1 - 2026-08-08

### Added

- `deploy/install.sh` 一键安装脚本：检查 Docker、下载用户版 Compose、自动避开已占用端口、等待首启就绪并打印两枚一次性密钥；重复运行即升级到最新镜像。
- 用户版 compose 支持 `MEMORY_PORT` 覆盖宿主机端口映射、`MEMORY_HOST=0.0.0.0` 放开局域网/手机访问，并内置 healthcheck 覆盖首启安装期；一键脚本在开启局域网模式时会直接打印手机可用地址。
- 支持在首次安装时自带密钥：`GATEWAY_API_KEY` 和 `MEMORY_CONSOLE_ADMIN_KEY` 两个环境变量对一键脚本、用户版 compose 和 `scripts/setup.sh` 都生效，留空则维持原来的自动生成。自带值需至少 16 个字符、不含空白、字符不过分重复，一键脚本在拉镜像前先校验一次，容器内 `memgw stack install` 再完整校验；密钥只经进程环境传递，不写入安装目录的 `.env`。

### Changed

- `scripts/bootstrap.sh` 的 Python/Node 缺失报错给出具体安装命令，并在构建 Web Console 前校验 Node.js ≥ 22。
- README 与运维、接入文档补充首启等待预期、Windows 安装路径、admin key 找回和端口冲突处理。
- Web Console「模型与路由」页横幅把 Model Gateway 地址明确标注为内部接线（不用填进客户端），并新增当前来源的客户端接入地址；「连接设置」页新增服务进程管理说明（启停命令、后台运行与开机自启）。
- 安装输出和接入文档明确区分两枚密钥的去向：只有 `GATEWAY_API_KEY` 需要填进客户端（含手机），admin key 留在本机浏览器使用。

### Fixed

- `scripts/setup.sh` 结束时会打印完整接入信息块（Web Console、客户端 Base URL、模型名），此前只打印管理台地址和模型名，缺少客户端最需要的 `/v1` Base URL；同时端口改为读取 `project.json` 的实际值，不再硬编码 2026（`--json` 输出的 `client.*` 同步修正）。
- 通过环境变量传入的 `GATEWAY_API_KEY` 现在会写入 `settings.env`。此前只有自动生成的分支才持久化，导致自带密钥在服务重启后丢失。
- `deploy/install.sh` 端口占用时给出的重试命令不再使用 `$0`。通过 `curl | sh` 运行时 `$0` 为 `sh`，原提示会让用户复制到无法执行的 `sh sh`。
- `deploy/install.sh` 区分「重复安装」与「首装但日志解析失败」：在 `up -d` 之前精确匹配本 compose 项目的数据卷，首装解析失败时提示查看日志，重复安装时给出两枚密钥的重新生成命令。

## 0.1.0 - 2026-08-08

首个公开版本。

### Added

- 根 `scripts/setup.sh` 的完整引导安装、`--install-only` 和 `--configure-only` 模式。
- 不含密钥的 AI quickstart JSON recipe、Schema、渠道预设与 stdin 密钥输入。
- 免费 `/models` 自动发现命令和交互式模型选择。
- AI 安装契约、结构化成功/失败输出与社区贡献、安全模板。
- 根级 `Dockerfile` 与 `deploy/`：单容器双服务一体化镜像、首启自动接线入口脚本、开发/用户 compose 文件，以及 tag 推送 GHCR 的 `docker.yml` workflow（含 PR smoke test）。
- 面向 Chatbox / RikkaHub 用户的 `docs/client-setup.md` 接入指南：三项配置、领 key 指引、常见坑速查表。
- `memgw stack install` 现在自动生成 Model Gateway admin key（`memory-console-admin`）并只打印一次，Web Console 的渠道管理不再需要手工创建 admin 身份。
- Web Console「模型与路由」页新增「新建渠道」分步向导：选预设/自定义渠道 → 单向写入渠道 key → discovery 拉取可见模型 → 选定聊天/向量模型并一键接管八条用途路由；全新安装后无需 CLI 即可完成首次模型配置。
- Model Gateway 管理面新增 `POST /admin/connections` 与 `POST /admin/deployments`（admin key、revision 冲突检测、dry_run、整图校验、原子写入热加载），discovery check 现在返回可见模型 ID 列表。

### Changed

- `modelgw quickstart --json` 现在只在 stdout 输出单个 JSON 对象；子流程日志不再污染机器输出。
- 首次安装在交互终端中自动继续完成模型配置和最终 doctor。
- Web Console「模型与路由」页的 admin key 引导改为优先指向安装时打印的密钥。

### Security

- quickstart recipe 拒绝未知字段和密钥字段。
- 模型发现不跟随重定向，不发送推理，也不写配置。
