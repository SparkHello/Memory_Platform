# 安卓客户端方案

目标：把 Memory Platform 整体装进一个安卓 App，在前台服务里常驻运行，只监听 `127.0.0.1`。
App 本身只显示运行状态；控制台仍然是现有的 Web UI（手机浏览器打开 `http://127.0.0.1:2026/`）；
手机上的聊天 App 把 Base URL 填成 `http://127.0.0.1:2026/v1` 即可走记忆代理，MCP 客户端填 `http://127.0.0.1:2026/mcp`。

## 已验证与未验证

| 项目 | 状态 |
| --- | --- |
| 两个服务在同一个 Python 进程、纯 asyncio + h11、不装 uvloop/httptools/websockets/watchfiles | 已在 macOS 干净虚拟环境验证：`/health`、`/readyz`、Web UI、MCP 401 均正常 |
| 进程内初始化（不调用 `memgw`/`modelgw` 子进程）：Model Gateway 客户端、settings.env、首次控制台令牌 | 已验证，重复运行幂等，令牌可直接登录 |
| `requirements-embedded.txt` 是完整闭包 | 已验证（`pip check` 通过）。去掉了服务从未导入的 cryptography、cffi、pycparser、pyyaml 后整套服务与鉴权、MCP、chat 端点仍正常，因此除两个 Rust 包外全部是纯 Python，不再依赖 Chaquopy 仓库里的任何原生包 |
| Termux 上编译 pydantic-core、rpds-py | 2026-09-02 在真机验证通过，编译约 10 分钟；整套服务在 Termux 里启动，浏览器用首次令牌登录成功，FTS5 可用 |
| 两个 Rust 轮子给 Chaquopy | 2026-09-02 已用 `termux-export-wheels.sh` 从手机导出 cp314 的 pydantic-core 2.46.4 和 rpds-py 2026.6.3（Termux 的 pip 原生就打 `android_24_arm64_v8a` 标签），放在 `apps/android/wheels/`；Chaquopy 能否加载仍待真机验证。备选 `build-rust-wheels.sh` 未验证 |
| Android 工程能否编译、Chaquopy 能否接受本地安卓轮子 | 2026-09-02 在 Mac 上用命令行工具（Homebrew android-commandlinetools + openjdk@21，`./gradlew assembleDebug`）编译通过，Chaquopy 接受了手机导出的两个 Rust 轮子和三个第一方轮子，debug APK 约 36 MB。2026-09-02 真机（Android，Python 3.14.0 / SQLite 3.50.4）安装运行成功：状态「运行中」、健康检查 OK、控制台登录成功。Chaquopy 内置 SQLite 没有 FTS5（状态页显示 false），自动回退全表扫描 |

## 为什么可行

- 后端只用 SQLite（FTS5 缺失时已有回退），没有 Postgres、Redis、向量库。
- 模型调用全部通过 httpx 打远端 OpenAI 兼容接口，手机上不需要推理。
- Web UI 是 Vite 静态包（约 1.8 MB），由 FastAPI 挂载，浏览器直接打开即可。
- 安卓的回环地址是全机共享的，其他 App 连 `127.0.0.1:2026` 就是连本 App。

## 唯一的硬坎

`pydantic-core` 和 `rpds-py`（jsonschema 的依赖，mcp 需要）是 Rust 写的。
截至 2026-09，PyPI、Chaquopy 仓库、python-for-android 都没有它们的安卓轮子。
`cryptography` 和 `cffi` 有 Chaquopy 预编译包。
所以这两个包必须自己交叉编译一次，之后就是普通 Gradle 构建。

## 代码组成

