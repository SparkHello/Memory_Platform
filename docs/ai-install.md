# 让 AI 帮你安装 Memory Platform

这份文档写给帮用户安装的 AI 助手 / Agent，也适合想要全命令行、零交互安装的人。AI 只需准备一份**不含密钥**的 JSON 配置，然后调用根目录的一条命令；环境、双服务接线、启动和最终检查都由脚本完成。

> 面向个人或可信本机/家庭网络。不要把服务无鉴权暴露到公网。

## 前提

- macOS 或 Linux。
- 已安装 Python ≥ 3.12（`python3.12`、`python3.13` 或满足版本的 `python3` 均可）。
- 已安装 Node.js 22 与 npm（仅构建 Web Console 需要；可用 `--skip-ui` 跳过）。
- 用户已经准备好至少一个模型渠道的 API Key、官方 OpenAI 兼容 base URL，以及该渠道页面显示的精确聊天模型 ID。

在开始前，向用户确认：**渠道简称、base URL、聊天模型 ID**。base URL、模型 ID、能力和可选 embedding 维度应以该渠道的官方资料为准。API Key 只允许在执行命令时通过标准输入提供，不要写进配置文件或命令参数。

## 推荐：一条命令完成

### 1. AI 生成可审阅的配置

配置契约是 [`ai-quickstart.schema.json`](ai-quickstart.schema.json)，示例位于 [`examples/quickstart.example.json`](../examples/quickstart.example.json)。配置中没有、也不允许出现 API Key：

```json
{
  "schema_version": 1,
  "preset": "deepseek",
  "chat_model": "exact-model-id-from-discover",
  "plan": "payg",
  "chat_capabilities": ["tools", "reasoning"],
  "reasoning_default": "inherit",
  "embedding": null
}
```

内置预设为 `deepseek`、`kimi-cn`、`mimo` 和 `dashscope-cn`。其他渠道不用 `preset`，改为填写 `channel`、官方 `base_url` 和必要时的 `adapter`。把确认后的内容写到仓库外临时路径，例如 `/tmp/memory-platform-quickstart.json`。不要写 `api_key`、`secret`、backend key 或 admin key；解析器会拒绝所有未知字段。

如果运行环境已经安装，而 AI 还不知道该 key 实际可用的精确模型 ID，可先执行一次只读发现。全新环境可以先运行 `scripts/setup.sh --install-only`，再执行：

```bash
printf '%s\n' "$USER_PROVIDED_API_KEY" | \
  services/memory-gateway/.venv/bin/modelgw discover \
    --preset deepseek \
    --non-interactive \
    --json
```

该命令只请求 `/models`，不发送推理、不修改配置、也不跟随重定向。AI 应让用户从返回列表确认聊天模型；不能只凭名称猜测工具、推理、多模态或 JSON 能力。

### 2. 安全输入 API Key 并执行

下面是一条完整命令。示例变量只存在当前 shell；不要把真实 key 直接写进命令文本：

```bash
printf '%s\n' "$USER_PROVIDED_API_KEY" | \
  scripts/setup.sh \
    --config /tmp/memory-platform-quickstart.json \
    --json
```

脚本会自动完成：

- 探测 Python、创建统一虚拟环境并安装两个服务；
- 构建 Web Console；
- 初始化、接线并启动双服务栈；
- 安全保存供应商 API Key；
- 用一个聊天模型承担全部 7 项文字用途；
- 可选配置 embedding route，并同步空间与维度；
- 连接 Memory Gateway，重启生效；
- 运行完整 `stack doctor`。

`--json` 模式只在 stdout 返回一个最终 JSON 对象，并以 `setup_verified=true` 表示 doctor 已通过。安装进度以及首次凭据的私有文件路径写到 stderr，避免污染机器可解析输出。密钥值不得进入命令行、环境变量、日志或 AI 回复；新设备应通过 `memgw token create` 按用途创建 scoped token。

任一步失败时 stdout 仍返回一个对象，例如：

```json
{
  "setup_verified": false,
  "error": {"step": "doctor", "exit_code": 1}
}
```

