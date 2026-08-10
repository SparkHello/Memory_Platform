# 运行与检查

## 命令入口与 PATH

开发安装后，命令首先位于项目虚拟环境：

```bash
cd /path/to/Memory_Platform/services/model-gateway
.venv/bin/modelgw --version
```

运行下面的命令可从任意目录调用 `modelgw`：

```bash
.venv/bin/modelgw install-path
```

macOS/Linux 默认创建 `~/.local/bin/modelgw`。如果命令提示该目录不在 `PATH`，把下面一行加入 `~/.zshrc`：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

然后重新打开终端，或执行 `source ~/.zshrc`。macOS、Linux 和 Windows 都使用 PATH 这个概念；Windows 的用户界面通常显示为 `Path`，`install-path` 会打印需要加入的目录。

`--target-dir DIR` 可选择其他用户目录；已有启动器不会被覆盖，除非显式 `--force`。

## 初始化与诊断

```bash
modelgw init
modelgw doctor
modelgw schema
```

- `init` 创建缺失的配置和密钥文件，不覆盖已有内容；
- `doctor` 检查 JSON/关系图、文件权限、密钥引用、route、pricing 和运行依赖；
- `schema` 打印当前 `GatewayConfig` JSON Schema。

全局 `--home DIR` 可选择另一套隔离配置；全局 `--json` 输出供脚本消费的 JSON：

```bash
modelgw --home /path/to/isolated-config doctor
modelgw --json status
```

## 前台运行

```bash
modelgw run
# 等价：modelgw serve
```

前台模式适合首次调试和观察错误；按 `Ctrl-C` 停止。可临时覆盖监听设置：

```bash
modelgw run --host 127.0.0.1 --port 2030 --log-level info
```

`--no-access-log` 可关闭 Uvicorn access log。监听地址仍受本地回环校验约束。

## 后台运行

```bash
modelgw start
modelgw status
modelgw logs --lines 100
modelgw logs -f
modelgw stop
```

`start` 创建独立后台进程，写入 `service-state.json`，并把 stdout/stderr 追加到 `model-gateway.log`。默认不启用 access log；需要时显式加 `--access-log`。

`status` 同时检查：

1. 状态文件中的 PID 是否仍是由同一配置目录和 Python 解释器启动的网关进程；
2. 记录 URL 的 `/health` 是否正常响应。

健康时退出码为 0；未运行或 HTTP 未就绪时退出码为 1。`start` 若发现同一状态文件管理的服务已运行会直接报告；若端口上有一个不受当前状态文件管理的网关，则拒绝接管，避免误杀其他进程。

`stop` 只对通过身份校验的受管进程发送正常终止信号。可设置等待时间：

```bash
modelgw stop --timeout 10
```

超时不会自动强杀；确认后才使用：

```bash
modelgw stop --timeout 10 --force
```

## 本地健康与上游健康不是一回事

- `modelgw status`：本地后台进程和 `/health`；
- `modelgw check`：上游 connection 与 deployment；
- `GET /readyz`：服务能否读取有效配置、使用已配置 route，并确认 config/secrets/usage 存储可写且高于软保留量；磁盘失败只返回 `disk_low` 或 `disk_unavailable`，不暴露路径和字节数；
- `GET /v1/models`：经本地 client 鉴权后可用的业务 routes。

## 免费 discovery 检查

```bash
modelgw check
modelgw check all
modelgw check --connection CONNECTION_ID
modelgw connection check CONNECTION_ID
```

默认模式不会发送 chat 或 embedding 请求。每个允许检查的 connection 最多请求一次其 `models_endpoint`，通常是 `GET /models`。

结果含义：

- `available`：连接、鉴权和模型列表均正常；
- `connected_unlisted`：连接正常，但 deployment 的模型 ID 没出现在列表中；这不是废弃证明；
- `connected_unverified`：连接正常，但响应形状无法识别；
- `check_unsupported`：未配置 models endpoint，或 provider 不支持；
- `not_configured`：缺少 secret；
- `policy_blocked`：本次检查用途不允许访问该套餐；
- `auth_failed`、`network_error`、`provider_error`：相应的鉴权、网络或 provider 失败。

保存 connection 的 API Key 后也会自动运行同样的免费检查：

```bash
modelgw secret set CONNECTION_ID
```

只有显式 `--no-check` 才跳过。密钥输入使用无回显提示；`secret list` 永远不显示值。

## live 检查

```bash
modelgw check --connection CONNECTION_ID --live
modelgw connection check CONNECTION_ID --live
```

`--live` 会向每个已启用 deployment 发送最小真实 chat 或 embedding 请求，可能计费、消耗配额或触发 provider 限流。它不会因为 `GET /models` 未列出模型而自动执行；必须由用户显式指定。

Embedding live check 还会对比实际向量维度和配置的 `dimensions`，不一致时报告 `dimension_mismatch`。

## 交互套餐检查

`token_plan`、`coding_plan` 和显式 `interactive_only` connection 默认按 backend 用途检查，因此在任何网络请求前返回 `policy_blocked`。需要核对用户交互用途时，必须显式写：

```bash
modelgw check --connection INTERACTIVE_CONNECTION_ID --as-interactive
```

若还要发送真实请求，再额外确认：

```bash
modelgw check \
  --connection INTERACTIVE_CONNECTION_ID \
  --as-interactive \
  --live
```

`--as-interactive` 只影响这一次检查的 workload 身份，不修改配置、不改变 client 权限，也不会让 backend route 使用该连接。My_Memory 必须继续使用 `backend` client。

## 用量与费用

```bash
modelgw usage summary
modelgw usage summary --days 7
modelgw --json usage summary --days 30
modelgw usage prune
modelgw usage prune --vacuum
```

汇总只读取元数据事件：实际 deployment/connection/model、状态、耗时、attempts、provider 返回的 usage 和调用时价格快照。数据库不保存请求或响应正文。

原始事件默认保留 90 天并滚入按日汇总，日汇总保留 365 天。服务启动会执行轻量 prune；`usage prune` 可手工触发，`--vacuum` 还会在低峰期回收 SQLite 空闲页。rollup 使用 SQLite writer transaction，多进程同时触发也不会重复计数。

缺失明确 usage、官方 pricing 或必要单价的成功调用会保留为费用不完整；CLI 不猜测 Token，也不把未知价格当成零。

## 常见排查顺序

```bash
modelgw doctor
modelgw status
modelgw logs --lines 200
modelgw check
modelgw check --connection CONNECTION_ID --live
```

最后一步可能产生费用，只在 discovery 不能确认真实调用能力时使用。
