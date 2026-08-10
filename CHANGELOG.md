# Changelog

本项目遵循语义化版本。发布前的改动记录在 `Unreleased`。

## Unreleased

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
