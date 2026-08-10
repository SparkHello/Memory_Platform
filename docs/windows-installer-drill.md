# Windows 安装器实机灾难恢复演练简报

> 读者：在另一台 Windows 机器上独立执行本演练的 AI agent（Kimi Code）。本文档自包含，不需要事先了解仓库上下文。
>
> 仓库地址：`https://github.com/SparkHello/Memory_Platform.git`（公开仓库）。`git clone` 该仓库可用于对照 `deploy/install.ps1` 实现，但不是执行演练的硬前提——演练只需要从固定 release 下载的安装脚本。本文档所有预期行为均已对照 `deploy/install.ps1`（约 2400 行）逐条核实；引用其函数名以便你区分"真 bug"与"设计如此的 fail-closed"。

## 任务目标与背景

Memory Platform（双容器 Docker 栈：memory-gateway + model-gateway）即将发布 0.2.0。macOS 侧的全面安全审查（`docs/security-audit-2026-08.md`）留下一条 P2：Windows 安装器 `deploy/install.ps1` 只经过 PowerShell 7.4 parser 与 Linux 容器中的动态 journal/fault 回归，**没有在真实 NTFS / Docker Desktop Windows 上做过最终灾难恢复演练**。正式发布 Windows 支持前，必须在真机验证三件事：凭据文件 DACL、FileShare 锁行为、中断/掉电恢复。你的任务就是在一台 Windows 机器上完成这场演练并按本文档末尾模板汇报结果。

## 红线（先读，违反即演练作废）

1. **不修改任何仓库代码或安装器副本来"修复"或"绕过"问题**，包括为了命中中断窗口给安装器加 `Start-Sleep`。发现问题只记录进报告。
2. **报告、日志、截图、聊天记录中不得出现真实密钥值**：`credentials\*.key` 内容、任何 Bearer token、provider API key。需要比对时只输出布尔结果或哈希前缀。安装器本身设计为不打印密钥；如果任何输出里出现了密钥值，直接记为 P0。
3. **只使用独立演练目录**（本文档统一为 `$env:USERPROFILE\memory-platform-drill`）和它对应的 Compose project、四个 Docker 卷。不碰机器上任何已有安装、容器、卷、镜像。卸载时只精确删除该 project 的对象；**禁止** `docker system prune`、`docker volume prune` 等无差别清理命令。
4. 若机器上已存在 Memory Platform 安装（安装器会通过运行中容器标签自动发现），先停下来向机主确认；继续时必须显式设置 `MEMORY_PLATFORM_DIR` 指向演练目录（安装器检测到多套安装会拒绝："检测到多套安装；请显式设置 MEMORY_PLATFORM_DIR。"）。
5. provider API key 只有机主明确提供时才使用，且只通过 Web Console 界面或 `modelgw secret set --stdin` 输入。演练默认不产生任何真实推理调用（`/health`、`/readyz` 都是本地检查），除机主另行允许。
6. 演练产生的备份 zip 含明文数据（虽然演练数据应全部用合成内容），按敏感文件保管；机主确认报告已提交后，删除演练目录、备份与日志。

## 环境前提

- Windows 10/11，x64（安装器内置的 cosign 下载只支持 X64；其他架构需先自行安全安装 cosign，本演练不覆盖）。
- 演练盘为 **NTFS**（`(Get-Volume -DriveLetter C).FileSystem` 应为 `NTFS`）。安装器依赖 NTFS DACL；exFAT/网络盘上它应当 fail-closed（这是正确行为，见场景 B 可选负向项）。
- Docker Desktop（WSL2 后端）已安装并启动；当前用户在 `docker-users` 组。`docker info`、`docker compose version` 正常。不需要启用 Kubernetes。
- **PowerShell 7.4+（`pwsh`）为主环境**——这是 macOS 审查时做 parser 回归的基线。安装器声明兼容 Windows PowerShell 5.1+（代码不含 `&&`、`||`、`??`、`ForEach-Object -Parallel` 等 7+ 专有语法，有 CI 断言），场景 A 可另用 `powershell.exe`（5.1）复跑一次作为兼容性观察。
- 执行策略：只对当前进程放行，不改机器级策略：
  ```powershell
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
  ```
- 普通用户权限即可，不需要管理员。DACL 设置对象是用户自己目录下的文件。
- 网络可达：`raw.githubusercontent.com`（下载安装脚本/Compose）、`github.com`（cosign 与 Sigstore bundle）、`ghcr.io`（三枚发布镜像）、Sigstore 透明日志。
- 磁盘 ≥ 10 GB 可用。
- 环境变量：每次运行安装器前都要设置同一组变量（见下）；**不要**设置 `GATEWAY_API_KEY` / `MEMORY_CONSOLE_ADMIN_KEY`（安装器会拒绝，见场景 A 负向检查）。

## 演练准备

