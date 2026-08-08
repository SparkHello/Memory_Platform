# Changelog

本项目遵循语义化版本。发布前的改动记录在 `Unreleased`。

## Unreleased

### Added

- 根 `scripts/setup.sh` 的完整引导安装、`--install-only` 和 `--configure-only` 模式。
- 不含密钥的 AI quickstart JSON recipe、Schema、渠道预设与 stdin 密钥输入。
- 免费 `/models` 自动发现命令和交互式模型选择。
- AI 安装契约、结构化成功/失败输出与社区贡献、安全模板。
- 根级 `Dockerfile` 与 `deploy/`：单容器双服务一体化镜像、首启自动接线入口脚本、开发/用户 compose 文件，以及 tag 推送 GHCR 的 `docker.yml` workflow（含 PR smoke test）。
- 面向 Chatbox / RikkaHub 用户的 `docs/client-setup.md` 接入指南：三项配置、领 key 指引、常见坑速查表。
- `memgw stack install` 现在自动生成 Model Gateway admin key（`memory-console-admin`）并只打印一次，Web Console 的渠道管理不再需要手工创建 admin 身份。

### Changed

- `modelgw quickstart --json` 现在只在 stdout 输出单个 JSON 对象；子流程日志不再污染机器输出。
- 首次安装在交互终端中自动继续完成模型配置和最终 doctor。
- Web Console「模型与路由」页的 admin key 引导改为优先指向安装时打印的密钥。

### Security

- quickstart recipe 拒绝未知字段和密钥字段。
- 模型发现不跟随重定向，不发送推理，也不写配置。
