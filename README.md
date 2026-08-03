# Memory Platform

Memory Platform 把长期记忆服务和模型接入网关放在一个可移植的单仓库中，同时保留两者清晰的运行边界：

- `services/memory-gateway`：长期记忆、知识库、MCP、OpenAI-compatible 代理和 Web Console。
- `services/model-gateway`：供应商连接、deployment、route、价格、用量和受控管理接口。

源代码、测试和 CI 统一提交到本仓库；运行配置、密钥、SQLite 数据和日志仍保存在仓库外或被 Git 忽略。这样既方便换设备和开源，也不会把模型密钥或私人记忆提交出去。

## 首次安装

需要 Python 3.12、Node.js 22 和 npm。macOS/Linux 在仓库根目录执行：

```bash
scripts/bootstrap.sh
scripts/memgw stack install --start
```

`bootstrap.sh` 会在 `services/memory-gateway/.venv` 创建统一 Python 运行环境，以 editable 模式安装两个服务，并安装依赖、构建 Web Console。若暂时不需要前端：

```bash
scripts/bootstrap.sh --skip-ui
```

`stack install` 会初始化仓库外配置、为两个服务建立独立本地身份并按 Model Gateway → Memory Gateway 的顺序启动。它不会把密钥写入仓库 `.env`。

## 日常使用

```bash
scripts/memgw stack start
scripts/memgw stack status
scripts/memgw stack doctor
scripts/memgw stack restart
scripts/memgw stack stop
```

Web Console 默认位于 `http://127.0.0.1:2026/ui/`，模型网关默认位于 `http://127.0.0.1:2030/v1`。供应商连接、模型和用途路由由 Model Gateway 管理；Memory Gateway 只保存调用它所需的本地 client key。

各服务的详细说明见：

- [Memory Gateway](services/memory-gateway/README.md)
- [Model Gateway](services/model-gateway/README.md)

## 测试

完成安装后，从仓库根目录运行：

```bash
scripts/test.sh
```

该命令依次运行两个 Python 测试集和 Web Console 生产构建。也可以进入对应服务目录运行定向测试。

## 换设备

以后只需要迁移一个 Git 仓库：

```bash
git clone <你的新仓库地址> Memory_Platform
cd Memory_Platform
scripts/bootstrap.sh
scripts/memgw stack restore /path/to/memory-stack-backup.zip --start
```

旧设备先运行 `scripts/memgw stack backup --output /path/to/memory-stack-backup.zip`。便携备份包含允许迁移的配置和数据，但不包含任何密钥；新设备恢复后需要重新输入上游渠道密钥和本地 admin key。

## 提交工作区

两个服务现在共享根目录的一个 `.git`：

```bash
git status --short
git add -A
git commit -m "feat: describe your change"
```

不要在 `services/` 下再次执行 `git init`。本地迁移仓库把旧 My_Memory 远端保留为只供追溯的 `memory-gateway-origin`；发布前应创建一个新的空远端，再执行：

```bash
git remote add origin <新的 Memory_Platform 仓库地址>
git push -u origin main
```

不要把这个单仓库直接推送到旧的 My_Memory 远端。准备公开发布前还需要明确选择并添加开源许可证。
