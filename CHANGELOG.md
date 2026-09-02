# Changelog

本项目遵循语义化版本。发布前的改动记录在 `Unreleased`。

## Unreleased

### Added

- 安卓 App（`apps/android`）：用 Chaquopy 内嵌 Python 3.14，在手机前台服务里以单进程运行 Model Gateway 与 Memory Gateway（仅监听 `127.0.0.1`），复用现有 Web Console；状态页提供启动/停止、复制首次登录令牌与模型网关管理密钥、复制接入地址、导出诊断包（日志、脱敏配置、memory.db 快照、决策与落库任务报告）、关闭电池优化。自带 FTS5 的 SQLite 替换 Chaquopy 内置版本；pydantic-core 与 rpds-py 由 Termux 在手机上编译后导出。构建、验证与导出脚本见 `scripts/android/`，方案与限制见 `docs/android.md`。
- 嵌入式单进程入口 `embedded_stack.py`：不依赖 `memgw`/`modelgw` CLI 与子进程完成两网关接线、首次凭据与 settings.env 生成；uvicorn 使用纯 asyncio/h11，不再需要 uvloop、httptools、websockets、watchfiles。
- Model Gateway `PATCH /admin/deployments/{id}` 支持部分更新 `capabilities`（仍受路由 `required_capabilities` 约束）；`POST /admin/channels/probe-capabilities` 新增 `connection_id`，用已保存渠道的密钥探测已有模型的能力，不再要求重新输入供应商密钥。
- Console：模型胶囊可点开修改能力并「自动检测能力」；新增模型面板同样支持自动检测，适配 profile 收进「高级」。
- 记忆抽取：`source_quote`/`context_quote` 逐字核对改为格式容错（忽略 markdown 标记、空白、emoji、全半角），`context_quote` 核对失败不再整条否决而是丢弃引用；新增「肯定确认」规则，用户仅回答「对了/是的」时可把紧邻的助手原话作为事实锚点，关系仍须在可见上下文中有证据；新增 `education` 关系族。
- Console 静态资源缓存头：`index.html` 为 `no-cache`，`assets/` 带 hash 文件为一年 immutable。

- `/v1/models` 新增记忆模式模型别名 `memory-read`（只召回不写入）与 `memory-off`（纯透明代理）：发不出自定义 Header 的客户端（Chatbox、RikkaHub 等）改模型名即可切换模式。所有 `memory-*` 别名都解析到同一聊天 route，只决定记忆模式；优先级为只读 token 限制 > `X-Memory-Mode` > 别名 > `CHAT_GATEWAY_DEFAULT_MEMORY_MODE`。旧别名 `auto`/`default`/`memory-gateway` 继续接受但不列出。客户端需重新同步模型列表。
- 提取前置过滤（`CHAT_GATEWAY_EXTRACTION_PREFILTER`，默认开启）：仅为寒暄致谢、纯提问或纯代码的轮次不再调用 `memory.extract`，也不写 finalize outbox；跳过原因以「本地预过滤：」开头写入决策日志（只记长度与 SHA-256）。明确的「记住/remember」请求与助手提问后的短回答永不跳过。不影响 `memory.compact` 压缩与请求侧召回。
- 敏感句子的本地直存：`ALLOW_SENSITIVE_EGRESS=false` 时，含密码/证件号/账号且紧邻「记住」的句子不再丢弃，而是不经模型、不生成向量地原句保存为 `sensitive` 记忆并归入「私密信息」空间；无「记住」的敏感句子只在决策日志留下哈希（理由「敏感句子未出站且未明确要求记住」）。
- `MEMORY_EGRESS_CEILING`（默认 `private`）：`ALLOW_SENSITIVE_EGRESS=false` 时仍允许出站到提取/embedding 模型的最高本地敏感级别；设为 `normal` 可恢复严格模式（同样按句子过滤）。
- 自动替换旧记忆（`MEMORY_AUTO_SUPERSEDE`，默认开启）：带明确转变标记（换成/改成/现在/不再/取代/switched/now）的新记忆，与某条同类型、同主体、同一可替换属性的活跃旧记忆向量高度相似时，自动把旧记忆关闭为历史（`status=resolved`、`valid_until`、`superseded_by`），不再留一对重复给体检。偏好类（喜欢/常喝）只有带否定冲突才替换；纯否定翻转、事件与反思类仍交给体检。需要已配置 `memory.embedding`。`POST /memories/{id}/temporal/restore` 对这类无键链原地重开旧记忆并解链；软删除链成员会自动解链。REST `/memories/ingest`、`POST /memories` 与 MCP 结果新增 `superseded_memory_id`；决策日志新增 `auto_supersede` / `auto_supersede_undo`；Console 记忆详情的历史版本提供「恢复此版本」。设 `MEMORY_AUTO_SUPERSEDE=false` 可回到仅体检建议模式。