```powershell
# 每次开新窗口都要重设
$Version     = "v0.2.0"   # 或机主指定的 RC tag
$InstallDir  = "$env:USERPROFILE\memory-platform-drill"
$env:MEMORY_PLATFORM_VERSION = $Version
$env:MEMORY_PLATFORM_DIR     = $InstallDir
$env:MEMORY_NO_OPEN          = "1"   # 安装结束不自动打开浏览器

New-Item -ItemType Directory -Force "$env:USERPROFILE\drill-artifacts" | Out-Null
New-Item -ItemType Directory -Force "$env:USERPROFILE\drill-logs" | Out-Null

# 下载固定 release 的安装脚本（禁止 irm ... | iex 管道执行可变分支）
irm "https://raw.githubusercontent.com/SparkHello/Memory_Platform/$Version/deploy/install.ps1" -OutFile install-memory-platform.ps1
Unblock-File .\install-memory-platform.ps1
```

所有安装器运行都把输出同时落盘（安装器不会打印密钥，但日志仍按敏感材料处理）：

```powershell
& .\install-memory-platform.ps1 *>&1 | Tee-Object "$env:USERPROFILE\drill-logs\<场景>-<次数>.log"
$LASTEXITCODE   # 记录每次退出码
```

下文所有 `curl.exe` 指 Windows 自带的 `curl.exe`，不是 PowerShell 的 `curl` 别名。

---

## 场景 A：全新安装

### 目的

验证正常流程端到端可用，并确认全新安装的安全边界（端口面、密钥面、凭据交付）。

### 操作

```powershell
& .\install-memory-platform.ps1 *>&1 | Tee-Object "$env:USERPROFILE\drill-logs\A-install.log"
```

### 预期与判定

预期按顺序出现这些步骤行（顺序本身是实现契约，macOS 整改点之一就是"先备份再下载再替换"）：

- `==> 检查运行环境`
- `==> 下载 v0.2.0 Compose 并校验`
- `==> 拉取三枚 semver 发布镜像`
- `==> 验证三枚镜像的 Sigstore 发布签名`
- `==> 校验 split stack 的端口、网络、UID 与卷隔离`
- `==> 在无宿主发布端口的隔离模式启动候选服务`
- `==> 通过容器内部链路验收 Memory、Model 与固定 TCP relay`
- `==> 发布已验收的 Memory 入口`
- 结尾打印 `Memory Platform v0.2.0 已启动`、Console/Client URL、`Console token: <路径>`、`Admin key: <路径>`（只打印路径，不打印值）、`密钥值没有进入脚本输出、Compose 环境或 Docker 日志。`

退出码 0。然后逐项验证：

```powershell
# 1. 容器状态：model-gateway / memory-gateway 为 Up (healthy)；stack-init 为 Exited (0)
docker compose -f "$InstallDir\docker-compose.user.yml" ps -a

# 2. liveness 200
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:2026/health

# 3. readiness：未配置模型时预期 503（不是 bug，见下）
curl.exe -s http://127.0.0.1:2026/readyz

# 4. 端口面：2030 不得发布；2026 只绑回环
docker compose -f "$InstallDir\docker-compose.user.yml" port model-gateway 2030   # 预期无输出
docker compose -f "$InstallDir\docker-compose.user.yml" port model-gateway 2026   # 预期 127.0.0.1:2026

# 5. 密钥面：.env 与 Compose 渲染结果中不得出现访问密钥字段
Select-String -Path "$InstallDir\.env" -Pattern "GATEWAY_API_KEY|MEMORY_CONSOLE_ADMIN_KEY"   # 预期 0 命中
docker compose -f "$InstallDir\docker-compose.user.yml" config | Select-String "GATEWAY_API_KEY|MEMORY_CONSOLE_ADMIN_KEY"   # 预期 0 命中

# 6. 凭据文件存在且非空（不要输出内容）
(Get-Item "$InstallDir\credentials\gateway.key").Length -gt 0
(Get-Item "$InstallDir\credentials\admin.key").Length -gt 0
```

### 已知正确行为（实现依据）

- **全新安装只验收 `/health`，不验收 `/readyz`**：安装器的宿主就绪等待为 `/health` 恒等 + `/readyz` 仅在非 fresh 布局时等待（`Invoke-MemoryPlatformInstall` 末尾的 `($script:Layout -ne "fresh" -and ...)` 条件）。`/readyz` 要求八条稳定 route 全部可用（`services/memory-gateway/app/api/health.py`），首次安装、尚未配置任何渠道时返回 503 `{"status":"not_ready","code":...}` 是**设计如此**。判定 bug 的方向相反：未配置模型时 `/readyz` 返回 200 才是 P0（"看似就绪、实际不可用"）。
- 若机主提供了 provider key：打开 `http://127.0.0.1:2026/ui/`，用 `credentials\gateway.key` 里的 Console token 登录，按模型向导完成渠道配置（向导只做只读 `/models` 发现，不自动发起推理）。配置完成后 `/readyz` 应在短时间内变为 200。**优先使用 Console 向导**，它会同步 Memory 侧的 route/embedding 契约；不要用容器内 `modelgw` 手工新增 embedding 配置（空间 ID 与 Memory 设置失配会导致 `/readyz` 报 `model_gateway_embedding_contract_mismatch`，该路径不是安装器覆盖面）。
- 密钥只经 `credentials\gateway.key`、`credentials\admin.key` 两个文件交付（离线 `stack-init` 容器经 bind mount 写入），不进入终端输出、Compose 环境或 Docker 日志。
- `stack-init` 是一次性离线初始化容器，`network_mode: none`，完成后退出；`stack-maintenance` 只在显式 `--profile maintenance` 时运行。

