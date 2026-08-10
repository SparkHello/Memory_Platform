# Memory Platform 发布宣传文案

> 用途：Show HN（英文）与 V2EX 分享创造（中文）。
> 状态：草稿，未发布。发布前建议先补一段 30 秒演示 GIF（见文末清单）。
> 本文件不打算提交进仓库历史，发布后可删除或移出。

---

## 1. Hacker News — Show HN

**Title:**

```
Show HN: Memory Platform – local-first automatic memory for OpenAI-compatible chat clients
```

**Body:**

```
Hi HN,

I built Memory Platform, a local gateway that sits between your existing chat
client (Chatbox, RikkaHub, FLIT, anything OpenAI-compatible) and your model
provider, and gives the AI long-term memory across conversations — without MCP
config and without "please remember this" prompts.

How it works: you point your client's base URL at the gateway's /v1 endpoint.
For normal chat it automatically recalls relevant memories, injects them into
context, and after a complete answer extracts what's worth keeping long-term.
Extraction is deliberately conservative: it waits for the full answer, checks
the original wording, subjects, and negations, and never writes memory from
truncated, filtered, or tool-interrupted responses. MCP (/mcp) is an optional
entry point for explicit search/curation, not a prerequisite.

Why I built it this way instead of using Mem0/Zep/Letta:

- Those are a hosted/SDK memory layer, a temporal knowledge graph, and an
  agent runtime respectively. I wanted to keep my existing chat client and get
  memory, local storage, auditability, and provider-independent model routing
  in one local service.
- Governance before "remembering more": every memory keeps its source and
  state, can explain why it was recalled, and can be edited, archived,
  restored, or permanently deleted from a local web console.
- Memory and knowledge are physically separate: personal facts go to
  memory.db; imported long documents (Markdown/PDF/DOCX/EPUB) go to a separate
  knowledge.db with hybrid full-text/vector search, and never leak into chat
  context unless you explicitly retrieve them.
- The client always calls the model name "memory-auto". A second gateway
  behind it picks channel/model/fallback per purpose, so switching providers
  is a server-side config change — clients and memory data don't migrate.

Honest current boundaries: it targets a personal machine or trusted home
network, not a hardened multi-tenant SaaS; SQLite/single-process design, not
million-scale ANN; lightweight topic/entity associations, not a full bitemporal
knowledge graph; the OpenAI-compatible surface focuses on Chat Completions.

Install is two commands if you have Docker (image on GHCR, amd64+arm64); the
first-run wizard handles model channels and routing from the browser. Source
install needs Python 3.12+ and Node 22.

Repo: https://github.com/SparkHello/Memory_Platform
License: Apache-2.0

Happy to answer questions — especially on the extraction pipeline and the
recall-explanation design.
```

**发帖提示：**
- 发布时间：美东工作日早上（北京时间晚上 9–11 点）效果最好。
- HN 标题不要带 emoji，不要写 "revolutionary" 之类的词；上面标题已符合。
- 发完后作者要在评论区保持活跃，前 2 小时每条评论都回。

---

## 2. V2EX — 分享创造

**标题：**

```
[开源] Memory Platform：给现有 AI 聊天客户端加一层"自动记忆"的本地网关
```

**正文：**

```markdown
## 一句话介绍

不用换聊天客户端、不用配 MCP、不用提示"请记住这件事"——把客户端的 Base URL 指向这个本地网关，普通聊天就会自动召回相关记忆、注入上下文，并在完整回答后提取值得长期保存的内容。

## 解决什么问题

我在用 Chatbox 这类客户端时一直有个痛点：AI 完全不记得上次聊过什么。试过几个方案都不太满意：

- Mem0 更偏 SDK / 托管平台，适合自研应用接入，不太适合"我就想继续用我的聊天客户端"；
- Zep / Graphiti 是完整的时态知识图谱，对个人使用来说太重；
- Letta 是一整套 agent runtime，等于换了个聊天入口。

所以我做了 Memory Platform：一个跑在本机的网关，夹在客户端和模型渠道之间。

## 工作方式

- 客户端只接 OpenAI 兼容的 `/v1`，网关自动完成记忆召回、注入和回答后提取，不依赖模型记不记得调工具；
- 提取是保守的：等完整回答结束，核对原话、主语、否定关系和敏感性；截断、内容过滤、工具调用中断的回答不会写入记忆；
- 每条记忆都能解释"为什么被召回"，可以在本地 Web Console 里编辑、归档、恢复、彻底删除；
- 记忆和知识库物理分开：个人事实进 `memory.db`，导入的长文档（Markdown / PDF / DOCX / EPUB）进独立的 `knowledge.db`，不会自动混进聊天上下文；
- 客户端永远填模型名 `memory-auto`，后面由 Model Gateway 按用途选渠道和备用顺序——换供应商只改服务端配置。

## 数据与边界

记忆、知识文档和配置全部保存在自己设备上（SQLite），可查看、可备份。说几个诚实的边界：默认面向个人电脑或可信家庭网络，不是公网多租户 SaaS；单进程 SQLite 设计，不追求百万级记忆的 ANN 检索；目前是轻量主题/实体关联，不是完整时态知识图谱。

## 快速体验

有 Docker 就行，两条命令：

```bash
VERSION=v0.2.0
curl -O "https://raw.githubusercontent.com/SparkHello/Memory_Platform/$VERSION/deploy/docker-compose.user.yml"
docker compose -f docker-compose.user.yml up -d
```

离线初始化会把初始 Console token 和 Model admin key 分别写入宿主机私有的 `credentials/gateway.key`、`credentials/admin.key`，不会写进 Docker 日志或容器环境。随后打开 `http://127.0.0.1:2026/ui/` 完成模型配置（渠道向导 + 自动发现可用模型，不用碰 CLI），并在「接入信息」为每台聊天设备创建独立 chat token；客户端只需填写 Base URL、该设备的 token 和 `memory-auto`。

## 链接

- GitHub：https://github.com/SparkHello/Memory_Platform
- 协议：Apache-2.0，欢迎 Star / Issue / PR

有任何问题（提取管线、召回解释、模型路由设计）都欢迎交流，我会认真回复。
```

**发帖提示：**
- 节点选择：`分享创造`。
- V2EX 正文里的 bash 代码块注意用 V2EX 支持的 Markdown；如果渲染异常，改成缩进代码块。
- 标题不要写"最强""颠覆"这类词，V2EX 反感营销腔。

---

## 3. 发布前检查清单

- [ ] 录制 30 秒演示 GIF：发一条含个人偏好的消息 → 打开 Web Console → 记忆出现 → 新对话中被自动召回。放在 README 顶部和帖子正文里。
- [ ] 确认 README 的 Release / CI badge 显示正常（Release v0.2.0 已发布，CI 需在 main 上有通过记录）。
- [ ] 设置 GitHub 仓库 Social preview 图（Settings → General → Social preview，可用 `docs/images/memory-platform-hero.jpg`）。
- [ ] Show HN 发出后 24 小时内，再发 V2EX 中文帖（两边受众重叠少，不用怕分流）。
- [ ] 第二波（发帖后 1 周）：提交 `awesome-selfhosted`、`awesome-mcp-servers` 列表；r/selfhosted、r/LocalLLaMA 用 Show HN 英文稿精简版。
