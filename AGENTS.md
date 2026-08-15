# AGENTS.md

这是 Memory Platform 单仓库。修改任何代码前先读本文件、根 `README.md`，再读目标服务自己的 `AGENTS.md` 和 `README.md`。

## 仓库结构

- `services/memory-gateway`：长期记忆、知识库、MCP、OpenAI-compatible 代理和 Web Console。
- `services/model-gateway`：模型连接、deployment、route、pricing、usage 和管理接口。
- `packages/model-gateway-contracts`：只含纯配置/schema、稳定 route、归因 Header 和错误枚举；不得依赖 HTTP、CLI、settings 或任一服务启动代码。
- `scripts/bootstrap.sh`：创建统一开发环境并安装两个服务。
- `scripts/memgw`：根目录统一运行栈入口。
- `scripts/test.sh`：两个后端测试集和前端构建。
- `Dockerfile` 与 `deploy/`：Memory、Model、离线 init/maintenance 三套隔离运行镜像、首启接线入口脚本、compose 文件和 `deploy/install.sh` Docker 一键安装脚本；长期镜像使用各自独立 Python 环境，只共享窄协议包。`.github/workflows/docker.yml` 在 tag 时推送 GHCR。旧单卷（legacy）布局的一次性迁移已从安装器拆出为独立工具 `deploy/legacy_cutover.py`（容器内仍复用 `backup_legacy.py` / `migrate_legacy.py`），安装器检测到 legacy 布局时只报错并指向该工具。`install.sh` 与 `install.ps1` 是同一安装器的双实现：改动必须两边同步，默认版本号等共享常量由 `services/memory-gateway/tests/test_installer_parity.py` 钉住。

## 开发边界

- 两个服务共享 Git 历史和提交，但运行配置、进程与安全职责继续分离。
- 新 provider、渠道、套餐、模型和价格在 `model-gateway` 中实现；不要把供应商特例重新写回 `memory-gateway`。
- Memory Gateway 只通过稳定 route 和独立 backend client key 调用 Model Gateway。
- 不得提交 `.env`、密钥、真实 SQLite 数据库、日志、评测快照、虚拟环境、`node_modules` 或前端构建产物。
- 不得用测试修改真实 `data/memory.db`、`data/knowledge.db` 或用户配置目录；测试必须使用 fake provider、MockTransport 和临时目录。
- 修改跨服务协议时，必须同时检查 Model Gateway 归因 Header、route 权限、embedding space、错误契约和两边测试。
- 保留用户已有改动；修改前先在仓库根目录运行 `git status --short`。

## 常用命令

```bash
scripts/bootstrap.sh
scripts/test.sh
scripts/memgw stack status
```

## 帮用户配置

- 用户要求 AI/Agent 完成首次安装或模型配置时，优先读取 `docs/ai-install.md`，使用根 `scripts/setup.sh --config <json> --json` 完成整套流程，不要让用户分别操作两个服务。
- `stack install`（含 `scripts/setup.sh` 与容器首启）会生成首次 Console token 和 Model Gateway admin key（`memory-console-admin`），只写入宿主私有凭据目录中的 `0600` 文件；终端仅报告路径，密钥不得进入日志或环境变量。丢失 admin key 后用标准输入安全重设。
- AI 生成的配置必须符合 `docs/ai-quickstart.schema.json`，且不得包含 API Key、backend key、admin key 或其他 secret；供应商 API Key 只允许通过标准输入传给安装命令。
- 渠道 `base_url`、精确模型 ID、能力和 embedding 维度必须来自用户指定渠道的官方资料；不确定时先让用户确认，不猜测。
- 需要确认当前 key 可用模型时，优先用 `modelgw discover --preset <id> --non-interactive --json` 或显式 `--base-url`；它只允许读取 `/models`，不得自动发起 `--live` 推理。
- 只需要准备环境、不配置模型时显式使用 `scripts/setup.sh --install-only`。已有高级配置继续使用 `modelgw` 的独立子命令，不要用 quickstart 覆盖用户精细路由。

定向测试：

```bash
cd services/memory-gateway
.venv/bin/python -m pytest tests/test_chat_gateway.py

cd ../model-gateway
../memory-gateway/.venv/bin/python -m pytest tests/test_service.py
```

UI 变更至少运行：

```bash
cd services/memory-gateway/ui
npm run build
```

更细的安全边界、测试选择和不可修改文件，以各服务 `AGENTS.md` 为准。