### 负向快速检查（每个都应在改动任何状态之前失败、exit 1、旧状态不变）

```powershell
$env:GATEWAY_API_KEY = "x"; & .\install-memory-platform.ps1; $LASTEXITCODE; Remove-Item Env:GATEWAY_API_KEY
# 预期：安装失败：新版安装器不接受环境变量中的密钥；请让离线初始化写入 credentials\*.key。

$env:MEMORY_HOST = "1.2.3.4"; & .\install-memory-platform.ps1; $LASTEXITCODE; Remove-Item Env:MEMORY_HOST
# 预期：安装失败：MEMORY_HOST 只允许 127.0.0.1 或 0.0.0.0。

$env:MEMORY_PLATFORM_VERSION = "main"; & .\install-memory-platform.ps1; $LASTEXITCODE
# 预期：安装失败：MEMORY_PLATFORM_VERSION 必须是 vX.Y.Z 形式的发布版本。
$env:MEMORY_PLATFORM_VERSION = $Version   # 恢复
```

---

## 场景 B：凭据文件 DACL 检查（NTFS）

### 目的

验证宿主凭据目录与各私有文件的 ACL 仅限当前 Windows 用户，语义对照 macOS 的 `0600`/`0700`。

### 操作

场景 A 完成后执行：

```powershell
$targets = @(
  "$InstallDir\credentials",
  "$InstallDir\credentials\gateway.key",
  "$InstallDir\credentials\admin.key",
  "$InstallDir\.env",
  "$InstallDir\.memory-platform-install.lock",
  "$InstallDir\backups"
)
foreach ($p in $targets) {
  $acl = Get-Acl $p
  [pscustomobject]@{
    Path           = $p
    InheritBlocked = $acl.AreAccessRulesProtected
    RuleCount      = $acl.Access.Count
    Identity       = ($acl.Access | ForEach-Object IdentityReference) -join ';'
    Rights         = ($acl.Access | ForEach-Object FileSystemRights) -join ';'
    IsInherited    = ($acl.Access | ForEach-Object IsInherited) -join ';'
  }
} | Format-List

icacls "$InstallDir\credentials\gateway.key"
icacls "$InstallDir\credentials"
whoami
```

### 预期与判定

每个对象必须同时满足：

- `InheritBlocked = True`（继承已禁用）；
- `RuleCount = 1`，唯一一条 Allow 规则的 `Identity` 等于 `whoami` 的当前用户（本机账户形如 `MACHINE\user`，域/AAD 账户形如 `DOMAIN\user`）；
- `Rights = FullControl`，`IsInherited = False`；
- `icacls` 输出中**只有这一行 ACE**：文件为 `<用户>:(F)`，目录为 `<用户>:(OI)(CI)(F)`；不得出现 `Administrators`、`SYSTEM`、`BUILTIN\Users`、`Everyone` 等任何其他条目。

通过/失败判定：

- **通过**：上述全部满足。语义对照：仅属主 FullControl ≈ macOS `0600`（文件）/`0700`（目录）；禁用继承 ≈ 无 group/other 授权；目录规则的 `(OI)(CI)` 使新建子文件自动继承"仅当前用户"。
- **P1**：任何对象多出第二条 ACE、继承未禁用、或凭据文件可被 `Users`/`Everyone`/`SYSTEM`/`Administrators` 读取。