| 路径 | 作用 |
| --- | --- |
| `apps/android/app/src/main/python/embedded_stack.py` | 嵌入式入口。`configure()` 设定目录与环境变量，`bootstrap()` 进程内完成两边接线和首次凭据，`EmbeddedStack.serve()` 在一个事件循环里先起 Model Gateway 再起 Memory Gateway。对 Kotlin 暴露 `start/stop/status` 三个返回 JSON 的函数，也能直接 `python -m embedded_stack` 跑 |
| `apps/android/requirements-embedded.txt` | 不含 `uvicorn[standard]` 的完整依赖闭包，版本与根目录 `requirements-runtime.lock` 一致 |
| `apps/android/app/build.gradle.kts` | Chaquopy 17、Python 3.13、arm64；pip 用 `--no-deps --find-links wheels/`；构建时把 `ui/dist` 拷进 assets |
| `GatewayService.kt` | 前台服务（`specialUse` 类型），在工作线程调用 Python `start()`，通知栏显示状态 |
| `MainActivity.kt` | 状态页：启动/停止、打开控制台、复制首次登录令牌、复制模型网关管理密钥、导出诊断包（分享面板）、关闭电池优化、每 5 秒探测 `/health` |
| `ConsoleAssets.kt` | 把 assets/ui 按版本号解压到 `filesDir/ui`，因为 StaticFiles 需要真实文件 |
| `BootReceiver.kt` | 开机自启（用户上次是运行状态时） |
| `scripts/android/build-wheels.sh` | 打三个第一方纯 Python 轮子到 `apps/android/wheels/` |
| `scripts/android/build-rust-wheels.sh` | 交叉编译两个 Rust 轮子 |
| `scripts/android/termux-verify.sh` | 手机上的一日验证 |
| `scripts/android/termux-export-wheels.sh` | 把手机上编好的 Rust 轮子导出给 Gradle |
| `scripts/android/build-sqlite-fts5.sh` | 用 NDK 编带 FTS5 的 SQLite 替换 Chaquopy 内置版本 |

数据目录布局（App 内为 `filesDir/memory-platform/`，Termux 默认 `~/memory-platform/`）：

```
memory-gateway/   settings.env  project.json  memory.db  knowledge.db  auth.db  eval/
model-gateway/    config.json  secrets.env  usage.db
credentials/      gateway.txt（首次控制台令牌）  admin.txt（Model Gateway 管理密钥）
```

## 执行顺序

### 第 1 步：Termux 验证（半天到一天，不需要 Android Studio）

在手机 Termux 里 clone 仓库后运行：

```bash
bash scripts/android/termux-verify.sh
```

它会装 Termux 预编译的 cryptography/cffi，用 Rust 在手机上编 pydantic-core 和 rpds-py（预计 10 到 30 分钟），
然后启动整套服务。要看的东西：

- 两个 Rust 包能否编过，这直接回答第 2 步的难度。
- 启动输出里 `fts5` 是否为 true，`sqlite` 版本。
- `cat ~/memory-platform/credentials/gateway.txt` 拿到令牌，手机浏览器打开 `http://127.0.0.1:2026/` 登录。
- 手机上任意支持自定义 OpenAI 端点的聊天 App，填 `http://127.0.0.1:2026/v1`，确认能走通。
- 熄屏后放一会儿，看请求是否还能到远端模型（Termux 需要 `termux-wake-lock`，正式 App 靠前台服务）。

`ui/dist` 不在 git 里，要么从电脑拷过去，要么在 Termux 装 nodejs 后 `npm run build`。

### 第 2 步：拿到两个 Rust 轮子

**捷径（先试这个）**：Termux 已经在手机上把两个包编出来了。在 Termux 里运行
`bash Memory_Platform/scripts/android/termux-export-wheels.sh`，它会把 pip 缓存里的轮子改成
`android_24_arm64_v8a` 标签并通过 Wi-Fi 分享，按它打印的命令在 Mac 上 curl 到 `apps/android/wheels/`。
注意它打印的 Python 小版本，`chaquopy { version }` 必须一致。风险：Termux 编的扩展模块是否与 Chaquopy 的
Python 二进制兼容只能靠 Gradle 构建后真机运行来确认；不兼容就走下面的备选。

**备选：在 Mac 上交叉编译（1 到 3 天）**

在电脑上装 Rust、`cargo-ndk`、`maturin`、Android NDK，下载与 `chaquopy { version }` 一致的 Chaquopy target Python 包，然后：

```bash
ANDROID_NDK_HOME=... CHAQUOPY_TARGET=... bash scripts/android/build-rust-wheels.sh
```

产物是两个 `*-android_24_arm64_v8a.whl`，放进 `apps/android/wheels/`。
如果 maturin 输出的平台标签与 Chaquopy 期望不一致，脚本末尾的 `wheel tags` 一步负责改名。
rpds-py 是 abi3，pydantic-core 要和 Python 小版本精确匹配。

### 第 3 步：构建 App（3 到 5 天含调试）

```bash
bash scripts/android/build-wheels.sh
(cd services/memory-gateway/ui && npm ci && npm run build)
```

然后用 Android Studio 打开 `apps/android/`，连真机构建。首次运行流程：

