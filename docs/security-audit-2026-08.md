# Memory Platform 全面安全、可靠性与可用性审查

审查日期：2026-08-10  
目标版本：0.2.0  
部署边界：单用户或可信家庭局域网；公共网络入侵概率低，但仍按入口服务失陷后的纵深防御设计。

> 本报告不记录任何 API key、访问 token、用户记忆正文或知识文档。真实模型评测只使用合成数据；费用、兼容性和最终 split 部署验收结果已记录在报告末尾。

## 结论摘要

审查开始时，项目“能启动”但不适合直接作为可靠的长期记忆平台使用。最严重的问题不是复杂的公网攻击链，而是中央 Model Gateway 迁移未完成：状态页声称后台模型可用，透明聊天、Knowledge Agent 和 Embedding 却仍读取另一套旧目录，导致核心链路在正式 Docker 部署中不可用。除此之外，共享访问 key、非原子渠道配置、选择导出越界、并发写入竞争、restore 半提交、10K 召回截断、不可恢复升级和单容器同 UID secret 横向暴露，都会在家庭 LAN 的现实使用中造成数据泄漏、数据丢失或“看似就绪、实际不可用”。

0.2.0 的整改方向是保持单机 SQLite 和清晰的静态 route，同时消除双重事实源和不必要的企业组件。Memory 与 Model 被拆为两个最小权限容器；模型调用统一经过中央运行时；访问权限改为按设备/用途 token；配置、备份、restore、删除和并发写入均获得可验证的事务边界。

## 风险模型与排序方法

- **P0**：核心功能被阻断，或状态/恢复契约会让用户在不知情时运行于错误状态。
- **P1**：在当前单用户 LAN 中仍具有现实概率，可能泄露凭据/隐私、破坏数据或造成不可审计费用。
- **P2**：低概率边界、长期维护成本、性能或专业用户体验问题。
- **不采用**：对当前拓扑收益明显小于复杂度、运维和新攻击面的设计。

LAN 并不等于“无需安全”。本项目更现实的风险依次是：误配置导致出站泄钥；聊天客户端拿到管理能力；备份/升级不可恢复；供应链漂移；入口进程失陷后横向读取 provider/admin secret；最后才是未经认证的公网直接攻击。

## 确认问题、风险与整改成本

下表中的“概率”按当前单用户、可信家庭 LAN 评估，不套用公网多租户系统的威胁模型。“实现中”表示代码与定向测试已经完成，但仍需通过最终 split Docker 与真实中央链路验收；它不是已部署状态。

