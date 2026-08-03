# AGENTS.md

这是 Memory Platform 单仓库。修改任何代码前先读本文件、根 `README.md`，再读目标服务自己的 `AGENTS.md` 和 `README.md`。

## 仓库结构

- `services/memory-gateway`：长期记忆、知识库、MCP、OpenAI-compatible 代理和 Web Console。
- `services/model-gateway`：模型连接、deployment、route、pricing、usage 和管理接口。
- `scripts/bootstrap.sh`：创建统一开发环境并安装两个服务。
- `scripts/memgw`：根目录统一运行栈入口。
- `scripts/test.sh`：两个后端测试集和前端构建。

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