### Changed

- `CHAT_GATEWAY_EXTRACTION_CONTEXT_TURNS` 默认值 2 → 4，几轮问答式确认落在抽取视野内。
- 手机端 Console：文档不再整体滚动而由内容区滚动（修复 fixed 底栏随浏览器工具栏跑位），底栏与顶栏改为近实色；记忆档案与核心记忆历史在手机上为底部抽屉；浮现模式改为下拉；空间管理仅专家模式显示；渠道名显示中文（dashscope → 阿里云百炼），模型胶囊只显示模型名；运行信息折叠、去掉「内部接线」；移除工作室「最近未写入记忆」面板与记忆库遮罩提示；顶栏用户首字母改为设置图标。
- 管理密钥验证后卡片收成一行；模型页移除专家模式提示条。
- 上游本地失败提示区分「域名解析失败」与「连接上游失败」，不再一律报「安全校验失败」；核心记忆整理未写入时显示原因而非「已重新整理」。
- 新建渠道自动创建的 chat token 命名为「聊天 App · 日期」。
- 敏感级别重新分档：健康/医疗与精确住址从 `sensitive` 降为 `private`，与联系方式、收入同档；`sensitive` 只保留密码/密钥、证件号、银行卡/账号。`private` 记忆由提取模型直接保存（importance≥7、confidence≥0.85，不再要求说「记住」），`sensitive` 仍需 importance≥8、confidence≥0.9 且紧邻「记住」。旧库中已标为 `sensitive` 的健康记忆不自动迁移。
- 出站过滤从整条消息改为按句子：一句话里出现敏感词不再丢掉整轮的其余内容；只有级别超过 `MEMORY_EGRESS_CEILING` 的句子被扣留，其余句子照常提取。压缩摘要、体检与知识代理的出站守卫本轮未改，仍是「非 normal 一律不出站」。
- 聊天召回可按相关性注入 `private` 记忆（排序压低，提示词标注「仅在与用户当前问题明确相关时使用」）；`sensitive` 永不注入；核心记忆整理、自然浮现与 REST 默认搜索仍只含 `normal`。检索缓存键第 4 位由布尔改为级别上限字符串。
- 标为 `normal` 的知识文档中仅提及健康/住址词汇的分块现在会与文档其余部分一同 embedding；标为 `private`/`sensitive` 的文档行为不变。
- 体检不再对已被新版本取代的记忆重复提出重复/冲突/过期建议；评测新增 `keyless_supersession_edge_count`，无键自动替换链不再被判为退化。
- `_looks_superseding` 的英文标记改为词边界匹配，`know`/`snow` 不再误判为 `now`。

- 旧单卷（legacy all-in-one）布局迁移从 Docker 一键安装器拆分为独立一次性工具 `deploy/legacy_cutover.py`；`install.sh` / `install.ps1` 检测到 legacy 布局时 fail-closed 并指向该工具，split 布局的 journal 状态机语义不变。删除未使用的 `stack-maintenance` Dockerfile target 与开发 compose 中的对应 service（发布路径 `docker-compose.user.yml` 的 maintenance 服务继续复用 init 镜像）。

## 0.5.1 - 2026-08-13

### Fixed

- Console Chromium 验收的 fake API 补齐 `/memories/health`，新增健康检查不再被误报为未知请求。
- 时间边界缓存测试改为等待实际生效时刻，避免较慢 CI runner 在首次断言前越过 0.2 秒边界。

## 0.5.0 - 2026-08-13

### Changed

- Console 去杂：顶栏已显示页名时不再重复大标题；工作室把情绪/网络/计数收到「探索」；知识空态只留添加文档；模型页无草稿时隐藏校验按钮，替换密钥收入展开项；报告页恢复命令收进高级；向导能力默认收起。