| 优先级 | 确认问题与证据边界 | LAN 概率 | 最坏影响 | 整改成本 | 当前状态 |
|---|---|---:|---|---:|---|
| P0 | 官方配置写入旧 catalog，而透明 chat、Knowledge、Embedding 读取新 catalog；状态接口却报告中央可用 | 高 | 核心链路 500/静默退化，用户误以为记忆和知识在工作 | 高 | 已修复并部署：唯一 resolver、中央 fail-closed、八 route readiness 与真实 PAYG canary 全通过 |
| P0 | `/health` 只校验进程/schema，零 provider/route 也“健康” | 高 | 升级或首次安装显示成功但不可用 | 中 | `/health` 仅 liveness；Memory `/readyz` 校验三库、Model、backend 权限与 embedding 契约 |
| P1 | 所有聊天设备与 Console 共用 `GATEWAY_API_KEY` | 中 | 任一设备泄漏即可导出、修改、永久删除全部数据 | 高 | chat/MCP/Console scoped token、单独撤销与 legacy 迁移已实现 |
| P1 | FastAPI 依赖鉴权发生在 restore 大正文解析后 | 中 | 未认证请求耗尽 CPU/内存/磁盘临时空间 | 中 | Early ASGI auth 在读取首个 body chunk 前拒绝；按 route 限制正文 |
| P1 | “导出所选”先下载全库，再只在浏览器过滤 memories | 中 | 被选文件仍携带 Core、回收站与扩展分区隐私 | 中 | 服务端 selection export、1000 ID 上限、用户隔离与引用净化已实现 |
| P1 | Memory restore 逐条提交；SQLite replace 未处理 WAL/SHM | 中 | 中断后半恢复；旧 WAL 在恢复后重放并污染数据 | 高 | prepare→单事务、dry-run、immutable 校验、sidecar checkpoint/清理与 rollback journal 已实现；容器恢复演练中 |
| P1 | ingest 查后写竞态、Core 可有多个 active、PATCH 丢失更新 | 中 | 重复记忆、核心画像分叉、用户修改被覆盖 | 高 | `BEGIN IMMEDIATE` 最终重查、持久 claim、revision/ETag/CAS 与唯一 active 索引已实现 |
| P1 | 召回候选硬截断 10,000 条 | 中 | 第 10,001 条以后即使精确相关也永久不可见 | 中 | 同快照 keyset 两遍扫描与有界 heap；10,001/50,001 回归已通过 |
| P1 | discovery 在 URL 校验前携带 bearer 请求 HTTP；模型 ID 可注入终端控制字符 | 低至中 | provider key 明文泄漏、终端剪贴板/显示劫持 | 中 | 统一 URL/DNS 边界、HTTPS 默认、私网显式 CIDR、2 MiB/1000/ASCII 模型目录已实现 |
| P1 | client/provider secret ref 或 value 可复用；非 ASCII client key 令鉴权抛 500 | 低 | 本地 admin/backend key 被发给供应商；全体鉴权 DoS | 低 | ref/value 域隔离、高熵 ASCII client token、UTF-8 bytes 常量时间比较已实现 |
| P1 | fallback 只记录最终 target；模糊文本 400 可跨供应商重发正文；无限 `Retry-After` | 中 | 双重费用、提示词跨渠道、永久熔断、账单不可审计 | 高 | 结构化错误分类、有限 breaker、默认 no fallback、逐 attempt 账本已实现 |
| P1 | capability 只作静态图校验，不根据 stream/tools/JSON/多模态请求过滤 | 中 | 工具或结构化任务被路由到不兼容模型并静默降级 | 中 | RequestRequirements 出站前过滤并返回稳定 422 |
| P1 | embedding 配置维度未注入请求，却发送“权威”维度 Header；空间名由普通用户任填 | 中 | 同维或异维向量混存，长期召回质量不可逆下降 | 中 | 强制 dimensions、逐向量验长、origin+精确模型+维度自动空间 ID |
| P1 | 新渠道分多次写 connection/secret/deployment/route，刷新后孤儿又不可见 | 高 | 配置“消失”、错 key 先毁旧 key、向导覆盖全部生产 route | 高 | discovery 零落盘、bundle validate/apply、文件锁/revision/CAS/journal、默认 keep 与完整对象管理 |
| P1 | Memory/Model 同容器、同 UID、同卷 | 低但高影响 | Memory 入口任意读/RCE 横向取得 provider/admin key | 高 | 已迁移：双容器、UID 10001/10002、独立 data/secret 卷；Memory 无 provider egress，2030 不发布 |
| P1 | installer 先覆盖 Compose、备份命令 UID/路径错误、失败无 rollback | 中 | “成功备份”没有核心 DB；升级失败导致停机或数据丢失 | 高 | 已修复：停写后备份、digest 固定、离线 migrator；durable commit 前失败回滚，commit 后只 forward-repair |
| P1 | 初装 key 写 daemon log；日志不轮转；Compose env/inspect 留密钥 | 中 | 截图、远程排障、日志备份扩散高权限 key | 中 | 已迁移并轮换：凭据只写宿主 0600 文件，服务 env 无 secret，旧容器日志随精确删除清理 |
| P1 | 只锁 direct Python 依赖、基础镜像/main/latest 浮动、无扫描/签名 | 中 | 同版本重建得到不同依赖；供应链更新成为 LAN 主风险 | 高 | 完整 hash lock、base digest、semver images、SBOM/provenance/Cosign/Trivy；发布流水线待首次 tag 验证 |
| P1 | UI 路由测试未隔离 `KNOWLEDGE_DATABASE_PATH`，直接跑全测可创建/迁移真实默认库 | 高（开发时） | 测试污染用户知识库或错误迁移 | 低 | 全局测试沙箱强制 memory/knowledge/auth/usage/eval 临时路径并清空 provider env |
| P2 | 15 页/6 分区、移动滚动不复位、底栏当前项无操作、危险确认默认聚焦确认 | 高 | 非专业用户迷路或误触不可逆操作 | 中 | 默认简洁模式、移动导航/滚动/复制 fallback、危险默认取消与 closure 预览已实现 |
| P2 | source install 默认 `0.0.0.0`，首次密钥在终端回显 | 中 | 用户无意扩大 LAN 暴露；终端历史/录屏泄密 | 低 | 默认回环、显式 LAN opt-in、首次 Console/admin 只写私有 credential 文件 |
| P2 | usage DB 永久增长且 Memory/Model 双重套价 | 高（长期） | 磁盘增长、同一调用两套费用互相矛盾 | 中 | Model 成为唯一事实源；90/365 天 retention、显式 prune/vacuum、Memory 受限代理 |
| P2 | PDF 解析与 Web 进程同资源边界 | 低至中 | 异常/恶意文件拖死整个服务 | 中 | 独立 worker、wall/CPU/RSS/页数/字符/并发上限；不引入 OCR |

## 原有正向控制

- 当前 Docker 公共端口默认只绑定 `127.0.0.1:2026`，Model 2030 未直接发布。
- 自动生成的 Gateway、backend、admin key 原本彼此独立且熵足够；配置目录主要为 0700、secret 文件为 0600。
- SQLite 适合单机数据规模，已有 WAL、foreign key 和 `quick_check` 基础；没有必要换成 PostgreSQL。
- OpenAI-compatible 代理保留原始 JSON/SSE 字节，避免全量重序列化带来的兼容损失。
- 测试规模较大，审查基线后两个后端与前端原有测试均可通过；问题主要在跨服务和真实部署盲区。

## P0：中央运行时与真实 readiness