若场景 C/E 跑过升级，`backups\pre-upgrade-*.zip` 与中断时残留的 `.memory-platform-cutover\` journal 目录及其中文件也应是同一 ACL 形态，顺手抽查记录。

### 已知正确行为（实现依据）

- `Protect-PrivatePath` 的实现：`SetAccessRuleProtection($true, $false)` 禁用继承并丢弃继承规则 → 删除全部既有 ACE → 只加当前用户 FullControl（目录带 ContainerInherit|ObjectInherit）。它对凭据目录、备份目录、`.env`、安装锁、备份 zip、journal 目录与文件、候选临时文件逐一调用。
- ACL 设置失败会中止安装："无法把私有文件权限限制为当前 Windows 用户；请使用本机 NTFS 目录后重试。"——即非 NTFS 上**必须 fail-closed 而不是弱化权限继续**。
- 可选负向项（有可移动盘时执行）：`MEMORY_PLATFORM_DIR` 指向 exFAT U 盘重跑全新安装，预期在上述错误处中止。无合适设备则记"未覆盖"。

---

## 场景 C：升级——先备份再升级、readiness 退化自动回滚

### 目的

验证升级流程的事务边界：备份先于下载；候选先在无宿主端口的隔离模式验收；readiness 退化时自动回滚到旧栈。

### C1 同版本重跑（完整 cutover 周期）

```powershell
& .\install-memory-platform.ps1 *>&1 | Tee-Object "$env:USERPROFILE\drill-logs\C1-rerun.log"
```

预期与判定：

- 输出顺序必须是 `==> 准备升级前备份` → `备份已保存：<...>\backups\pre-upgrade-<时间戳>-<pid>.zip` → `==> 下载 v0.2.0 Compose 并校验` → … → `==> 旧服务已停写，创建并复验最终一致性备份` → 隔离验收 → `==> 发布已验收的 Memory 入口` → 成功总结（含 `升级前备份：<路径>`）。
- **备份先于下载**是 macOS P1 整改的硬契约，顺序颠倒即 P1。
- 结束后 `backups\` 应新增两份 zip：在线备份 `pre-upgrade-<时间戳>-<pid>.zip` 与停写时点备份 `pre-upgrade-<时间戳>-<pid>-quiesced.zip`，以及旧 Compose 副本 `pre-upgrade-<时间戳>.compose.yml`。
- 成功后 journal 已清理：`Test-Path "$InstallDir\.memory-platform-cutover"` 应为 False。
- `/health` 200；若场景 A 已配置模型，`/readyz` 200。
- 已知正确行为：**同版本重跑不短路**，仍走完整备份 + journal + cutover（实现不比较新旧版本）；默认只保留最近 5 份 `pre-upgrade-*.zip`（`MEMORY_BACKUP_RETENTION`，1–50）。

### C2 跨版本升级（有条件执行）

仅当机主提供第二个已发布 tag 时：把 `MEMORY_PLATFORM_VERSION` 改为新 tag 重跑，预期同 C1，且 `.env` 中 `MEMORY_PLATFORM_*_IMAGE` 变为新 digest、容器镜像随之更新。没有第二个 tag 时记"未覆盖"（单 tag 环境下 C1 已覆盖全部 cutover 机制，唯一未覆盖的是镜像 digest 真实变化）。

### C3 readiness 退化自动回滚

按环境二选一：

- **未配置模型的环境**：直接重跑安装器即天然触发——候选内部验收的 `/readyz` 检查（90 次尝试，约 90 秒）必然失败。这本身就是有效的回滚演练。
- **已配置模型的环境**：先制造退化再重跑：
  ```powershell
  docker compose -f "$InstallDir\docker-compose.user.yml" exec -T model-gateway modelgw route list
  # 记录 knowledge.pro 的 target deployment 名，善后要用
  docker compose -f "$InstallDir\docker-compose.user.yml" exec -T model-gateway modelgw route remove knowledge.pro
  & .\install-memory-platform.ps1 *>&1 | Tee-Object "$env:USERPROFILE\drill-logs\C3-rollback.log"
  ```

预期与判定（两种环境相同）：

- 安装器跑到"通过容器内部链路验收"阶段后失败，退出码 1，错误为：`候选内部 readiness 退化；旧服务和数据已恢复。`
- 回滚后：旧栈容器在跑、`/health` 200；`Test-Path "$InstallDir\.memory-platform-cutover"` 为 False（回滚成功后 journal 按 committed 语义清理）；`docker-compose.user.yml` 与升级前字节一致（可用 `Get-FileHash` 对比 C1 前后）。
- 已配置环境注意：回滚会把三卷数据回灌到**停写备份时点**（此时 `knowledge.pro` 已删），所以 `/readyz` 仍 503 属预期；善后恢复 route 并确认 `/readyz` 回到 200：
  ```powershell
  docker compose -f "$InstallDir\docker-compose.user.yml" exec -T model-gateway modelgw route set knowledge.pro <记录的deployment> --kind chat
  ```
- **P0 判定**：错误变成 `……且自动回滚不完整`；或旧栈起不来；或 journal 消失但运行的是新栈/数据丢失。
- **P1 判定**：安装器在 readiness 退化后仍然提交并发布新栈（即把"看似成功实际退化"交付给用户）。

### 已知正确行为（实现依据）

- 回滚（`Invoke-Rollback`）只在 commit 前发生：停候选 →（split 布局）用停写时点 quiesced 备份经 `restore_split.py` 回灌 memory-data/memory-secrets/model-data 三卷 → 按字节快照恢复 `.env` → 原子恢复旧 Compose → 用 journal 记录的精确旧镜像 `--pull never` 重启 → journal 标记 committed 后删除。回滚失败会明确报"不完整"，绝不静默。
- commit（`Mark-CutoverCommitted`，durable 写 `phase.txt = committed`）是**单向门**：commit 之后任何失败都不再回滚（"升级已提交但……；不会回滚……journal 已保留供重试"），因为新栈此时可能已接受新写入，用旧备份反向覆盖会更糟。C 场景不应观察到 commit 后回滚；观察到即 P0。

---

## 场景 D：FileShare 锁与并发安装器

### 目的与范围说明

**重要背景**：真实 SQLite 数据库位于 Docker 命名卷（WSL2 虚拟机磁盘），**不落在 NTFS 上**，宿主进程无法占用它们；安装器对数据一致性的答案是"在线备份 + 停写后 quiesced 备份 + 恢复前先停服务"的流程设计，而不是宿主文件锁。因此"在宿主上打开 SQLite 文件导致备份 fail-closed"这一字面场景**无实现依据，不要按字面去测 DB 文件锁**。审计 P2 中"FileShare 锁"在 NTFS 上的落实面是安装器自己管理的宿主文件：安装锁、`.env`、Compose、journal、备份 zip。以下 D1–D4 均有实现依据。

### D1 并发第二个安装器

一个窗口正常重跑安装器，另一个窗口立即再跑一次。

预期：第二个实例立即失败，退出码 1：`安装失败：另一安装器仍在运行，或无法取得安装事务排他锁；本次未修改任何状态。` 且它确实没有下载/备份/改写任何文件。若第一个实例结束太快来不及重叠，以 D2 的确定性结果为准，本项记观察。

### D2 独占持有安装锁（确定性）

```powershell
$fs = [System.IO.FileStream]::new("$InstallDir\.memory-platform-install.lock", 'OpenOrCreate', 'ReadWrite', 'None')
& .\install-memory-platform.ps1; $LASTEXITCODE   # 在另一个 pwsh 进程里跑
$fs.Dispose()
```

预期同 D1 的错误信息与退出码 1；`$fs.Dispose()` 后重跑正常。实现依据：`Acquire-InstallerLock` 以 `FileShare.None` + WriteThrough 打开 `.memory-platform-install.lock` 并写入 PID。

### D3 `.env` 被独占打开（升级流程中）

```powershell
$fs = [System.IO.FileStream]::new("$InstallDir\.env", 'Open', 'Read', 'None')
& .\install-memory-platform.ps1; $LASTEXITCODE   # 另一个 pwsh 进程
$fs.Dispose()
```

预期：安装器在最早期读取 `.env` 快照即抛"文件被占用"类 IOException，退出码 1（该错误不带"安装失败："前缀，是原始 .NET 错误信息——属正常）；旧栈保持运行，Compose/`.env`/数据卷均未被修改。**判定 P1**：安装器绕过占用继续改写了任何文件。实现依据：安装器不杀占用方、不降级共享模式；对 `.env` 的所有写入走 `[IO.File]::Replace` 原子替换（`Write-TextAtomic`/`Write-BytesAtomic`），占用只会让操作失败并进入 fail-closed 分支。

### D4 旧备份被独占打开时的保留清理（观察项）

先在 `backups\` 制造至少 2 份 `pre-upgrade-*.zip`（跑过 C1 即满足），设 `$env:MEMORY_BACKUP_RETENTION=1`，对较旧那份持 `FileShare.None`，重跑安装器。预期：`Remove-StaleHostBackups` 删除失败 → 安装中止、退出码 1；此刻新备份已完成但旧栈未被修改、journal 未创建（清理发生在下载与停栈之前）。结束后 `Remove-Item Env:MEMORY_BACKUP_RETENTION`。记录实际行为。

### 判定总则（D 全场景）

宿主文件被占用的唯一合法后果是安装器中止或回滚；任何"绕开锁继续写"的行为都是 P1。

---

## 场景 E：中断/掉电恢复（journal 幂等恢复）

### 目的

在升级事务的关键窗口强行终止安装器进程，验证重跑后按 journal 阶段幂等恢复，绝不出现半提交状态。

**方法学说明（如实记录的限制）**：整机断电无法安全实机模拟；本场景用 `Stop-Process -Force` 杀安装器进程 + 退出 Docker Desktop 近似。`MoveFileExW(MOVEFILE_WRITE_THROUGH)`、`Flush(true)` 的断电持久性语义由 NTFS 与硬件保证，进程 kill 无法区分"掉电"与"死进程"——这一差距在报告中如实注明。

### kill 方法

```powershell
$log = "$env:USERPROFILE\drill-logs\E-run1.log"
$proc = Start-Process pwsh -PassThru -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',
        "& { `$env:MEMORY_PLATFORM_VERSION='$Version'; `$env:MEMORY_PLATFORM_DIR='$InstallDir'; `$env:MEMORY_NO_OPEN='1'; & .\install-memory-platform.ps1 *>&1 | Tee-Object '$log' }"
