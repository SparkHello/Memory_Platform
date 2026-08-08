# Contributing to Memory Platform

感谢你帮助改进 Memory Platform。提交代码前请先阅读根 `AGENTS.md`，再阅读目标服务自己的 `AGENTS.md` 和 `README.md`。

## 开始开发

```bash
scripts/bootstrap.sh
scripts/test.sh
```

只准备运行环境而不配置真实模型时使用 `scripts/setup.sh --install-only`。测试必须使用临时目录、fake provider 或 `httpx.MockTransport`，不得调用真实供应商或修改用户的真实数据库与配置目录。

## 提交范围

- 新 provider、渠道、deployment、route 和 pricing 行为放在 `services/model-gateway`。
- 长期记忆、知识库、MCP、OpenAI-compatible 代理和 Web Console 放在 `services/memory-gateway`。
- 跨服务协议变更要同时验证 route 权限、归因 Header、embedding space、错误契约和两边测试。
- 不提交 `.env`、API Key、真实 SQLite、日志、评测快照、备份、虚拟环境、`node_modules` 或前端构建产物。

## Pull Request

PR 请保持主题单一，并说明：

1. 用户可见的问题和结果；
2. 安全或数据边界是否变化；
3. 实际运行的测试；
4. 是否包含迁移、兼容性变化或已知限制。

代码变更至少运行相关定向测试。跨服务、安装、协议或高风险数据变更运行 `scripts/test.sh`；UI 变更至少运行 `npm run build`。

## AI 辅助配置

AI/Agent 应遵守 `docs/ai-install.md`。配置 recipe 必须符合 `docs/ai-quickstart.schema.json` 且不含任何密钥；供应商 API Key 只能经 stdin 传入。