### 原始问题

Memory 内部抽取可使用 `MODEL_GATEWAY_*`，但透明 `/v1`、Knowledge Agent 和中央 Embedding 分别继续读取旧 provider catalog 或直接 `EMBEDDING_*`。官方 installer/CLI 写入的又是另一组路径。中央配置开启时，实际对象可为空，而 `/providers/status` 和 `/knowledge/status` 仍报告 Model Gateway 可用。

### 整改

- 新增唯一 `app.llm.runtime` resolver。中央配置完整时，八条稳定 route 必须同时有效；任何缺失都 fail-closed，绝不静默回退 direct provider。
- 透明聊天保留未知 JSON 字段、工具、多模态和原始 SSE；每次响应校验 route、deployment、connection、channel operator、model author 和 upstream model。
- 多轮工具调用使用严格 deployment affinity；失效返回稳定 409，原请求不重发、不得清理私有 reasoning 后静默改投其他 deployment 或渠道。
- Knowledge fast/pro 在 phase 内锁定首次 deployment。
- Embedding 使用中央 backend key，强制配置维度，并校验 space、header 和每个实际向量长度。
- `/health` 只表示进程存活；Memory `/readyz` 核对 SQLite、Model `/readyz`、backend token 可见的八条 route 与 Embedding 契约。状态页复用同一 resolver。

### 验收

- 单元 MockTransport、两个 ASGI 应用跨服务测试、Docker 内部 DNS/HTTP opt-in 和最终合成 canary 全部通过后关闭 P0。

## P1：凭据、鉴权与出站边界

| 原始问题 | LAN 下影响 | 整改 |
|---|---|---|
| 同一 `GATEWAY_API_KEY` 同时授予第三方聊天客户端和 Console 全部管理接口 | 任一手机/客户端泄漏即可导出、修改或永久删除全部记忆；只能全局轮换 | 新增 `chat`、`mcp`、`console` scoped token；服务端只存 secret SHA-256；按设备单独撤销和 last-used；legacy key 仅迁移一版 |
| cursor/review/upload 与访问 key 共用签名材料 | 访问 key 泄漏扩大为状态伪造 | 独立 `GATEWAY_SIGNING_SECRET`，缺失时相关功能 fail-closed |
| 鉴权依赖在 FastAPI body 解析之后 | 未认证 restore 可先消耗/解析完整大正文 | Early ASGI auth 在读取第一个 body chunk 前返回 401/403 |
| 非 ASCII key 触发 `hmac.compare_digest(str,str)` TypeError | 手工改 key 后所有鉴权 500 | scoped token 限定高熵 ASCII；legacy 比较统一 UTF-8 bytes，任何输入都不抛 500 |
| client secret ref/value 可与 provider 共用 | admin/backend key 可能被作为上游 Authorization 发送 | ref 集合和值域都强制不相交；doctor/配置校验阻止 |
| 带 bearer 的 HTTPX 客户端继承 `HTTP(S)_PROXY` | 本地 backend/provider/admin key 可被环境代理收走 | 所有受信出站客户端 `trust_env=false`、不跟随 redirect |
| discover 在 URL 安全校验前向远程 HTTP 发 Authorization | 误输/钓鱼 URL 即明文泄露 provider key | 网络动作统一先验证；远端仅 HTTPS，loopback HTTP 默认允许，私网 HTTP 需显式 CIDR/内部服务名 |
| 模型 ID/错误输入未经净化进入终端或校验响应 | OSC 控制字符；userinfo 中 secret 回显 | discovery 2 MiB/1000 ID/可打印 ASCII；Pydantic 错误仅返回 field/code，不包含 input |

Memory 新 token 默认限制 chat 60 次/分钟且最多 4 并发、MCP 120/分钟且最多 8 并发、Console 120/分钟；不可逆操作另限 10 次/分钟。单进程部署不引入 Redis。

## P1：Model 数据面、fallback 与计费

- 请求体动态推导 stream、tools、parallel tools、多模态、reasoning、JSON object/schema；目标未声明能力时在出站前返回 422。
- route 默认 `fallback_scope=none`、`max_attempts=1`。跨渠道必须专家显式启用；生产八条 route 全部单目标。
- Generic adapter 不再用自由文本模糊匹配 400。只有请求尚未发出的连接错误及明确 408/429/5xx 才能进入受限 fallback；401/402、redirect、404、model-not-found、read/write timeout 与 2xx 空流都不向下一目标重发。
- `Retry-After` 必须 finite 且有上限；401/402/429、model-not-found 和连续 5xx 使用 connection/deployment 级进程内 breaker，每个 attempt 前重新检查。
- 新增 `attempt_events`：每个真实 HTTP 请求记录目标、状态、延迟、usage、价格快照、已知费用、未知计费风险和 retry 决策，绝不存请求/响应正文。逻辑 `usage_events` 保留兼容。
- Embedding 采用 input-only 定价；缺少 output token 不再被误判为 incomplete。
- Model 是供应商、attempt 与费用的唯一事实源。Memory 只发送 operation、correlation ID 和不可逆 opaque user tag，用量页读取受限 Model 汇总，不再重复套价。
- 原始事件保留 90 天、日聚合保留 365 天；prune/vacuum 只能显式执行。