### Fixed

- 启用语义搜索后可补齐缺少当前空间向量的旧记忆：向导保存 embedding 路由后自动 `scan` 重嵌，记忆库对无向量条目给出条幅和「补齐向量」。
- 「用户只喝美式」这类饮食事实不再被「我喜欢喝什么」问句的主语门误排除，文档里的咖啡验收路径可以召回。
- 渠道发现命中 Clash/Surge TUN fake-ip 时，在错误条下方直接提供同一勾选，不必打开高级设置。
- 渠道保存成功后刷新顶栏就绪状态，避免仍显示「待配置模型」。
- 向导不再在已有 chat token 时重复签发；聊天下拉去掉嵌入/语音/图像模型并支持筛选。
- `/favicon.ico` 不再掉进 MCP 鉴权返回 401。
- 工作室与导航不再预取未初始化的召回评测 workbench（避免控制台 404）。
- Console 401 错误不再一律引导「核对 Console token」：按请求路径/错误码区分 admin key、Console token 与供应商密钥，并保留服务端具体说明。
- 工作台在模型未就绪时展示「下一步：配置模型渠道」卡片与三钥说明（gateway.key / admin.key / chat token），与「生成 chat token」接入卡衔接。
- 未配置或访问密钥失效时强制进入「连接设置」并隐藏日常导航（简洁模式也不例外）；密钥有效后连接设置仍只在专家模式侧栏与头像入口。文案明确访问密钥 = `credentials/gateway.txt`（兼容旧版 `gateway.key`）的 Console token。
- Docker 凭据交付改用 `gateway.txt` / `admin.txt`（纯文本，避免 macOS 把 `.key` 当成 Keynote 演示文稿）；读取仍兼容旧的 `*.key`。

### Added

- `POST /memories/stack-backup/validate`：上传便携整栈 zip 做 dry-run 校验（清单哈希、schema、SQLite），返回组件与聚合计数，**不写生产库**。
- Console「报告与备份」支持校验备份 Zip，并展示源码 / Docker 可复制的停服恢复命令。
- 记忆空间工作台：`POST/PATCH/DELETE /memories/spaces`、归档/取消归档；支持 `color`（#RRGGBB）、`description`、`sort_order`；列表可 `include_archived`。记忆库页可展开管理空间。
- 历史对话批量导入：`POST /memories/import/conversations/preview|commit`，支持 OpenAI messages JSON 与 User/Assistant 角色行文本；提交后走同源提取门控。Console「报告与备份」提供预览与确认导入。
- 已配置渠道可「添加模型」：复用已保存密钥列出模型，把新聊天模型追加为同渠道备用，不再要求再贴 Key、也不再新建第二条渠道。
- 渠道向导可单独填写向量接入点：与聊天地址不同时，原子创建一条只跑 embedding 的渠道并复用同一密钥；相同或留空则仍挂在聊天渠道上。

### Changed

- 渠道向导打开时不再预选 DeepSeek；第三步文案改为「检查并保存」。
- 本地 Compose 初始化结束句点名 `credentials/gateway.txt` 与 `credentials/admin.txt`。
- 增加 Docker 卸载说明与 `deploy/uninstall.sh`（只拆当前 project，不 prune）。
- `POST /providers/admin/*` 缺少 admin key 时返回结构化 `code=admin_key_required`；上游 401 映射为 `admin_auth_failed` 并给出可读中文原因。
- 渠道向导预设：去掉 Kimi Code / 阿里云 Token Plan，改为 Anthropic Claude 与 Google Gemini（OpenAI 兼容）；「自定义渠道」不再继承上一预设的地址，改为空白表单。
- Model Gateway 不再因 `token_plan` / `coding_plan` / 套餐域名强制 `interactive_only`；仅显式 `usage_scope=interactive_only` 时 backend 不可用。提供商条款由使用者自行遵守。
- 渠道只读发现失败时透传真实原因（含 fake-ip / 网络策略文案），不再只显示裸 `HTTP 400`；TUN fake-ip 场景明确提示勾选代理选项。
- 渠道向导支持「探测模型能力」：对聊天/流式/tools/推理/json_object 发极短试请求并自动勾选；说明未勾选会被路由视为不支持。

## 0.4.0 - 2026-08-12

### Fixed

