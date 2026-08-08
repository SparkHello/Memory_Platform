# Changelog

本项目遵循语义化版本。发布前的改动记录在 `Unreleased`。

## Unreleased

### Added

- 根 `scripts/setup.sh` 的完整引导安装、`--install-only` 和 `--configure-only` 模式。
- 不含密钥的 AI quickstart JSON recipe、Schema、渠道预设与 stdin 密钥输入。
- 免费 `/models` 自动发现命令和交互式模型选择。
- AI 安装契约、结构化成功/失败输出与社区贡献、安全模板。
- 根级 `Dockerfile` 与 `deploy/`：单容器双服务一体化镜像、首启自动接线入口脚本、开发/用户 compose 文件，以及 tag 推送 GHCR 的 `docker.yml` workflow（含 PR smoke test）。
- `deploy/install.sh` 一键安装脚本：检查 Docker、下载用户版 Compose、自动避开已占用端口、等待首启就绪并打印两枚一次性密钥；重复运行即升级到最新镜像。
- 用户版 compose 支持 `MEMORY_PORT` 覆盖宿主机端口映射、`MEMORY_HOST=0.0.0.0` 放开局域网/手机访问，并内置 healthcheck 覆盖首启安装期；一键脚本在开启局域网模式时会直接打印手机可用地址。
- 面向 Chatbox / RikkaHub 用户的 `docs/client-setup.md` 接入指南：三项配置、领 key 指引、常见坑速查表。
- `memgw stack install` 现在自动生成 Model Gateway admin key（`memory-console-admin`）并只打印一次，Web Console 的渠道管理不再需要手工创建 admin 身份。
- Web Console「模型与路由」页新增「新建渠道」分步向导：选预设/自定义渠道 → 单向写入渠道 key → discovery 拉取可见模型 → 选定聊天/向量模型并一键接管八条用途路由；全新安装后无需 CLI 即可完成首次模型配置。
- Model Gateway 管理面新增 `POST /admin/connections` 与 `POST /admin/deployments`（admin key、revision 冲突检测、dry_run、整图校验、原子写入热加载），discovery check 现在返回可见模型 ID 列表。

### Changed

- `modelgw quickstart --json` 现在只在 stdout 输出单个 JSON 对象；子流程日志不再污染机器输出。
- 首次安装在交互终端中自动继续完成模型配置和最终 doctor。
- Web Console「模型与路由」页的 admin key 引导改为优先指向安装时打印的密钥。
- `scripts/bootstrap.sh` 的 Python/Node 缺失报错给出具体安装命令，并在构建 Web Console 前校验 Node.js ≥ 22。
- README 与运维、接入文档补充首启等待预期、Windows 安装路径、admin key 找回和端口冲突处理。
- Web Console「模型与路由」页横幅把 Model Gateway 地址明确标注为内部接线（不用填进客户端），并新增当前来源的客户端接入地址；「连接设置」页新增服务进程管理说明（启停命令、后台运行与开机自启）。

- 支持在首次安装时自带密钥：`GATEWAY_API_KEY` 和 `MEMORY_CONSOLE_ADMIN_KEY` 两个环境变量对一键脚本、用户版 compose 和 `scripts/setup.sh` 都生效，留空则维持原来的自动生成。自带值需至少 16 个字符、不含空白、字符不过分重复，一键脚本在拉镜像前先校验一次，容器内 `memgw stack install` 再完整校验；密钥只经进程环境传递，不写入安装目录的 `.env`。
- 安装输出和接入文档明确区分两枚密钥的去向：只有 `GATEWAY_API_KEY` 需要填进客户端（含手机），admin key 留在本机浏览器使用。

### Fixed

- `scripts/setup.sh` 结束时会打印完整接入信息块（Web Console、客户端 Base URL、模型名），此前只打印管理台地址和模型名，缺少客户端最需要的 `/v1` Base URL；同时端口改为读取 `project.json` 的实际值，不再硬编码 2026（`--json` 输出的 `client.*` 同步修正）。
- `deploy/install.sh` 端口占用时给出的重试命令不再使用 `$0`。通过 `curl | sh` 运行时 `$0` 为 `sh`，原提示会让用户复制到无法执行的 `sh sh`。
- `deploy/install.sh` 区分「重复安装」与「首装但日志解析失败」：改为在 `up -d` 之前检测数据卷是否已存在，首装解析失败时提示查看日志，重复安装时补充给出两枚密钥的重新生成命令。

### Security

- quickstart recipe 拒绝未知字段和密钥字段。
- 模型发现不跟随重定向，不发送推理，也不写配置。