## P1：数据一致性与恢复

### Memory/Core 并发

- 远程 extract/embed 在事务外运行，最终匹配重查、create、temporal invalidation 在同一 `BEGIN IMMEDIATE` 中完成。
- 持久化 chat side-effect claim 替代仅进程内 cache，跨 worker/retry 仍幂等。
- Memory/Core 增加 revision；UI 始终发送 `expected_revision`，冲突为 409。迁移时合并重复 active Core，再建立每 user/section 唯一 partial index。

### Restore、召回、导出与删除

- Memory restore 改为完整 prepare 后单事务写入，意外错误全回滚，并提供 `dry_run`。
- 删除 10K candidate 截断；同一 SQLite 快照下 keyset 两遍扫描并维护有界 top-k heap，覆盖 10,001 与 50,001 条低重要度精确/语义命中。
- `POST /memories/export/selection` 最多 1000 ID，仅返回当前用户所选 memory 和必要空间元数据；删除未选引用，其他分区为空；缺失/跨用户 ID 返回 409。
- 批量永久删除改为 preview → commit：预览返回真实 evidence 闭包、Core 影响、指纹和短期签名 token；commit 在单事务内重验，任何漂移 409，且只记录一次审计。
- PDF 在独立进程解析：30 秒 wall、20 秒 CPU、512 MiB、1000 页、1000 万字符、并发 1；不增加 OCR。

## P1：部署、备份与供应链

### 原始问题

- 单容器两个进程和两个 0700 目录都使用 UID 10001；Memory 入口若出现任意文件读/RCE，可直接读取 provider/admin key。
- 初装 key 永久写 Docker 日志；自定义 key 进入 Compose environment；日志无轮转。
- installer 先覆盖 live Compose 再备份，失败无可靠 rollback；旧 `docker exec` 用户与动态路径错误可产生“成功但没有核心 DB”的备份。
- `main/latest`、可变基础镜像与未锁 transitives 使构建不可复现，且无 SBOM/签名/自动扫描。

### 整改

- Memory UID 10001 只挂 memory-data/memory-secrets；Model UID 10002 只挂 model-data/model-secrets。2030 只在 internal backend；只有 Model 拥有 provider egress。
- 两个长期容器均非 root、只读 rootfs、独立 tmpfs、`cap_drop: ALL`、`no-new-privileges`。Model secret 卷仅 UID 10002 可写以支持原子 key rotation；Memory 无法持久挂载或读取 Model secret。管理 UI 代理渠道配置时，provider/admin secret 会短暂经过 Memory 进程内存，因此这里是持久隔离而不是绝对不可见。
- 离线 root initializer/migrator 是唯一一次看见四卷的进程，network none，完成后退出。凭据只写宿主 0600 文件，不打印值。
- portable backup v2 必需 memory/knowledge/auth DB 和脱敏 Model config，usage 明确 present/absent；归档发布前重新校验 hash、SQLite、schema 和 `secrets_included=false`。restore 预检磁盘并使用 journal/rollback。
- 安装器从入口即持有排他锁，按运行中容器标签确定唯一旧 project，并在停机前生成 typed `noop|repair|upgrade` 计划与旧服务 readiness 事实基线；`noop`/`repair` 不创建全量事务。`upgrade` 保存原始 Compose、精确 image ID 与 `.env` 字节快照，停止旧栈后生成并权威复验一致性备份；候选不发布宿主端口，必须通过 `/health` 及不低于旧事实的 `/readyz` 验收后才持久标记 committed。commit 后才发布 2026，之后不再用旧备份反向覆盖可能已接受的新写入；任一更早失败或中断则按 journal 幂等恢复旧 Compose、镜像、环境和数据。
- Python 使用完整带 hash 的 runtime lock 和非 editable wheel；Node/Python 基础镜像固定 patch+digest。三个 release image 使用 semver、SBOM、provenance 与 keyless 签名；扫描阻断所有已有修复的 HIGH/CRITICAL。未修复项会完整报告，发布操作规范要求人工 VEX/可达性复核，但当前流水线尚未把该人工审批做成强制 gate，不能把“无修复版本”误写成零风险。

## UX 与普通用户适用性

原 UI 同时暴露 15 页/6 分区，首次配置要求理解 connection、deployment、作者、能力、向量空间和 route，对非专业用户认知负担过高。新默认只展示工作室、记忆、知识、模型、备份和接入；诊断、价格研究和底层路由进入本机保存的专家模式，旧深链接保持兼容。

渠道向导改为：安全 URL 与 `/models` 零落盘发现 → 从结果选择模型 → bundle validate → 一次 apply。已有 route 默认 `keep`，replace 必须明确选择；Embedding 替换显示维度、空间和重索引影响。失败、关闭或刷新不留下 connection/secret/deployment 孤儿。

同时修复移动路由滚动复位、重复点击当前底栏、LAN HTTP clipboard fallback、批量选择导出、危险按钮默认焦点和永久删除的 closure 预览。