具体诊断保留在 stderr，不进入配置文件。常见 `step` 为 `arguments`、`bootstrap`、`stack_install`、`quickstart`、`doctor` 或 `finalize`。

只装后端、跳过 Web Console 构建时加 `--skip-ui`。只准备运行环境、暂不配置模型时使用 `scripts/setup.sh --install-only`。

### 可选：同时配置语义搜索

把 recipe 中的 `embedding` 从 `null` 改成：

```json
"embedding": {
  "model": "exact-embedding-model-id",
  "dimensions": 1024
}
```

没有向量模型时保留 `null`；记忆和知识检索会安全回退到关键词/FTS。默认会根据渠道 origin、精确模型 ID 与维度自动派生不可混用的向量空间。只有明确验证过兼容性的专家场景才填写可选 `space` 覆盖值。

## 已有环境：只重新配置模型

如果环境已经安装，不想重新跑依赖安装，可直接使用同一个 recipe：

```bash
printf '%s\n' "$USER_PROVIDED_API_KEY" | \
  scripts/setup.sh \
    --configure-only \
    --config /tmp/memory-platform-quickstart.json \
    --json
```

如果当前已经存在 `memory.*` / `knowledge.*` 文字 route，quickstart 默认拒绝覆盖。只有用户明确确认要把这些 route 全部改为本次单模型配置时，才在 recipe 加入 `"replace_existing_routes": true`；已有多渠道 fallback 或精细用途拆分时不要使用它，改用 `modelgw route`。

高级用户仍可使用原来的 `quickstart --non-interactive --channel ...` 参数方式，或完整的 `connection`、`deployment`、`route` 子命令。recipe 是可选入口，不会取代精细配置。

## AI 的操作边界

- 可以根据用户指定渠道的官方资料填写 base URL、精确模型 ID、adapter 和能力。
- 无法确认精确模型 ID、embedding 维度或套餐是否允许后台调用时，必须要求用户确认，不能猜。
- 不把任何 API Key 写进 recipe、shell 参数、日志、聊天转述或 Git；只经 stdin 传入。
- 不替用户自动开启 `ALLOW_SENSITIVE_EGRESS`，也不把服务暴露到公网。
- 已有自定义 fallback、价格和多渠道配置时，不应重新运行 quickstart 覆盖它们；改用精细 CLI。
- 最终检查 `warnings` 为空、`memgw_wired=true`、`started=true`、`setup_verified=true`，否则按错误信息处理。

## 完成后给用户的信息

- **Web Console**：`http://127.0.0.1:2026/ui/`
- **OpenAI 兼容 base URL**：`http://127.0.0.1:2026/v1`
- **MCP**：`http://127.0.0.1:2026/mcp`
- **Web Console token**：安装输出列出的私有凭据文件——`scripts/setup.sh` / `memgw` 路径写入 `gateway.key`，容器化（Docker）首启写入 `gateway.txt`；恢复或查找时两种扩展名都被接受（仅 Console 管理用途）
- **客户端 token**：运行 `scripts/memgw token create --name DEVICE --role chat`；MCP 改用 `--role mcp`
- **模型名**：客户端填 `memory-auto`

## 手工校验

```bash
scripts/memgw stack status
scripts/memgw stack doctor
```

## 排错

| 现象 | 处理 |
| --- | --- |
| `setup.sh` 报找不到 Python ≥ 3.12 | 安装 Python 3.12+，或 `PYTHON_BIN=/path/to/python scripts/bootstrap.sh` |
| recipe 报未知字段 | 按 `docs/ai-quickstart.schema.json` 修正；不要把 key/secret 加入 JSON |
| JSON 中 `warnings` 非空或 `memgw_wired=false` | 按 warning 处理，再运行 `scripts/memgw stack doctor` |
| `stack doctor` 报 chat 路由无可用模型 | 确认渠道 API Key、base URL、模型 ID 正确，用 `modelgw check --live` 核实 |
| scoped token 丢失 | 撤销旧 token，再用 `scripts/memgw token create` 按设备和用途新建 |

更完整的说明见根 [README](../README.md) 与两个服务各自的 README。