# 轮询日志，命中目标步骤行即杀（输出有缓冲，允许落在窗口附近）
while (-not (Select-String -Path $log -Pattern '旧服务已停写' -Quiet -ErrorAction SilentlyContinue)) { Start-Sleep -Milliseconds 500 }
Stop-Process -Id $proc.Id -Force
# 然后重跑安装器，观察恢复
& .\install-memory-platform.ps1 *>&1 | Tee-Object "$env:USERPROFILE\drill-logs\E-run1-rerun.log"
```

每个窗口一次"kill + 重跑"记一条结果；同一窗口多次尝试命中不同窗口。机器太快命中不了某窗口时如实记"未命中"，不允许改安装器加延时。

### 窗口与预期（实现依据：`Restore-InterruptedCutover`，每次运行在改动任何状态之前执行）

| 窗口（日志中的标志行） | journal 状态 | 重跑预期 |
| --- | --- | --- |
| E0 `下载 v0.2.0 Compose 并校验` / `拉取三枚 semver 发布镜像` | 无 journal | 重跑正常；旧栈全程未被触碰。可能残留 `.docker-compose.user.yml.candidate.<32hex>` 等隐藏临时文件（唯一后缀设计，不影响重跑），可安全删除，记录残留情况。 |
| E1 `准备升级前备份` | 无 journal | 同上；可能多留一份 `backups\pre-upgrade-*` 备份（含明文，按敏感处理）。 |
| E2 `旧服务已停写，创建并复验最终一致性备份` | `prepared` | 重跑先打印 `==> 检测到中断的升级事务，先幂等恢复旧栈`：原子恢复旧 Compose 与 `.env`、重启旧栈（prepared 阶段不做数据回灌），打印 `中断升级已恢复；继续重新执行发布校验。` 后继续新一轮升级。 |
| E3 `在无宿主发布端口的隔离模式启动候选服务` / `通过容器内部链路验收…` | `data_may_change` | 恢复流程额外用 quiesced 备份经 `restore_split.py` 回灌三卷，再重启旧栈。数据必须回到停写时点（可用场景 F 的基线方法核对）。 |
| E4 `发布已验收的 Memory 入口` | `committed` | 重跑打印 `已完成中断升级的端口发布；继续校验当前版本。`：把已验收的新栈发布到宿主端口、等待 `/health`+`/readyz`、清理 journal，**绝不回滚数据**。若发布未完成会报 `已提交升级尚未完成端口发布；journal 已保留供下次幂等恢复。`，再次重跑应继续幂等。E4 只有在场景 A 已配置模型、`/readyz` 可达 200 的环境下才能达到（安装器设计要求候选 readyz 通过才 commit）；未配置环境记"不可达"。 |
| E5 任意窗口退出 Docker Desktop（托盘 Quit） | 视时机 | 安装器因 docker 命令失败走各自 fail-closed 分支退出。Docker 未恢复时重跑：`安装失败：Docker Desktop 尚未运行。`（该检查在碰任何状态之前）。启动 Docker Desktop 后重跑：按上表对应 journal 阶段恢复。 |

### 每次"kill + 重跑"的通用判定标准

只允许三种结局：

1. 旧栈被完整恢复（Compose/`.env` 与升级前一致，容器健康，数据回到停写时点）；或
2. 已提交的新栈被完成发布；或
3. 明确报错退出且 `.memory-platform-cutover\` journal 保留、再次重跑可继续幂等恢复。

以下任何一种都是 **P0**：

- `.memory-platform-cutover` 与 `.memory-platform-cutover.committed-cleanup` 都消失了，但运行状态既不是完整旧栈也不是完整新栈（journal 丢失 = 失去恢复依据）；
- `docker-compose.user.yml` 内容既非旧版也非候选新版（半写）；
- 新旧栈同时发布/争抢端口；
- 数据丢失或数据与备份时点矛盾；
- journal 不完整或字段无效时重跑仍"静默继续"（合法行为是报 `升级事务 journal 不完整；拒绝覆盖当前状态。` 等明确错误并保留 journal 供重试）。

其他已知正确行为：

- journal 目录形态：`.memory-platform-cutover\` 内含 `phase.txt`（`prepared` → `data_may_change` → `committed`）、`metadata.json`（version=2、project、layout、备份文件名、旧镜像 digest、发布 host/port 等）、`old-compose.yml`、`old.env`；staging 目录名为 `.memory-platform-cutover.pending.<guid>`，清理墓碑为 `.memory-platform-cutover.committed-cleanup`。
- 命中 `New-CutoverJournal` 内部极小窗口时可能残留 `.memory-platform-cutover.pending.<guid>` staging 目录；安装器**没有**主动清理它的逻辑（预期不影响重跑）。这是"无实现依据"观察项：记录是否残留、是否影响重跑。
- 杀进程后 `.memory-platform-install.lock` 的 OS 句柄随进程死亡释放，重跑不应报锁错误；报了即为异常，记录。
- 中断在 journal 创建之前只可能留下可安全清理的临时文件/备份，不会切换任何 live 状态——与审计报告"最终接受风险"一节口径一致。

---

## 场景 F：便携备份 v2 完整恢复演练

### 目的

走完 备份 → 卸载 → 全新安装 → 恢复 → 数据一致性核对 全链路。

### F1 造种子数据并记录基线

```powershell
$gw = [IO.File]::ReadAllText("$InstallDir\credentials\gateway.key").Trim()   # 只进变量，禁止外发