## 明确不采用的过度设计

以下能力对当前单用户 LAN 的收益不足以覆盖复杂度和新故障面：

- OAuth/OIDC、企业多租户 RBAC；
- PostgreSQL、Redis、分布式锁/事务；
- Kubernetes、service mesh、WAF、SIEM；
- Vault 或外部 secret 服务；
- 外部向量数据库或 ANN 集群；
- 自动供应商健康推理、启动自动重嵌入；
- 将整个网关替换为 LiteLLM/Bifrost。

保留 LiteLLM/Bifrost 值得借鉴的机制：同 provider retry 与跨 provider fallback 分层、逐 attempt 账本、按连接预算/并发限制。价格网页抓取+LLM 研究继续只作为 expert/experimental 工具，日常配置优先人工确认的官方价格快照。

## 最终测试与实测结果

### 离线与浏览器回归

| 层级 | 最终结果 | 覆盖边界 |
|---|---:|---|
| Memory Gateway | **1087 passed，1 skipped** | 中央协议、鉴权、并发/CAS、50K 召回、restore、只读 WAL 备份、容量不足、PDF、导出、purge、安装/迁移故障 |
| Model Gateway | **258 passed** | adapter/capability、URL/secret、attempt ledger、breaker、配置事务、容量不足、retention/readiness |
| Console Vitest | 22 passed | scoped token、渠道 bundle、危险操作、中央 usage、移动选择 |
| Console production build | 通过 | TypeScript 与 Vite 生产构建 |
| Playwright | 3/3 projects | 1440×900、390×844、375×667；首次接入、简洁/专家模式、滚动、clipboard fallback、导出和 purge |
| 依赖 | `pip check` 通过；`npm audit` 0 | Python 运行依赖一致性与前端已知漏洞 |

后端测试使用全局临时 memory、knowledge、auth、usage、eval 路径，并清空所有 provider 环境变量；前端构建和浏览器测试也在合成 API 边界内。真实模型调用只读取三枚 provider key 的安全 secret store，不读取真实记忆或知识正文。

### 三渠道兼容矩阵

| 渠道 | 发现与基础协议 | 工具/思考限制 | 最终用途 |
|---|---|---|---|
| Kimi Code Allegretto | `kimi-for-coding`、`k3-256k` 精确发现；非流、流、JSON 均通过；6 次串行人工冒烟 | high thinking + 指定函数返回 provider 400；生产只能按官方能力使用 `auto`/`none`，必须转发真实 User-Agent | `interactive_only`，不进入任何 memory/knowledge route |
| 阿里云百炼按量 | Qwen Plus/Flash、DeepSeek V4 Pro/Flash 精确发现；四者非流、流、JSON、归因通过 | high thinking + 指定函数四者均 400；新 `dashscope_openai`/`dashscope_deepseek_v4` 使用 `enable_thinking` 与 `tool_choice=auto` | 全部后台 route、评测与 Embedding |
| 阿里云 Token Plan Lite | `qwen3.7-plus` 精确发现；6 次串行冒烟通过非流、流、JSON、归因 | 指定函数 + high thinking 返回 400；个人版不允许后台/自动化 | `interactive_only`，无自动 fallback |