- 渠道向导「复制客户端配置」不再误用 Console token：apply 成功后自动创建 chat token，仅复制该设备密钥。
- Model Gateway 上游地址校验失败时返回可读中文原因（含 fake-ip / 198.18 提示），而不再只显示裸 `ConnectError` 类型名。
- 显式 `allowed_private_networks` 现可写入整个 RFC 2544 段 `198.18.0.0/15`（Clash/Surge TUN fake-ip），不再强制只能 `/32`。
- 多意图消息（「新事实 + 提问」混在一条）现按句多路召回取并集，不再因整句语义稀释而漏召相关记忆。
- 检索与聊天注入的记忆激活加上上限（每次最多 5 条头部命中），「被检索曝光就自增激活度」的正反馈回路被消除；聊天召回本身不再计入使用。
- mcp/console token 请求 `memory_access="read"` 不再被静默改写为 read-write，而是返回 422。
- 偏好软路径只在 `source_quote` 内匹配偏好标记，英文标记要求词边界；无关候选不再被误降门槛。
- 提取候选的实体列表清洗复合名称碎片（如「Dark Mode」不再拆出「Dark」），提示词同步约束实体完整性与中性配置偏好的 semantic 归类。
- 整栈备份的 schema 版本常量收敛为单一事实源（`app/schema_versions.py`），恢复时校验支持区间并拒绝未知版本；错误路径不再留下半写文件。
- 聊天 finalize outbox 补齐状态机：`done` 为终态不可回退、stale claim 清理、周期 drainer 兜底重试、完成后清除 payload。
- `POST /providers/live-probe` 尊重 60s 缓存并支持显式 `force`，并发探测去重；该端点不再被误分类为不可逆操作而占用破坏性操作限流预算。

### Added

- Console「报告与备份」支持下载**整栈便携备份**（`POST /memories/stack-backup`）；Docker 拆分部署可附带 `X-Model-Gateway-Admin-Key` 拉取 Model 脱敏配置。
- Model Gateway `GET /admin/portable-config`：admin 导出完整配置 JSON（仍不含 secrets.env）。
- 工作室展示「最近未写入记忆」及原因（来自决策日志 ignore）。
- 第一人称偏好句（如「我喜欢…」）对 semantic 等类型启用略低的 importance 门槛（soft path），假设/敏感门控不变。
- `GET /providers/status?live_probe=true` 与 `POST /providers/live-probe`：探测 memory.chat 上游连通性（约 60s 缓存）；Console「模型与路由」可一键探测。
- chat token 支持 `memory_access=read|read-write`：只读 token 可召回但禁止自动提取写入；接入页可选择。
- 聊天 finalize **outbox**（`chat_finalize_jobs`）：提取意图先落库，进程重启后可恢复未完成的记忆写入。
- 大库（≥2000 条）纯关键词检索改用 SQLite FTS5 索引生成候选再精排，打分与解释字段不变；小库、embedding 查询与单字/类别查询自动回退全表扫描。
- 记忆详情在正文明显偏离原始来源时显示「可能经过编辑，原文仅供追溯」提示。
- 工作台在「模型已就绪但还没有 chat token」时显示接入引导卡片：一键生成 token 并复制三行客户端配置。
- 安装器支持 `MEMORY_IMAGE_REGISTRY` 指定 GHCR 镜像加速站，GitHub 直连失败时给出代理/镜像指引；回滚校验对任意 registry 的 digest 固定引用一视同仁。
- usage 事件保留策略：Memory / Model Gateway 每日删除超过 365 天的用量明细（汇总不受影响）。

### Changed

- Memory / Model Gateway 默认关闭 `/docs`、`/redoc`、`/openapi.json`；开发可用 `MEMGW_ENABLE_OPENAPI=1` 或 `MODEL_GATEWAY_ENABLE_OPENAPI=1` 打开。
- 渠道向导增加「TUN fake-ip 代理」勾选；知识库空态说明导入文档不会自动进入普通聊天；移动底栏主导航改为含「接入信息」。
- 工作室与导航在 legacy 共享密钥仍启用时显示醒目迁移提示。
- Docker 拓扑简化：`memory-gateway` 直接发布宿主端口，Model Gateway 保持私网不发布端口。
- 安装器减负：Sigstore/Cosign 验证改为 `MEMORY_VERIFY_SIGNATURES=1` 显式开启（digest 固定不变）；Compose 拓扑校验移入 CI；升级备份收敛为停旧栈后的单次静默备份并做真实复验（ZIP CRC + SQLite quick_check）；`MEMORY_HOST` 允许任意本机 IPv4；端口被占自动顺延时显式提醒替换文档中的端口。
- README 快速开始改写为三步凭据导览；Windows PowerShell 安装器标注为实验性；文档补充安装目录丢失后的重挂载恢复步骤。
- Console token 展示统一默认掩码、显式「显示 token」切换；开发者页新增关闭 legacy key 的分步指引。

