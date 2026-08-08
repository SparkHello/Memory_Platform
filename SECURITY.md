# Security Policy

## Supported scope

当前 `0.1.x` 是面向个人、本机或可信家庭网络的预览版本。默认部署不是未经加固的公网多租户 SaaS；Model Gateway 管理接口应保持回环访问，跨主机时必须置于 HTTPS 后。

## Reporting a vulnerability

请不要在公开 Issue 中提交未修复漏洞、密钥、私人记忆、知识正文、数据库、日志或可利用细节。优先使用 GitHub 仓库的私密 Security Advisory 报告功能；若该功能不可用，请通过维护者公开资料中的私密联系方式先发送不含真实用户数据的最小摘要。

报告建议包含：受影响版本、入口、影响、最小复现、是否需要本地权限，以及已经采取的临时缓解措施。请使用测试密钥、临时目录和脱敏数据。

## Secret exposure

如果密钥曾进入 Git、日志、命令参数、Issue、聊天记录或构建产物，应立即在对应服务商处轮换；仅从文件中删除不能撤销已经泄露的凭证。Memory Platform 不会把密钥加入便携备份，但备份仍包含完整记忆和知识正文，应按敏感文件保管。

安全边界详情见根 `README.md`、两个服务的 `AGENTS.md` 和 `docs/ai-install.md`。