# 保存第一份 Console token，供 F6 正反核对（只存本地 drill-artifacts，F7 统一删除，报告不得包含）
Copy-Item "$InstallDir\credentials\gateway.key" "$env:USERPROFILE\drill-artifacts\gateway-first.key"

# 创建一个 chat scope token（写入 auth.db，随备份迁移）
curl.exe -s -X POST -H "Authorization: Bearer $gw" -H "Content-Type: application/json" `
  -d "{\"name\":\"drill-chat\",\"role\":\"chat\"}" http://127.0.0.1:2026/auth/tokens
# 从返回 JSON 只记录 token_id（16 位 hex）；token 值留在本地变量/临时文件，不进报告

# 基线快照（含合成记忆正文，只存本地 drill-artifacts，报告只贴条数）
curl.exe -s -H "Authorization: Bearer $gw" "http://127.0.0.1:2026/memories?limit=200" -o "$env:USERPROFILE\drill-artifacts\memories-before.json"
curl.exe -s -H "Authorization: Bearer $gw" "http://127.0.0.1:2026/knowledge/documents" -o "$env:USERPROFILE\drill-artifacts\docs-before.json"
curl.exe -s -H "Authorization: Bearer $gw" "http://127.0.0.1:2026/auth/tokens" -o "$env:USERPROFILE\drill-artifacts\tokens-before.json"
```