### Removed

- 删除 Model Gateway 前的 ingress TCP relay（`deploy/ingress_relay.py`）及其入口脚本接线；连接路径少一跳，安全边界由网络隔离与鉴权承担（见 `docs/security-audit-2026-08.md` 附录）。

## 0.3.0 - 2026-08-10

### Changed

- Memory Gateway **仅支持 Model Gateway** 作为模型运行时：移除 `UPSTREAM_*` / `LLM_*` direct-provider 聊天、embedding 与 Knowledge Agent 第二实现；未配置中央网关时启动路径与 `/readyz` 失败并给出迁移指引。见 [docs/migrate-to-model-gateway.md](docs/migrate-to-model-gateway.md)。
- `/v1/chat/completions` 使用显式 route 模型名（非 `memory-auto`）时保留并透传客户端自带的 `reasoning_content`；`memory-auto` 仍剥离无法证明来源的旧推理原文。
- 默认测试沙箱与 Console 简洁导航继续以双网关金路径为准；高级 UI 仍可通过专家模式显式开启，API 不删减。
- `Settings` 去掉 direct-provider 死字段（`UPSTREAM_*`、`LLM_*`、本地 model/routes catalog 路径、`pricing_catalog_path`、独立 embedding base/key/model）；`PRICING_CATALOG_PATH` overlay 一并移除，deploy 不再写入；保留中央 `MODEL_GATEWAY_*` 与 `EMBEDDING_DIMENSIONS`。
- `memgw init` / `cli_config` 不再种子化本地 models/routes 目录，导入 settings 时剥离已退役 direct-provider 环境变量；`CliPaths` 仍保留 legacy 路径供 stack backup。
- Knowledge Agent 仅通过 `ModelRuntime` 的 `knowledge.fast` / `knowledge.pro` route 调用中央网关。
- `app/memory/store` 改为 package（对外 `app.memory.store` 契约不变，类方法薄委托）：
  - `schema.py` / `schema_ensure.py` / `migrations.py` — 建表、列补齐、版本迁移
  - `crud.py` / `merge.py` — 读写合并
  - `temporal.py` — 时态链与 time-ripple
  - `export_import.py` — 导出导入恢复
  - `purge_ops.py` / `lifecycle_purge.py` — 永久删除
  - `core_memory.py` / `conversation.py` / `spaces.py` / `digest.py` / `decision_logs.py`
  - `helpers.py` / `constants.py` / `errors.py`
  - `_monolith.py` 收束为编排壳（init/connect/side-effect claim + 委托）
- `/memories` HTTP API 拆为 `app/api/memories/` 包（crud/search/core/conversation/export/graph/review/evaluation/purge/item），URL 不变；`/{memory_id}` 最后注册以免遮蔽静态路径。
- 离线 legacy 迁移不再依赖 `model_catalog`；本地 models/routes 仅可选拷贝，pricing 仍用于历史模型本地账本展示。

### Removed

- 透明聊天与内部任务对本地 `providers`/`model_catalog` 直连 failover 的运行时路径。
- Memory 进程内对直连上游的本地用量记账（中央响应改由 Model Gateway 归因；本地 `app/catalog/pricing.json` 仅作已知历史模型展示兜底）。
- `memgw model` / `route` / `pricing` 与 `memgw secret set/delete mimo|kimi|deepseek|upstream|embedding`：只打印迁移提示并以退出码 2 退出，原实现已删除；模型、路由与价格请改用 `modelgw` 或 Console「模型与路由」。
- `app/model_probe.py`、`app/model_catalog.py`、`app/providers/`、`app/catalog/models.json` / `routes.json` 及其 schema（**保留** `pricing.json` 供 usage 目录展示）。

## 0.2.0 - 2026-08-10