1. 点「启动服务」，通知栏出现常驻通知，状态页变为「运行中」，健康检查 OK。
2. 点「复制首次登录令牌」，再点「打开控制台」，在登录页粘贴。
3. 在控制台里配置模型连接，要填「模型网关管理密钥」时点状态页的「复制模型网关管理密钥」粘贴。
4. 在控制台创建 chat 令牌，填到手机聊天 App。
5. 点「关闭电池优化」，否则熄屏后 Doze 会掐断对远端模型的请求，记忆抽取会静默失败。
6. 要在手机聊天 App 里用搜索、MCP 等工具：客户端里给 memory-auto 打开「工具」能力，控制台里点该模型胶囊勾选「工具调用 tools」。

## 已知限制

- **前台服务类型**：用了 `specialUse`。`dataSync` 在 Android 15 上每天只允许 6 小时，不适合常驻服务。上 Play 商店需要在审核里说明用途；自用 sideload 没有限制。
- **Doze**：即便是前台服务，未加入电池优化白名单时后台网络仍可能被限制。状态页提供了一键跳转。
- **明文 HTTP**：`network_security_config.xml` 只放开了本 App 自己对 127.0.0.1 的请求。系统浏览器允许 `http://127.0.0.1`；第三方聊天 App 是否允许明文回环取决于它自己。
- **FTS5**：Chaquopy 内置的 SQLite 没有编 FTS5，会让长文知识库初始化失败、记忆关键词索引退化。现在用 `scripts/android/build-sqlite-fts5.sh`（需要 Android NDK，`sdkmanager "ndk;<版本>"` 安装）编一份同版本（3.50.4）、带 FTS5/RTREE/JSON 的 `libsqlite3_python.so` 放在 `apps/android/native/arm64-v8a/`，Gradle 任务 `patch<Variant>SqliteFts5` 在 Chaquopy 生成 jniLibs 之后、AGP 合并之前把它覆盖进去。已核对导出符号是 Chaquopy 原库的超集且覆盖 `_sqlite3` 模块全部 86 个导入。该 .so 不进 git，换机器重跑脚本即可。
- **模拟器**：只配置了 arm64。要跑 x86_64 模拟器需要再交叉编译一份 x86_64 的 Rust 轮子并加进 `abiFilters`。
- **体积**：Python 运行时加依赖预计 APK 30 到 50 MB。
- **凭据**：首次控制台令牌和管理密钥以 0600 文件存在 App 私有目录，App 通过剪贴板交给用户；复制后剪贴板内容标记为敏感。
- **不复用 CLI**：`memgw`/`modelgw` 里大量 Windows 与进程管理逻辑，且依赖子进程，安卓上不可用。嵌入式入口独立实现接线，后续若 `apply_stack_install` 的接线规则变化需要同步。

## 发布签名

正式包用 `./gradlew assembleRelease` 构建，签名配置读取 `apps/android/keystore.properties`（已 gitignore），它指向仓库外的密钥库 `~/.memory-platform/android-release.keystore`。**这个密钥库和 properties 文件务必备份**：安卓只允许同一签名的包覆盖升级，丢了密钥库就只能让用户卸载重装（本机记忆随之丢失）。debug 包与 release 包签名不同，从 debug 包切到 release 包也必须先卸载；切换前先在控制台导出备份。产物文件名为 `memory-platform-android-<版本>-release.apk`，发布到 GitHub Releases 的预发布标签（`android-v*`，不触发 Docker 镜像流水线）。

## 诊断包

状态页「导出诊断包」生成一个 zip 并弹出系统分享面板（可发给自己、存到云盘或传到电脑）。内容：

- `runtime.json`、`logs/stack.log*`（服务日志，2 MB 轮转 3 份）、`logs/logcat.txt`（App 进程日志）
- `config/settings.env` 与 `config/model-gateway.config.json`，所有密钥类字段已替换为 `<redacted>`
- `db/memory.db` 一致性快照，含 memories、memory_decision_logs、chat_finalize_jobs、conversation_branch_nodes、core_memory_sections
- `reports/summary.json`（按状态/决策/任务状态计数）、`reports/decision_logs.jsonl`（最近 1000 条抽取决策及原因）、`reports/finalize_jobs.json`（未完成的落库任务，即尚未保存的记忆）、`reports/recent_conversations.json`（最近 50 段对话摘要与近几轮原文）

明确排除：auth.db、model-gateway/secrets.env、knowledge.db。定位「某条记忆没存下来」看 decision_logs 里对应 conversation 的 decision/reason，再看 finalize_jobs 里有没有 pending/failed。

## 后续可以补的

- 把嵌入式入口的 bootstrap 逻辑抽到 `app.stack_install` 里，与 Docker 初始化共用一份规则。
- CI 里用 `--bootstrap-only` 跑一遍嵌入式入口，防止两边接线规则漂移。
