# 让 AI 帮你安装 Memory Platform

这份文档写给帮用户安装的 AI 助手 / Agent，也适合想要全命令行、零交互安装的人。全程不需要任何 TTY 交互式提问：每一步都是可直接执行的命令，密钥通过标准输入传入，不进入命令行历史。

> 面向个人或可信本机/家庭网络。不要把服务无鉴权暴露到公网。

## 前提

- macOS 或 Linux。
- 已安装 Python ≥ 3.12（`python3.12`、`python3.13` 或满足版本的 `python3` 均可）。
- 已安装 Node.js 22 与 npm（仅构建 Web Console 需要；可用 `--skip-ui` 跳过）。
- 用户已经准备好至少一个模型渠道的 API Key、官方 OpenAI 兼容 base URL，以及该渠道页面显示的精确聊天模型 ID。

在开始前，向用户确认这三项信息：**渠道简称、base URL、聊天模型 ID**。API Key 让用户在执行到对应步骤时提供，不要写进任何文件或命令参数。

## 三步安装

以下命令都在仓库根目录执行。

### 1. 准备环境 + 安装并启动双服务栈

```bash
scripts/setup.sh
```

它会依次完成：探测合适的 Python 创建统一虚拟环境、以 editable 模式安装两个服务、构建 Web Console，然后运行 `memgw stack install --start`。

`stack install` 会自动生成客户端访问密钥（`GATEWAY_API_KEY`）并在输出中**打印一次**。请把这一行完整转达给用户，并说明它不会再次显示，也不会写入项目 `.env`。这就是客户端（如 FLIT）访问 `/v1`、MCP、REST 和 Web Console 时使用的密钥。

只装后端、跳过前端：

```bash
scripts/setup.sh --skip-ui
```

### 2. 非交互配置模型渠道

模型渠道、模型和用途由 Model Gateway 负责。用它的 `quickstart --non-interactive` 一步配好，API Key 从标准输入读取一行：

```bash
printf '%s\n' "$USER_PROVIDED_API_KEY" | \
  services/memory-gateway/.venv/bin/modelgw quickstart --non-interactive \
    --channel deepseek \
    --base-url https://api.deepseek.com/v1 \
    --chat-model deepseek-chat \
    --json
```

这一步会：

- 添加该渠道（connection）并安全保存 API Key；
- 添加这个聊天模型（deployment）；
- 把全部 7 项文字用途（`memory.chat`、`memory.extract`、`memory.compact`、`memory.core`、`memory.review`、`knowledge.fast`、`knowledge.pro`）都指向这一个模型；
- 确保存在 `memory-gateway` backend client，并把 backend key 同步给记忆服务；
- 默认自动连接记忆服务并重启，使配置立即生效。

不要把 API Key 作为 `--api-key` 之类的命令行参数传入——本命令只从 stdin 读取，避免泄露到进程列表和 shell 历史。

`--json` 让输出可被程序解析。检查返回中的 `warnings` 数组：为空表示连接记忆服务成功；非空需按提示处理（最常见是没有自动找到 memgw，用 `--memgw /path/to/scripts/memgw` 显式指定）。

#### 可选：同时配置语义搜索（向量）模型

```bash
printf '%s\n' "$USER_PROVIDED_API_KEY" | \
  services/memory-gateway/.venv/bin/modelgw quickstart --non-interactive \
    --channel deepseek \
    --base-url https://api.deepseek.com/v1 \
    --chat-model deepseek-chat \
    --embedding-model text-embedding-3-small \
    --embedding-dimensions 1536 \
    --embedding-space my-space-v1 \
    --json
```

没有向量模型时可跳过；记忆检索会安全回退到关键词/FTS。向量空间名称一旦选定，换模型或换维度时必须改用新名称，避免不同向量空间被错误比较。

#### 常用可选参数

| 参数 | 用途 |
| --- | --- |
| `--adapter {generic,kimi,deepseek,mimo}` | 接口兼容方式，默认 `generic` |
| `--plan {payg,subscription,free_tier,...}` | 套餐类型，默认 `payg` |
| `--chat-capability tools`（可重复） | 声明模型能力：`tools`、`reasoning`、`multimodal_input`、`json_object`、`json_schema` 等 |
| `--chat-author` | 模型作者简称，默认用渠道名 |
| `--no-connect-memory` | 只配置模型服务，不连接记忆服务 |
| `--no-start` | 配置完成后不自动启动模型服务 |
| `--memgw <path>` | 显式指定记忆服务 `scripts/memgw` 路径 |

### 3. 校验

```bash
scripts/memgw stack status
scripts/memgw stack doctor
```

`doctor` 全部通过即表示两个服务、路由、密钥和接线正常。

## 完成后给用户的信息

- **Web Console**：`http://127.0.0.1:2026/ui/`
- **OpenAI 兼容 base URL**：`http://127.0.0.1:2026/v1`
- **MCP**：`http://127.0.0.1:2026/mcp`
- **客户端 API Key**：第 1 步 `stack install` 打印的 `GATEWAY_API_KEY`
- **模型名**：客户端填 `memory-auto`

## 排错

| 现象 | 处理 |
| --- | --- |
| `setup.sh` 报找不到 Python ≥ 3.12 | 安装 Python 3.12+，或 `PYTHON_BIN=/path/to/python scripts/bootstrap.sh` |
| quickstart 的 `warnings` 提示没找到 memgw | 加 `--memgw <仓库>/scripts/memgw` 重跑 |
| `stack doctor` 报 chat 路由无可用模型 | 确认第 2 步渠道 API Key、base URL、模型 ID 正确，用 `modelgw check --live` 核实 |
| 忘记 `GATEWAY_API_KEY` | 运行 `scripts/memgw secret set gateway` 重新设置一枚 |

更完整的说明见根 [README](../README.md) 与两个服务各自的 README。
