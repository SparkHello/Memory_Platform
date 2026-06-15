# 需求 AI 对话记录

- 线程: thr_42w8z40u
- 更新时间: 2026-06-15T05:32:29.801Z

---

## 用户

你能看到我的项目文件吗

## 需求 AI

我先看看你的项目结构和当前 draft 文件。

> [工具] ls

> [工具] read

## 需求 AI

看得到，以下是你的项目概览：

**项目: `memory-gateway`** — 一个 Python 项目，位于 `C:/Users/spari/Documents/Memory/memory-gateway`

| 目录/文件 | 说明 |
|-----------|------|
| `app/` | 主应用代码 |
| `config/` | 配置文件 |
| `tests/` | 测试代码 |
| `ui/` | 前端/UI |
| `scripts/` | 脚本 |
| `data/`、`logs/` | 数据和日志 |
| `pyproject.toml` | Python 项目配置 |
| `.env` / `.env.example` | 环境变量 |
| `AGENTS.md` | Agent 说明文档 |
| `README.md` | 项目说明 |

当前 SDD 草稿还是一片空白。让我同时看看 README 和 AGENTS.md 来更深入了解项目背景，这样能帮你更好地完善需求。

> [工具] read

> [工具] read

> [工具] ls

## 需求 AI

都能看到。总结一下你项目的核心情况：

---

### 项目 `memory-gateway` 概览

| 层面 | 内容 |
|------|------|
| **定位** | 本地长期记忆网关，架在 AI 客户端和上游 LLM 之间 |
| **核心能力** | 自动记忆注入/沉淀、MCP 记忆工具、多 provider 路由、本地计费账本、Web 管理台 |
| **后端** | Python 3.12 + FastAPI + SQLite + MCP SDK |
| **前端** | React + TypeScript + Vite（Memory Console） |
| **入口** | `/v1/chat/completions`（网关模式）、`/mcp`（MCP 工具模式） |

**模块划分 (`app/`)**：`api/`、`memory/`、`mcp_server/`、`openai_compat/`、`providers/`、`llm/`

---

### 当前 SDD 草稿状态

草稿是空白的 — 标题「未命名需求」，背景、目标、验收标准全部为空。

---

要帮你把这份需求补起来，我需要你提供方向。比如：

- **你想做什么功能/改动？** 是新功能（比如记忆导出、批量操作、权限控制），还是改进现有功能（比如记忆提取算法优化、前端交互优化），还是修 bug/重构？
- **有没有具体的痛点或场景？** 比如「当前记忆检索太慢」、「前端管理台缺少批量删除」、「想让 MCP 工具支持 xxx」？
- **给谁用的？** 主要影响网关用户、MCP 用户、管理台用户，还是你自己运维？

跟我说说你想做的方向，我就能把这份 draft 帮你填成一份结构清晰的 SDD。