Kimi 与 Token Plan 的订阅额度没有用于批量质量评测，增量费用为 0；它们之间也没有自动故障切换。该边界遵循 [Kimi Code 模型说明](https://www.kimi.com/code/docs/en/kimi-code/models.html) 与 [Token Plan 个人版规则](https://help.aliyun.com/zh/model-studio/token-plan-personal-overview)。

### 四模型质量评测

64 条中英记忆提取结果如下。`critical` 包括错主体、否定反转、把假设或第三人事实保存成用户事实；这是硬门槛，不能用更高 F1 抵消。

| 模型 | F1 | critical | 结构化成功 | p95 | 结论 |
|---|---:|---:|---:|---:|---|
| Qwen 3.7 Plus | 0.679 | 5 | 100% | 12.29s | 不合格 |
| Qwen 3.6 Flash | 0.609 | **0** | 100% | 3.86s | 唯一通过安全门槛 |
| DeepSeek V4 Pro | 0.612 | 3 | 100% | 14.19s | 不合格 |
| DeepSeek V4 Flash | 0.741 | 4 | 100% | 9.85s | F1 最高但错存，不合格 |

24 组 Memory 多轮中，Qwen Flash 的聊天事实正确率为 100%，DeepSeek Flash 为 83.3%；四模型 review 均能被生产解析器 100% 解析。按相同生产 prompt 单独复核 compact 时，DeepSeek Flash/Pro 为 6/6，Qwen Flash/Plus 为 3/6；同分选择更便宜的 DeepSeek Flash。连续八轮分支的真实编排离线回归会恰好触发一次 compact，评测中较低的 route 激活率来自部分场景没有达到 8 轮/6000 字符阈值，不是 route 丢失。

Core 经结构化工具改造后，用 Qwen Flash 复测 6 个分区：解析 6/6、关键错存 0、p95 14.66s，但 section/evidence 仅 4/6；两个失败均为空结果而不是错存。因此它只保留为显式人工整理能力，UI 明示“可能漏项、完成后复核”，不作为自动后台任务。

Knowledge 初测的最佳候选 DeepSeek Flash 为 90%，失败点是无答案时返回弱相关 baseline 以及显式请求注入。改为远程失败安全空结果、显式注入前置拒绝，并强化“只有直接证据才能选择”后，在同 ID/类别分布的固定合成 v2 集复测：8 文档、20 问全部通过；answerable 12/12、无答案 4/4、请求/文档注入 10/10、引用合法 20/20；fast p95 10.66s、deep p95 12.34s。fast 使用 thinking=none，pro 使用 high，全部工具选择为 auto，phase affinity 全部有效。

### Embedding

200 条合成记忆、30 个有答案问题和 10 个无答案问题的独立 1024 维空间比较：

| 检索器 | Recall@5 | MRR | nDCG@5 | p50 | p95 |
|---|---:|---:|---:|---:|---:|
| keyword | 0.367 | 0.350 | 0.354 | 67ms | 80ms |
| `text-embedding-v4` | 0.767 | 0.750 | 0.754 | 375ms | 546ms |
| `qwen3.7-text-embedding` | 0.800 | 0.767 | 0.775 | 478ms | 660ms |

Qwen 3.7 只多找回 1/30，样本不足以证明稳定优势，而 p50/p95 分别慢约 27%/21%；因此选择 `text-embedding-v4`。它虽然未出现在本次 `/models` 返回中，但对精确 ID 的一次最小 probe 返回 200、1024 维、完整归因与 usage，不存在 alias 猜测。两个模型共有的 2/10 无答案误报来自 keyword 通道，换向量模型不能修复；这部分由 Knowledge 的证据门控和空结果策略兜底。

### 最终 route 决策

| route | 单一目标 | 决策 |
|---|---|---|
| `memory.chat` | Qwen 3.6 Flash | 聊天 100%，首 token p95 8.63s，低于 Plus 成本 |
| `memory.extract` | Qwen 3.6 Flash | 唯一 critical=0 的提取模型 |
| `memory.compact` | DeepSeek V4 Flash | 生产 prompt 6/6；与 Pro 同分取低价 |
| `memory.core` | Qwen 3.6 Flash | 仅人工触发；证据校验 fail-closed，仍有 2/6 漏项 |
| `memory.review` | Qwen 3.6 Flash | 与 DeepSeek Pro 同为 5/6、解析 100%，按低价选择；仍保留 preview+确认 |
| `knowledge.fast` | DeepSeek V4 Flash | v2 复测 20/20，thinking=none |
| `knowledge.pro` | DeepSeek V4 Flash | v2 复测 20/20，thinking=high、phase affinity |
| `memory.embedding` | `text-embedding-v4` / 1024 | 质量接近而更快；独立稳定空间 |

上述八条生产 route 已在真实 split 部署中生效，均为一个 target、`max_attempts=1`、`fallback_scope=none`，并通过 `/readyz`、归因 Header 与合成 canary。未胜出的 DeepSeek Pro 未进入 live；Qwen Plus 与另一 embedding 也没有作为后台备用。高价模型没有达到“比合格 Flash 至少高 3 个百分点”的生产门槛。

DeepSeek 目标固定为官方当前稳定快照 `deepseek-v4-flash-0731`，不跟随浮动 alias。华北 2（北京）官方原价快照为每百万 token 输入 ¥1、缓存命中输入 ¥0.2、输出 ¥2；配置记录来源和核对日期，促销价不作为长期预算依据。[阿里云 DeepSeek V4 Flash 官方说明](https://help.aliyun.com/zh/model-studio/deepseek-v4-flash)

### 真实 split 迁移与运行验收

2026-08-10 已完成旧单容器、单卷部署到四卷双容器栈的真实迁移。最终固定镜像为：

- init：`sha256:ad919165341dbc0657804400ca3420dcb20d9391ee376c7f5ad94812738f9103`
- Model：`sha256:80c49cb6748f70243c258eaf0bd1975f964830d700bc6ada16b0744810a006cf`
- Memory：`sha256:d5d0be0d32da0f008f8016532f844822da99daf6971cdce3933e1281bb1a57e4`

验收结果：

- Memory 以 UID/GID `10001:10001` 运行，只连接 internal backend，不能访问公网，也不挂载 Model secret；Model 以 `10002:10002` 运行，连接 backend 与 provider-egress，只发布 `127.0.0.1:2026` 的固定 relay，2030 不发布。两个长期容器均只读 rootfs、`cap_drop: ALL`、`no-new-privileges`。
- `/health` 与 `/readyz` 均为 200；Model `doctor` 为 0 error、0 warning。Providers/Knowledge 状态与 resolver 一致，八条 route 全部 `usable=true`；三份业务 SQLite、Auth 与 usage 均 `quick_check=ok`。
- 新旧 Memory/Knowledge 所有业务表行数一致。真实 canary 覆盖非流、SSE、tools、JSON、extract、compact、Knowledge fast/pro、1024 维 embedding、完整 attribution 与 usage correlation；没有读取或发送真实记忆/知识正文。
- Console token 只能访问管理 REST；chat token 只能访问 `/v1`；MCP token 只能访问 `/mcp`。正向请求均 200，六项跨 scope 负向请求均 403。legacy Gateway key 与旧 admin key 在删除旧锚点前已盲验为 401，新凭据为 200；长期凭据只留在 `/Users/spark/memory-platform/credentials/` 的 `0600` 文件中。
- 停写时点的迁移前备份为 `/Users/spark/memory-platform/backups/pre-upgrade-ea8d0b4b0fcf21fd.zip`；迁移后备份为 `/Users/spark/memory-platform/backups/post-cutover-ea8d0b4b0fcf21fd.zip`。两者均为 v2、`secrets_included=false`，迁移后包的 9 个组件通过 size/SHA-256、ZIP CRC、SQLite identity/schema 与重新打开校验。备份仍含明文记忆/知识，应按敏感文件保存。
- Finalize 首次发现只读停止态 WAL 缺少可写 SHM 时无法备份；现改为在备份卷创建字节稳定快照并在那里恢复 WAL，源库字节与元数据不变。第二个收尾缺陷是私有 rollback tree 未接受合法的嵌套 `credentials/`；已改为限定 owner/mode/硬链接数的单层安全删除。相关备份/迁移定向测试 32/32 通过。
- 所有门禁通过后才精确删除旧单容器与 `memory-platform_memory-platform-data` 旧卷；迁移 journal 已标记 `finalized`。没有使用 `down -v`、prune、浮动 tag 或模糊卷名。
- `key.md` 已删除，根 `.gitignore` 与 `.dockerignore` 都保留精确 `/key.md` 规则，并额外忽略 runtime credentials/settings/secrets。以当前 9 个真实 secret 值作只输出布尔结果的 canary 扫描：Git diff、整个工作区、安装 `.env`/Compose、容器 inspect/log、两份最终备份和评测产物均为 0 命中。所有评测脚本、fixture、checkpoint、临时 Gateway Home/数据库、Docker 测试对象、失败批次 harness 与冗余备份已精确清理；只保留上述一份迁移前和一份迁移后有效备份。

最终 Trivy 0.70.0 对三个 exact runtime image 的结果一致：每个镜像 6 个 Critical、18 个 High，均来自 Debian OS 且没有 `fixedVersion`；Python 包 High/Critical 为 0，有修复版本的 High/Critical 为 0。它们不是“零漏洞”，按下述 P2 作为未修复上游项透明接受；正式发布仍应附 VEX/可达性复核。

### 费用与评测可信边界

- 审查前已发生费用：¥1.571131。
- 主评测账本已知费用：¥4.651581；不完整 cached pricing 和 provider 400 按最坏再预留 ¥0.1264612。
- Core/Knowledge 修复后定向复测：账本已知 ¥0.0843316；22 个价格不完整成功 attempt 预留 ¥0.44；三次 harness 失败按最坏预留 ¥1.20。
- 最终真实切换的 9 个短合成 canary：Model 事实账本 ¥0.009124。
- **累计保守费用：¥8.0826288**，未达到 ¥15 软停止或 ¥20 硬停止。

正常完成或发生可捕获错误的每次实际 HTTP attempt 都由隔离 Model Gateway 记录；结果不保存 prompt/response。provider 已收到请求、但网关在写账本前被 `SIGKILL` 或掉电的极窄窗口仍可能漏记一个已计费 attempt，作为 P2 接受并由预算预留兜底。初次大评测产物故意不含正文，因而定向复测只能使用同 ID/类别分布而非逐字相同的 synthetic v2 fixture，报告没有把它冒充成同一数据集。Codex 执行环境把官方域映射到 RFC 2544 `198.18/15`；默认 SSRF 边界没有放宽，只有隔离部署中实际解析出的单个地址可由 admin 显式列为 `/32`，HTTPS 仍按原 hostname 校验证书。

## 最终接受风险

以下项目被明确降为 P2 或设计上不采用；它们不构成当前 LAN 部署的 P0/P1：

- Core 模型仍会漏掉约 2/6 合成分区，但所有失败为空结果，未发生错主体/错证据保存；功能仅人工触发且 UI 要求复核。继续提高召回属于模型/prompt 质量迭代，不应以降低证据门控换取。
- Memory 不持久挂载 Model secret，但渠道管理请求中的 admin/provider key 会短暂经过 Memory 进程内存。要消除此瞬时暴露必须让浏览器直连 2030 或增加另一控制面，会扩大 LAN 暴露与复杂度；当前以 HTTPS 上游、无日志/持久化、低频配置接受。
- Docker Desktop 无法同时让 Memory 只连接 `internal` 网络并直接发布宿主端口。为保持两容器边界，宿主 2026 由 Model 容器内一个固定目标、纯字节转发的最小 relay 接收，再转给 internal Memory；它不解析 HTTP、不记录正文或 Header，并受 128 连接、64 KiB 缓冲和超时限制，2030 仍不发布。这样 Memory 确实没有 provider egress，但入口 relay 与 provider secret 处于同一容器；相较增加第三个无 secret sidecar，这是当前两容器约束下接受的 P2 权衡。可信 LAN 中 128 个长期空闲 TCP 连接仍可在 30 分钟 idle timeout 内暂时占满入口，后续可增加握手截止时间或按源速率门禁。
  - **2026-08 复审更新**：综合评审（JJC-20260812-003）判定该 relay 收益近零（Memory 本就必须能调用 Model Gateway，被攻破后仍可经其外传数据）且引入静默限流故障，已删除。现由 memory-gateway 挂非 internal `ingress` 网络直接发布宿主 2026，接受 Memory 具备出站 egress 的微小残余风险；model-gateway 仍不发布任何宿主端口。本节其余描述保留为审计时点记录。
- 无修复版本的基础 OS HIGH/CRITICAL 扫描项不会被悄悄忽略：流水线完整报告，有修复版本的 HIGH/CRITICAL 一律阻断；但未修复项的人工 VEX 审批目前仍是流程要求而非 CI 强制 gate。最终镜像报告必须披露实际数量，不能写成“零漏洞”。
- Model pricing 快照没有覆盖本次 Qwen cached-input tier 时，账本明确标记 incomplete/unknown，不把部分估价当完整账单；本报告按普通输入价保守计费。正式更新官方价格快照后可消除该提示。
- 原生 Windows 的源码 CLI 仍依赖 Python `chmod`，不具备 PowerShell Docker installer 的显式 DACL；当前目标部署为 macOS/Linux Docker。若未来支持原生 Windows 服务，应补 ACL 后再宣称同等 secret 权限。
- 极低概率硬中断若发生在 restore journal 创建前，可能遗留精确前缀的私有 staging 目录，但不会切换 live 数据；后续可安全按前缀清理。journal 创建后的中断会 fail-closed 并支持幂等恢复。
- portable backup 分别取得 Memory、Knowledge、Auth 和 usage 的一致 SQLite 快照，但不是四库跨文件的同一全局时间点。当前单进程写入与停写迁移备份足以保证恢复；如果未来引入跨库强事务语义，应增加统一写屏障，而不是误把多个 SQLite backup 当分布式事务。
- 安装器会校验 release Compose bundle、镜像签名与 registry digest，但用户最先下载并执行的 bootstrap shell/PowerShell 本身仍依赖 HTTPS 与固定 release URL；本次工作区迁移使用的是本机 exact image，并没有正式 release 的 Cosign 身份。两者均已固定 hash/digest并经过本机验收，但不能冒充已签名发布产物。
- Windows 安装器经过 PowerShell 7.4 parser 和 Linux 容器中的动态 journal/fault 回归，没有在真实 NTFS/Docker Desktop Windows 上做最终灾难恢复演练；Windows 正式发布前仍需在 NTFS 验证 DACL、FileShare 锁和掉电恢复。
- 从便携备份迁移到新设备会保留 token hash，不会自动显示已有 Console token；这是避免备份泄密的刻意取舍，新设备需在主机 CLI 创建新的 Console token。
- 对 schema v1 的多目标 route 读取兼容仍保留原 `any_channel` 语义，避免升级时静默改变已有用户策略；doctor/Console 会提示，迁移候选配置会显式规范化为 v2、单目标、`fallback_scope=none`，真实部署验收结果另行记录。
- Model 的 URL 校验先解析并验证 DNS，再由 HTTPX 建立连接；极低概率 DNS rebinding 仍可能发生在这两个动作之间。生产默认 HTTPS、redirect 禁止、私网目标需显式 allowlist，当前单机 LAN 接受这一 P2；若未来开放公网多租户，应改为固定已验证 IP 的连接器并校验证书主机名。
- portable backup 的 `secrets_included=false` 对所有受支持设置键成立，但未知自定义环境变量仍依赖名称启发式和 URL 凭据扫描；例如自定义的非 URL opaque secret 可能无法识别。当前不支持把任意自定义 secret 塞进 settings，迁移前应先清理；后续可改成配置 schema allowlist。
- Console token 为了刷新后仍能使用而保存在当前浏览器的 `localStorage`；同源 XSS 或恶意浏览器扩展可读取，admin key 则只保存在组件内存。当前前端不加载第三方脚本、入口只在可信 LAN，接受为 P2；不同设备应使用独立 token，浏览器不与不可信用户共用，丢失时单独撤销。
- 未鉴权 `/readyz` 会对三份 SQLite 执行 `quick_check` 并查询 Model 控制面；可信 LAN 中持续高频请求仍可能制造 I/O/CPU 压力。后续可加几秒级安全结果缓存和 single-flight；它不应取代早期鉴权与业务接口限流。

不引入 OAuth/OIDC、PostgreSQL、Redis、Kubernetes、WAF、SIEM、Vault 或外部向量数据库的结论不变。对单用户家庭 LAN，增加这些组件带来的供应链、恢复和运维风险高于收益。
