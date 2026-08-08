# 客户端接入指南（Chatbox / RikkaHub / FLIT 等）

这份指南写给已经用过 Chatbox、RikkaHub、FLIT 这类 AI 客户端的个人用户。你只要会在客户端里填「Base URL + API Key + 模型名」，就能接入 Memory Platform，不需要懂代码。

接入后，客户端里的每次聊天都会经过 Memory Platform：它自动召回你的长期记忆、注入给模型，并在回答完成后提取新记忆。模型渠道、价格和故障切换由服务端管理，客户端永远只需要填一套配置。

## 还没有 API Key？去哪领

Memory Platform 自己不产生回答，背后需要至少一个模型渠道的 API Key。安装时的 quickstart 内置四个预设渠道，任选一个去其官方开放平台注册、创建 key 即可：

- **DeepSeek 官方**：[platform.deepseek.com](https://platform.deepseek.com) 注册 → API keys 页面创建；
- **Kimi 中国区**：[platform.moonshot.cn](https://platform.moonshot.cn) 注册 → API Key 管理页面创建；
- **小米 MiMo**、**阿里云百炼（DashScope 北京区）**：到各自官方控制台创建。

具体价格、额度和可用模型以各渠道官方页面为准。quickstart 选定预设后会自动列出当前 key 实际可用的模型让你挑，不用手抄模型 ID。

**语义搜索（embedding）是可选的**。不配也能用，检索会自动回退关键词模式；配了效果更好。想零成本体验可以用硅基流动（[siliconflow.cn](https://siliconflow.cn)）的 `BAAI/bge-m3`（免费政策以其官网为准），作为自定义渠道填入。两个最常踩的坑：base URL 必须完整写成 `https://api.siliconflow.cn/v1`（末尾 `/v1` 漏了会 404），模型名必须带 `BAAI/` 前缀（只写 `bge-m3` 会报模型不存在）。

## 推荐路径：当作 OpenAI 兼容网关用

Memory Platform 对外是一个标准的 OpenAI 兼容接口。在客户端里新建一个「OpenAI 兼容 / 自定义 OpenAI」供应商，填这三项：

```text
Base URL: http://127.0.0.1:2026/v1
API Key:  安装时打印一次的 GATEWAY_API_KEY
模型名:   memory-auto
```

要点：

- **API Key 是安装时打印的那一个**。运行 `scripts/setup.sh` 完成安装时，终端会打印一次 `GATEWAY_API_KEY`，之后不再显示。用 Docker 部署时，它在首次启动的容器日志里：`docker compose -f docker-compose.user.yml logs memory-platform`。它和你填给模型渠道（DeepSeek、Kimi 等）的 key 完全是两回事：客户端只认 Memory Platform 的 key，真实供应商 key 只保存在服务端。
- **找不回 key 就换一个新的**：在仓库目录运行 `scripts/memgw secret set gateway`（Docker 部署用 `docker compose -f docker-compose.user.yml exec memory-platform memgw secret set gateway`），按提示生成新 key，然后更新所有客户端。旧 key 会立即失效。
- **模型名固定填 `memory-auto`**。它不代表某个具体模型，而是「让服务端按用途路由选择当前配置的模型」。以后换渠道、换模型只改服务端，客户端不用动。
- **关闭 Responses API / 使用 Chat Completions**。如果客户端有「API 类型」选项，选 `Chat Completions`（大多数客户端默认就是）。

### 手机或局域网设备

`127.0.0.1` 和 `localhost` 指的是「客户端自己」。在手机上填这个地址会连到手机本机，连不上电脑上的服务。正确做法：

0. **Docker 部署先放开局域网监听**：默认只监听本机。在 `docker-compose.user.yml` 同目录的 `.env` 加一行 `MEMORY_HOST=0.0.0.0`，然后 `docker compose -f docker-compose.user.yml up -d` 重启（用一键脚本安装的，也可以 `MEMORY_HOST=0.0.0.0` 重跑脚本）。源码安装默认已监听局域网，跳过这一步。
1. 查电脑的局域网 IP：macOS 运行 `ipconfig getifaddr en0`，Linux 运行 `hostname -I`；已装 Tailscale 时用 `tailscale ip -4`。
2. Base URL 换成 `http://<电脑IP>:2026/v1`，例如 `http://192.168.1.20:2026/v1`。
3. API Key、模型名不变。MCP 地址同理换成 `http://<电脑IP>:2026/mcp`。

只在可信家庭网络或 Tailscale 内这样用，不要把服务无鉴权暴露到公网。

### 常见客户端怎么填

**Chatbox**：设置 → 模型提供方 → 添加自定义提供方（OpenAI API 兼容）→ 填上面的 Base URL、API Key，模型填 `memory-auto`。

**RikkaHub**：设置 → 提供商 → 添加 OpenAI 兼容提供商 → 填 Base URL 和 API Key → 在模型列表中添加 `memory-auto`。模型能力里可以打开「工具」和「推理」开关（服务端会透明转发这些字段）。

**FLIT**：详见更完整的 [Client Integration](../services/memory-gateway/docs/client_integration.md)，包括输入输出模态、工具与推理开关的说明。

### 验证接通了没有

1. 在客户端随便聊一句，比如「我喜欢黑咖啡，以后推荐咖啡时记住这一点」。
2. 打开浏览器访问 `http://127.0.0.1:2026/ui/`（Web Console，用同一个 API Key 登录）。
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

鉴权用同一个 `GATEWAY_API_KEY`（作为 Bearer token）。支持远程 MCP 的客户端（如 RikkaHub）在 MCP 服务器配置里选择「Streamable HTTP」，填上面的地址，并在请求头里加 `Authorization: Bearer <你的GATEWAY_API_KEY>`。

为了让模型正确使用这些工具，建议把 [Client Integration](../services/memory-gateway/docs/client_integration.md#mcp-client-rules) 里的推荐系统提示词放进该 MCP 会话的系统提示。两个路径可以同时开启；普通聊天客户端只配网关即可，知识库检索再由支持 MCP 的客户端负责。

## 常见问题

**这套 key 会发给 DeepSeek / Kimi 吗？**
不会。`GATEWAY_API_KEY` 只用于客户端访问 Memory Platform 本机服务；真实供应商 key 保存在服务端仓库外的密钥文件里，永远不会透传给客户端。

**换模型要改客户端吗？**
不用。运行 `services/memory-gateway/.venv/bin/modelgw quickstart` 重新配置服务端即可，客户端始终填 `memory-auto`。

**Web Console 是什么？**
`http://127.0.0.1:2026/ui/` 是本地管理台：查看、编辑、删除记忆，导入知识文档，看用量和费用，备份恢复数据。删除记忆只在这里操作，客户端没有删除工具。

**多人共用一套服务？**
默认所有请求属于同一个 `default` 用户。给家人用建议各跑一套实例，而不是共享 key。

## 常见坑速查表

| 现象 | 原因和做法 |
| --- | --- |
| 手机上填 `localhost` / `127.0.0.1` 连不上 | 这个地址指手机自己；改成电脑的局域网 IP 或 Tailscale 地址 |
| API Key 找不回了 | 它只在安装时打印一次；运行 `scripts/memgw secret set gateway` 换新，旧 key 立即失效，所有客户端要更新 |
| 配置模型时要的 admin 密钥找不到了 | admin key 同样只打印一次；运行 `services/memory-gateway/.venv/bin/modelgw secret set memory-console-admin` 重设（Docker 部署把命令前缀换成 `docker compose -f docker-compose.user.yml exec memory-platform modelgw`） |
| 模型名不知道填什么 | 固定 `memory-auto`；以后换渠道换模型只改服务端，客户端不动 |
| 聊了但什么都没记住 | 记忆只在**完整**最终回答后写入；中途断开、被截断、内容被过滤都不会写 |
| 担心渠道 key 被发给客户端 | 不会。供应商 key 只存在服务端仓库外的密钥文件，客户端永远只拿 `GATEWAY_API_KEY` |
| 私密内容会不会发给提取/embedding 模型 | 默认不会（`ALLOW_SENSITIVE_EGRESS=false`），本地识别为敏感的内容不出站 |
| 导入的文档没出现在聊天里 | 知识库设计为不自动进入聊天上下文；用 MCP 工具、REST 或 Web Console 显式检索 |
| 想删除某条记忆 | 客户端没有这个工具；打开 `http://127.0.0.1:2026/ui/` 在 Web Console 里操作 |
| 关闭终端后还能用吗 | 能，服务以后台进程运行；但**重启电脑后**源码安装要在仓库目录重新运行 `scripts/memgw stack start`，Docker 用户启动 Docker Desktop 即可（设置里勾选「登录时启动」可免手动） |