### Added

- Model Gateway 配置 schema v2：请求能力感知路由、显式 fallback scope、Qwen/DeepSeek V4 deployment profile、逐 attempt 账本、进程内 breaker、配置 revision/CAS/crash journal，以及零落盘渠道发现和原子 channel bundle API。
- Memory Gateway 统一中央运行时：透明聊天、八条后台 route、Knowledge Agent 与 Embedding 共用 Model Gateway，并校验完整归因、deployment 亲和、向量空间和维度；新增可操作的 `/readyz`。
- 按设备、用途隔离的 `chat`、`mcp`、`console` token，独立签名密钥、提前鉴权、限流和并发门禁；Console 可一次性创建 chat/MCP token。
- Memory/Core revision 与条件更新、持久化聊天副作用 claim、原子 restore、50K keyset 召回、所选导出，以及永久删除 preview/commit 事务。
- split Docker 栈：Memory UID 10001 与 Model UID 10002 使用独立数据/secret 卷，Model 仅在内部网络提供 2030；新增离线初始化、旧单卷迁移、portable backup v2 和自动回滚。

### Changed

- 默认 UI 收敛为面向普通用户的简洁导航，高级诊断按设备显式开启；渠道向导改为 discover → validate → apply 的单次原子提交，现有 route 默认保留。
- 发布版本进入 0.2.x：三个 runtime/init 镜像使用固定 semver，完整 hash lock、固定基础镜像 digest、SBOM、provenance、签名和 HIGH/CRITICAL 扫描。
- Linux/macOS/Windows 安装器都先用旧版本创建并校验备份，再拉取 digest 固定的新镜像；凭据只写宿主机私有文件，不再从 daemon log 读取。

### Fixed

- 修复中央 Model Gateway 配置存在时透明聊天、Knowledge Agent 和 Embedding 仍读取旧 provider 目录而不可用的问题。
- 修复并发 ingest/Core 更新、restore 半提交、10K 召回截断、批量选择导出越界和危险批量删除确认不足。
- 修复移动端路由滚动、当前底栏重复点击、LAN HTTP clipboard、危险对话框默认焦点及首次渠道配置遗留孤儿。
- 修复 Embedding input-only 费用漏计、可变维请求与声明 header 不一致、Retry-After 无限冷却、gzip 原始字节损坏和失败 fallback 账本缺失。

### Security

- Client/provider secret ref 与值域强制隔离，token 使用高熵 ASCII/UTF-8 bytes 常量时间比较；provider/admin/backend key 不再进入日志、Compose 展开、命令参数或长期容器环境。
- 所有带凭据的出站请求禁用 ambient proxy 和重定向；远端仅 HTTPS，loopback/private HTTP 需显式、受限启用；限制 discovery、请求/响应、上传、解析和内部模型输出大小。
- Memory 与 Model 长期容器互相不能读取对方 secret；rootfs 只读、capabilities 全部移除，provider egress 仅授予 Model。
- 入口 relay 增加首字节截止时间（默认 10 秒）与按源 IP 并发上限（默认 32），长期空闲 TCP 连接不能再占满入口槽位。
- `/readyz` 增加 3 秒结果缓存与 single-flight，未鉴权高频探测不再放大 SQLite quick_check 与 Model 控制面调用。

## 0.1.2 - 2026-08-08

### Fixed

- 容器日志里的「接入信息」现在打印宿主机端口，而不是容器内固定的 2026。用户版 compose 通过新的 `MEMORY_PUBLIC_PORT` 把实际映射端口告诉容器。此前只要端口不是 2026（包括一键脚本在 2026 被占用时自动顺延的情况），日志里给出的就是一份连不上的地址——而文档恰恰引导用户回到日志里找密钥和地址。
- 「接入信息」里的 Model Gateway base URL 补上「内部接线地址，不要填进客户端」标注，与 Web Console 横幅的说法保持一致。

## 0.1.1 - 2026-08-08

### Added