若场景 A 已配置真实 provider，可另外通过 Console 聊一条合成内容产生至少一条记忆；未配置时基线为初始状态也成立（比对"前后一致"即可）。

### F2 创建便携备份

```powershell
Push-Location $InstallDir
docker compose -f docker-compose.user.yml --profile maintenance run --rm stack-maintenance `
  --home /data/config --project-root /app/services/memory-gateway stack backup `
  --model-gateway-home /model-data --output /data/drill-backup.zip
docker compose -f docker-compose.user.yml cp memory-gateway:/data/drill-backup.zip "$env:USERPROFILE\drill-artifacts\drill-backup.zip"
Pop-Location
```

预期：退出码 0，zip 拷出且非空。备份 v2 必含记忆库、知识库、Auth token 哈希库和 Model Gateway 脱敏配置（usage 明确标记 present/absent），`secrets_included=false`——**不含** provider key、admin key、backend key 或任何 token 明文。可用 `tar -tf drill-backup.zip` 只列文件名核对结构，不解压、不记录正文。卷内残留 `/data/drill-backup.zip` 属预期，随演练卷一起删除。

### F3 卸载（只删演练对象）

```powershell
Push-Location $InstallDir
$project = (Select-String -Path .\.env -Pattern '^COMPOSE_PROJECT_NAME=(.+)$').Matches.Groups[1].Value
docker compose -f docker-compose.user.yml down
docker volume ls --filter "label=com.docker.compose.project=$project"   # 确认只列出四个演练卷
docker volume rm "${project}_memory-data" "${project}_memory-secrets" "${project}_model-data" "${project}_model-secrets"
Pop-Location
# 删目录前确认 F1/F2 的产物已在目录外
Test-Path "$env:USERPROFILE\drill-artifacts\gateway-first.key"   # 预期 True
Test-Path "$env:USERPROFILE\drill-artifacts\drill-backup.zip"    # 预期 True
Remove-Item -LiteralPath $InstallDir -Recurse -Force   # 仅限演练目录
```

### F4 全新安装

重设场景 A 的环境变量后重跑安装器（同场景 A 命令），验证 `/health` 200。记录新装的 `credentials\gateway.key` 路径存在（值同样不进报告）。

### F5 恢复

```powershell
Push-Location $InstallDir
docker compose -f docker-compose.user.yml cp "$env:USERPROFILE\drill-artifacts\drill-backup.zip" memory-gateway:/data/restore.zip
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml --profile maintenance run --rm `
  --entrypoint python stack-maintenance /usr/local/libexec/memory-platform/restore_split.py
docker compose -f docker-compose.user.yml up -d
Pop-Location
```

预期：恢复命令退出码 0（恢复前会校验清单哈希、SQLite 与 schema；备份被篡改时必须 fail-closed，这是可选负向项）；`up -d` 后 `/health` 200。

### F6 一致性核对

```powershell
$gwOld = [IO.File]::ReadAllText("$env:USERPROFILE\drill-artifacts\gateway-first.key").Trim()   # 禁止外发
```

- **Auth 迁移（核心检查）**：备份只含 token 哈希。恢复后 auth.db 被备份内容替换，因此：
  - 用**第一次安装**交付的 `gateway.key`（F3 删目录前应已把它安全复制到 `drill-artifacts`，或提前用变量保存）：`curl.exe -s -o NUL -w "%{http_code}" -H "Authorization: Bearer $gwOld" http://127.0.0.1:2026/auth/tokens` → 预期 200，且返回列表包含 F1 记录的 `drill-chat` token_id；
  - 用**新安装**交付的 `gateway.key` 调同一接口 → 预期 401（新 token 的哈希不在恢复回来的 auth.db 里）。这一正一反直接证明"token 哈希随备份迁移、新设备不自动获得旧凭据"。
  - 按官方口径在新设备补发 Console token（主机 CLI）：
    ```powershell
    docker compose -f "$InstallDir\docker-compose.user.yml" exec -T memory-gateway memgw token create --role console --name drill-recovered --user default
    ```
    用新 token 调 `/auth/tokens` 应 200。
