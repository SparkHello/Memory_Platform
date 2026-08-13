# 客户端接入指南（Chatbox / RikkaHub / FLIT 等）

这份指南写给已经用过 Chatbox、RikkaHub、FLIT 这类 AI 客户端的个人用户。你只要会在客户端里填「Base URL + API Key + 模型名」，就能接入 Memory Platform，不需要懂代码。

接入后，客户端里的每次聊天都会经过 Memory Platform：它自动召回你的长期记忆、注入给模型，并在回答完成后提取新记忆。模型渠道、价格和故障切换由服务端管理，客户端永远只需要填一套配置。

## 还没有 API Key？去哪领

Memory Platform 自己不产生回答，背后需要至少一个模型渠道的 API Key。安装时的 quickstart 内置四个预设渠道，任选一个去其官方开放平台注册、创建 key 即可：

- **DeepSeek 官方**：[platform.deepseek.com](https://platform.deepseek.com) 注册 → API keys 页面创建；
- **Kimi 中国区**：[platform.moonshot.cn](https://platform.moonshot.cn) 注册 → API Key 管理页面创建；
- **小米 MiMo**、**阿里云百炼（DashScope 北京区）**：到各自官方控制台创建。

具体价格、额度和可用模型以各渠道官方页面为准。quickstart 选定预设后会自动列出当前 key 实际可用的模型让你挑，不用手抄模型 ID。

**语义搜索（embedding）是可选的**。不创建或关闭 `memory.embedding` route 即明确使用关键词模式；创建并启用它才表示同意向量化。Memory 的 space 默认留空，会自动采用 route 声明的唯一空间和维度。想零成本体验可以用硅基流动（[siliconflow.cn](https://siliconflow.cn)）的 `BAAI/bge-m3`（免费政策以其官网为准），作为自定义渠道填入。两个最常踩的坑：base URL 必须完整写成 `https://api.siliconflow.cn/v1`（末尾 `/v1` 漏了会 404），模型名必须带 `BAAI/` 前缀（只写 `bge-m3` 会报模型不存在）。

## 推荐路径：当作 OpenAI 兼容网关用

Memory Platform 对外是一个标准的 OpenAI 兼容接口。在客户端里新建一个「OpenAI 兼容 / 自定义 OpenAI」供应商，填这三项：

```text
Base URL: http://127.0.0.1:2026/v1
API Key:  为这台设备创建的 chat token
模型名:   memory-auto
```

要点：

- **每台设备用自己的 chat token**。在 Web Console「接入信息」创建，明文只显示一次；服务端 Auth DB 只保存不可逆 SHA-256。Docker 全新安装交付到 `credentials/gateway.txt` 的是 Console-only 初始 token（旧安装可能为 `gateway.key`）；迁移旧卷时该文件才暂存 legacy key。两者都不应复制给聊天设备。
- **聊天、MCP、Console 权限分开**。聊天客户端只拿 chat token；MCP 客户端只拿 MCP token；Model Gateway admin key 仅在电脑浏览器中临时解锁渠道配置，绝不能填进聊天或 MCP 客户端。
- **丢一台设备只撤销这一枚 token**。在 Console 撤销，或运行 `scripts/memgw token revoke <token-id>`；其他设备不用重配。不要为了好记而自定义低熵密码，也不要经聊天软件或跨设备剪贴板传递 token。
- **Docker 初始密钥不在日志或环境变量里**。安装器只报告宿主机 `credentials/gateway.txt` 与 `credentials/admin.txt` 路径（旧安装兼容 `.key`）；文件应保持仅当前用户可读。不要再用 `GATEWAY_API_KEY=...` 环境变量运行安装器。
- **模型名固定填 `memory-auto`**。它不代表某个具体模型，而是「让服务端按用途路由选择当前配置的模型」。以后换渠道、换模型只改服务端，客户端不用动。
- **关闭 Responses API / 使用 Chat Completions**。如果客户端有「API 类型」选项，选 `Chat Completions`（大多数客户端默认就是）。

### 手机或局域网设备

`127.0.0.1` 和 `localhost` 指的是「客户端自己」。在手机上填这个地址会连到手机本机，连不上电脑上的服务。正确做法：

0. **Docker 部署先放开局域网监听**：默认只监听本机。一键安装用户重新运行安装命令即可：macOS/Linux 在命令前加 `MEMORY_HOST=0.0.0.0`；Windows PowerShell 先运行 `$env:MEMORY_HOST="0.0.0.0"`。安装器会保留数据并直接打印手机地址。手工安装则在 Compose 同目录的 `.env` 加一行 `MEMORY_HOST=0.0.0.0` 后重启。源码安装默认已监听局域网，跳过这一步。
1. 一键安装结束时会打印电脑的局域网地址。手工查询时：macOS 运行 `ipconfig getifaddr en0`，Linux 运行 `hostname -I`，Windows 运行 `ipconfig`；已装 Tailscale 时用 `tailscale ip -4`。
2. Base URL 换成 `http://<电脑IP>:2026/v1`，例如 `http://192.168.1.20:2026/v1`。
3. 模型名不变；在手机上填写专门为它创建的 chat token。MCP 地址同理换成 `http://<电脑IP>:2026/mcp`，但使用独立 MCP token。

只在可信家庭网络或 Tailscale 内这样用，不要把服务无鉴权暴露到公网。

### 常见客户端怎么填

**Chatbox**：设置 → 模型提供方 → 添加自定义提供方（OpenAI API 兼容）→ 填上面的 Base URL、API Key，模型填 `memory-auto`。

**RikkaHub**：设置 → 提供商 → 添加 OpenAI 兼容提供商 → 填 Base URL 和 API Key → 在模型列表中添加 `memory-auto`。模型能力里可以打开「工具」和「推理」开关（服务端会透明转发这些字段）。

**FLIT**：详见更完整的 [Client Integration](../services/memory-gateway/docs/client_integration.md)，包括输入输出模态、工具与推理开关的说明。

### 验证接通了没有

1. 在客户端随便聊一句，比如「我喜欢黑咖啡，以后推荐咖啡时记住这一点」。
2. 打开浏览器访问 `http://127.0.0.1:2026/ui/`（Web Console 使用 Console/迁移期 legacy 凭据，不复用 chat token）。
3. 新开一个对话问「我喜欢什么咖啡」，回答里应该能用到上一轮记住的信息；Web Console 的记忆列表里也能看到新记忆。

如果没记住：确认客户端确实填的是 Memory Platform 的地址而不是直连渠道；确认上一轮的最终回答完整结束（中途断开不会写入记忆）。

### 记忆模式（一般不用管）

默认 `read-write`：自动召回 + 自动提取。需要在某个请求里改变行为时，客户端可以加自定义请求头：

- `X-Memory-Mode: off`：只做透明代理，不读不写记忆；
- `X-Memory-Mode: read`：注入记忆但不提取新记忆；
- `X-Memory-Mode: read-write`：默认行为。

## 可选路径：MCP（知识库和模型自助管理记忆）

网关路径已经覆盖日常聊天的记忆需求。**MCP 是给想要更主动玩法的用户准备的**：让模型自己决定什么时候搜索记忆、整理记忆，以及检索你显式导入的知识库文档（PDF、EPUB、笔记等长文本不会自动进入聊天上下文，只通过知识库工具检索）。

MCP 地址：

```text
http://127.0.0.1:2026/mcp
```

在 Web Console「接入信息」为该客户端创建独立 MCP token。支持远程 MCP 的客户端（如 RikkaHub）在 MCP 服务器配置里选择「Streamable HTTP」，填上面的地址，并在请求头里加 `Authorization: Bearer <你的 MCP token>`。chat token 调 MCP 会得到 403，而不是被提升为管理权限。

为了让模型正确使用这些工具，建议把 [Client Integration](../services/memory-gateway/docs/client_integration.md#mcp-client-rules) 里的推荐系统提示词放进该 MCP 会话的系统提示。两个路径可以同时开启；普通聊天客户端只配网关即可，知识库检索再由支持 MCP 的客户端负责。

## 常见问题

**设备 token 会发给 DeepSeek / Kimi 吗？**
不会。设备 token 只用于访问本机 Memory Gateway；它在进入 Model Gateway 前被替换为独立 backend key。真实供应商 key 只存在 Model Gateway 的隔离 secret volume，永远不会透传给客户端。

**换模型要改客户端吗？**
不用。运行 `services/memory-gateway/.venv/bin/modelgw quickstart` 重新配置服务端即可，客户端始终填 `memory-auto`。

**Web Console 是什么？**
`http://127.0.0.1:2026/ui/` 是本地管理台：查看、编辑、删除记忆，导入知识文档，看用量和费用，备份恢复数据。删除记忆只在这里操作，客户端没有删除工具。

**多人共用一套服务？**
token 会固定绑定创建时的用户，调用方不能改写命名空间；但当前产品仍面向个人/可信家庭网络，而不是公开多租户服务。需要强隔离时建议各跑一套实例。

## 常见坑速查表

| 现象 | 原因和做法 |
| --- | --- |
| 手机上填 `localhost` / `127.0.0.1` 连不上 | 这个地址指手机自己；改成电脑的局域网 IP 或 Tailscale 地址 |
| 设备 token 找不回了 | 明文只显示一次；撤销该 token 并为这台设备新建一枚，其他设备不受影响 |
| 配置模型时要的 admin 密钥找不到了 | Docker 首先检查宿主 `credentials/admin.txt`（旧版为 `admin.key`）；确需重置时用 `modelgw secret set memory-console-admin --stdin`，不要把值放进命令参数或环境变量 |
| 模型名不知道填什么 | 固定 `memory-auto`；以后换渠道换模型只改服务端，客户端不动 |
| 聊了但什么都没记住 | 记忆只在**完整**最终回答后写入；中途断开、被截断、内容被过滤都不会写 |
| 担心渠道 key 被发给客户端 | 不会。供应商 key 只存在 Model Gateway 的隔离 secret volume，客户端只拿固定角色的设备 token |
| 私密内容会不会发给提取/embedding 模型 | 默认不会（`ALLOW_SENSITIVE_EGRESS=false`），本地识别为敏感的内容不出站 |
| 导入的文档没出现在聊天里 | 知识库设计为不自动进入聊天上下文；用 MCP 工具、REST 或 Web Console 显式检索 |
| 想删除某条记忆 | 客户端没有这个工具；打开 `http://127.0.0.1:2026/ui/` 在 Web Console 里操作 |
| 关闭终端后还能用吗 | 能，服务以后台进程运行；但**重启电脑后**源码安装要在仓库目录重新运行 `scripts/memgw stack start`，Docker 用户启动 Docker Desktop 即可（设置里勾选「登录时启动」可免手动） |