- `deploy/install.sh` 一键安装脚本：检查 Docker、下载用户版 Compose、自动避开已占用端口、等待首启就绪并打印两枚一次性密钥；重复运行即升级到最新镜像。
- 用户版 compose 支持 `MEMORY_PORT` 覆盖宿主机端口映射、`MEMORY_HOST=0.0.0.0` 放开局域网/手机访问，并内置 healthcheck 覆盖首启安装期；一键脚本在开启局域网模式时会直接打印手机可用地址。
- 支持在首次安装时自带密钥：`GATEWAY_API_KEY` 和 `MEMORY_CONSOLE_ADMIN_KEY` 两个环境变量对一键脚本、用户版 compose 和 `scripts/setup.sh` 都生效，留空则维持原来的自动生成。自带值需至少 16 个字符、不含空白、字符不过分重复，一键脚本在拉镜像前先校验一次，容器内 `memgw stack install` 再完整校验；密钥只经进程环境传递，不写入安装目录的 `.env`。

### Changed

- `scripts/bootstrap.sh` 的 Python/Node 缺失报错给出具体安装命令，并在构建 Web Console 前校验 Node.js ≥ 22。
- README 与运维、接入文档补充首启等待预期、Windows 安装路径、admin key 找回和端口冲突处理。
- Web Console「模型与路由」页横幅把 Model Gateway 地址明确标注为内部接线（不用填进客户端），并新增当前来源的客户端接入地址；「连接设置」页新增服务进程管理说明（启停命令、后台运行与开机自启）。
- 安装输出和接入文档明确区分两枚密钥的去向：只有 `GATEWAY_API_KEY` 需要填进客户端（含手机），admin key 留在本机浏览器使用。

### Fixed

- `scripts/setup.sh` 结束时会打印完整接入信息块（Web Console、客户端 Base URL、模型名），此前只打印管理台地址和模型名，缺少客户端最需要的 `/v1` Base URL；同时端口改为读取 `project.json` 的实际值，不再硬编码 2026（`--json` 输出的 `client.*` 同步修正）。
- 通过环境变量传入的 `GATEWAY_API_KEY` 现在会写入 `settings.env`。此前只有自动生成的分支才持久化，导致自带密钥在服务重启后丢失。
- `deploy/install.sh` 端口占用时给出的重试命令不再使用 `$0`。通过 `curl | sh` 运行时 `$0` 为 `sh`，原提示会让用户复制到无法执行的 `sh sh`。
- `deploy/install.sh` 区分「重复安装」与「首装但日志解析失败」：在 `up -d` 之前精确匹配本 compose 项目的数据卷，首装解析失败时提示查看日志，重复安装时给出两枚密钥的重新生成命令。

## 0.1.0 - 2026-08-08

首个公开版本。

### Added

- 根 `scripts/setup.sh` 的完整引导安装、`--install-only` 和 `--configure-only` 模式。
- 不含密钥的 AI quickstart JSON recipe、Schema、渠道预设与 stdin 密钥输入。
- 免费 `/models` 自动发现命令和交互式模型选择。
- AI 安装契约、结构化成功/失败输出与社区贡献、安全模板。
- 根级 `Dockerfile` 与 `deploy/`：单容器双服务一体化镜像、首启自动接线入口脚本、开发/用户 compose 文件，以及 tag 推送 GHCR 的 `docker.yml` workflow（含 PR smoke test）。
- 面向 Chatbox / RikkaHub 用户的 `docs/client-setup.md` 接入指南：三项配置、领 key 指引、常见坑速查表。
- `memgw stack install` 现在自动生成 Model Gateway admin key（`memory-console-admin`）并只打印一次，Web Console 的渠道管理不再需要手工创建 admin 身份。
- Web Console「模型与路由」页新增「新建渠道」分步向导：选预设/自定义渠道 → 单向写入渠道 key → discovery 拉取可见模型 → 选定聊天/向量模型并一键接管八条用途路由；全新安装后无需 CLI 即可完成首次模型配置。
- Model Gateway 管理面新增 `POST /admin/connections` 与 `POST /admin/deployments`（admin key、revision 冲突检测、dry_run、整图校验、原子写入热加载），discovery check 现在返回可见模型 ID 列表。

### Changed

- `modelgw quickstart --json` 现在只在 stdout 输出单个 JSON 对象；子流程日志不再污染机器输出。
- 首次安装在交互终端中自动继续完成模型配置和最终 doctor。
- Web Console「模型与路由」页的 admin key 引导改为优先指向安装时打印的密钥。

### Security

- quickstart recipe 拒绝未知字段和密钥字段。
- 模型发现不跟随重定向，不发送推理，也不写配置。