- **业务数据**：重新拉取 `/memories?limit=200`、`/knowledge/documents`，与 `drill-artifacts\*-before.json` 比较（条数与内容一致；`Compare-Object` 或哈希对比均可，报告只贴条数结论）。
- **secret 边界**：便携备份不覆盖目标机 secret 卷——新安装的 backend key / admin key 继续有效（`/readyz` 的 Model 控制面检查不因密钥失配失败）。若 F1 环境配置过真实 provider：provider key 存于旧 secret 卷、不在备份里，恢复后该连接 secret 缺失、`/readyz` 退化属**预期**；在 Console 重新输入 provider key 后 `/readyz` 应回 200。与运维文档"目标机没有相应密钥时，需要在恢复后重新输入"口径一致。
- **无实现依据观察项**：`restore_split.py` 在"两个长期服务未停止"时被误用的防护不由 `install.ps1` 覆盖（安装器自己的恢复路径总是先停容器；运维文档要求恢复必须在停服后执行）。如有余力可观察未停服直接跑恢复的实际行为并记录，不得预设结论。

### F7 演练后清理

机主确认报告已提交后：撤销/删除种子 token，删除演练 project 容器与四卷（同 F3），删除 `$InstallDir`、`drill-artifacts\`、`drill-logs\` 与下载的安装脚本。删除前再确认一遍路径只指向演练对象。

---

## 结果汇报模板

```markdown
# Windows 安装器灾难恢复演练报告

## 环境
- Windows 版本/构建：（winver 或 [System.Environment]::OSVersion）
- 演练盘文件系统：（Get-Volume 输出，确认 NTFS）
- PowerShell：$PSVersionTable.PSVersion（主环境 pwsh 7.4+；是否另测 5.1）
- Docker Desktop / Engine / Compose：docker version、docker compose version 输出
- 安装器 release tag：
- 是否配置了真实 provider：是/否（不提供任何 key 值）
- 机器上是否已有其他 Memory Platform 安装：是/否

## 场景结果
| 场景 | 通过/失败/未覆盖 | 关键证据（日志文件名、命令输出摘录，不含密钥） | 备注 |
| A 全新安装 | | | |
| A 负向检查 | | | |
| B DACL | | | |
| C1 同版本重跑 | | | |
| C2 跨版本 | | | |
| C3 readiness 回滚 | | | |
| D1–D4 FileShare 锁 | | | |
| E0–E5 中断恢复（逐窗口） | | | |
| F 备份恢复全链路 | | | |

## 发现的问题
| 编号 | 优先级 | 场景 | 现象 | 最小复现 | 预期 vs 实际 |
（优先级口径：P0 = 数据丢失/半提交/journal 丢失后状态矛盾/密钥值进入输出或日志；
P1 = fail-closed 被破坏（该回滚未回滚、ACL 多出条目、锁被绕过、备份顺序颠倒）；
P2 = 残留文件、文案、时序体验等。）

## 未覆盖项与原因
（如：E4 因未配置 provider 不可达；C2 无第二个 tag；真断电未模拟只有进程级 kill；等）

## 附件
- 日志文件清单（drill-logs\ 下，已确认不含密钥值）
```

## 附：本演练中"无实现依据、需重点观察实际行为"的场景清单

以下场景在 `deploy/install.ps1` 中找不到对应保护逻辑（或逻辑不在安装器边界内）。演练时不要预设结论，如实记录实际行为：

1. **宿主打开 SQLite 文件占用 → 备份/恢复失败**：无实现依据。数据库在 Docker 卷的 WSL2 虚拟磁盘里，不经过 NTFS；安装器的一致性靠"停写后备份"流程而非宿主文件锁。FileShare 的真实落实面是场景 D 的宿主文件。
2. **整机断电（区别于杀进程）**：无法安全实机模拟；WriteThrough/Flush/MoveFileExW 的断电语义只有代码与 NTFS 保证，本演练以 `Stop-Process -Force` 与退出 Docker Desktop 近似，差距在报告中注明。
3. **`.memory-platform-cutover.pending.<guid>` staging 残留**：安装器没有清理旧 pending 目录的逻辑；预期不影响重跑，观察并记录。
4. **未停服误用 `restore_split.py`**：不由安装器覆盖（运维文档要求先停服）；行为需观察记录。
5. **E4（committed 后中断）在未配置模型的环境不可达**：安装器设计要求候选 `/readyz` 通过才 commit；需要真实 provider 配置后才能达到该窗口。
6. **跨版本（不同 tag）升级**：需要第二个已发布 tag；单 tag 环境下 C1 同版本重跑已覆盖全部 cutover 机制，仅"镜像 digest 真实变化"未覆盖。
7. **legacy 单卷 → split 迁移**：不在本次范围（需要旧版部署夹具；审计 P2 只要求 NTFS 上的 DACL、FileShare 锁与掉电恢复三项）。
