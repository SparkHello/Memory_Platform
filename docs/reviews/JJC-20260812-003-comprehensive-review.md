# Memory Platform 综合审查报告（安全缺陷 / 易用性 / 设计合理性 / 过度设计）

- **审查日期**：2026-08-12（Asia/Shanghai）
- **审查对象**：工作区 `main` + 约 1700 行未提交改动（上轮整改产物）
- **方法**：① 用当前源码重建三镜像并原地升级本机 Docker 栈做真实用户实测（含独立项目模拟全新安装）；② 四路并行代码深审（memory-gateway 后端、model-gateway、Web Console、部署与文档）；③ 完整测试门禁；④ 联网对比 Mem0 / Zep / Letta / LiteLLM 等同类项目。按用户要求，本报告不覆盖网络安全（SSRF/TLS/公网暴露），该部分见 `docs/security-audit-2026-08.md`。
- **证据标记**：`[T]` 实机复现，`[C]` 代码确认，`[S]` 疑似需验证。

## 执行摘要

整体判断：核心闭环（自动召回→注入→提取→治理）在实机上工作良好，上轮整改的大方向（scoped token、渠道向导、finalize outbox、只读 token）全部正确。但**本轮未提交改动自身引入了 1 个 P0 和一组 P1**，且集中体现同一个模式：**机制做了一半——持久化写了、恢复语义没闭合；常量复制了、没有单一事实源；缓存声明了、入口绕过了；提示给了、出路没给**。合并前必须先修 P0/P1。

更长期的问题不是功能缺失，而是**复杂度预算错配**：约 6.1 万行后端 + 2.7 万行 UI + 约 5600 行安装体系，服务一个"个人电脑 Docker Desktop 单用户"场景。安全审计已经砍掉了企业组件，但安装链路（ingress relay、cosign、compose 白名单校验）和 pricing 层仍在为不存在的威胁模型与规模付费，而真正影响目标用户的——五种凭据的认知负担、大陆网络下安装必败、备份不可用——恰恰是最该花预算的地方。

## 一、P0：合并前必须修复

### P0-1 整栈备份完全失效（auth schema 版本常量漂移）[T][C]

- `app/auth/tokens.py:22` 将 auth schema 升到 v2（`memory_access` 列），`app/stack_backup.py:69` 的 `_SUPPORTED_AUTH_SCHEMA_VERSION = 1` 未同步，且校验要求严格相等。
- **实测**：当前代码的真实部署上 `POST /memories/stack-backup` 必然 409，错误文案还是恢复语境的"Auth 数据库来自更高版本，拒绝降级恢复"；恢复侧同样被 `_validate_auth_database` 拦死。备份→恢复双向失效，用户只会在灾难恢复那一刻发现。
- 测试没抓到：`tests/test_stack_backup.py:78-87` 夹具手写 `PRAGMA user_version = 1`，与真实 store 脱节（memory 库夹具已同步为 5，唯独 auth 漏了）。
- **附带副作用**[T]：备份流程还会在错误路径 `/data/config/auth.db` 副作用创建 0 字节空库（两套路径解析不一致，`cli_config.py:175` 的 `MEMGW_HOME/auth.db` vs settings 的 `AUTH_DATABASE_PATH`）。
- **修复**：常量改 2（恢复侧建议允许 1..2 区间）；夹具改用真实 `AuthTokenStore.init_db()`；版本常量收敛到单一事实源（见 P2）；修掉错误路径副作用。

## 二、P1：应当尽快修复

### memory-gateway 后端（本轮改动）

1. **outbox 崩溃恢复被持久 claim 阻塞**[T 复现]：进程在 claim 成功后、ingest 完成前崩溃，重启恢复时 `_claim_turn_side_effect` 被崩溃前残留的持久 claim（TTL 1h）挡回，job 滞留 `running`、attempts 白烧（8 次即永久 failed）。outbox 恰好覆盖不了它要解决的核心崩溃窗口。恢复路径应清理该 job 的 stale claim 或按 job_id 绕过。（`app/api/chat_gateway.py:1593-1603, 1653-1714`）
2. **`mark_chat_finalize_job(status="running")` 无条件覆盖 `done`**[T 复现]：重复投递把已完成 job 翻回 running，最终对同一 turn 重复 LLM 提取（烧 token + 近重复记忆）。UPDATE 需加 `WHERE status != 'done'` 或把置 running 移到 claim 成功后。（`app/memory/store/_monolith.py:288-306`）
3. **outbox 无周期 drainer、done 行永不清理**[C]：`retryable=true` 的 job 除非重启永远没人再看（"可重试"=丢弃）；且每轮对话的 user_text+assistant_text 完整原文永久留存 `payload_json`——无界增长 + 隐私面扩大（项目对 usage 明确规定不存正文，outbox 表却整轮保存，敏感过滤只作用于 extraction_context）。done 时应清 payload/删行，并加周期排空任务与保留上限。
4. **`POST /memories/stack-backup` 打破 X-User-Id 隔离**[C]：任意 console token（按 user 绑定）可下载全部用户的原始三库，是第一个跨用户返回原始数据的 REST 端点。单用户部署实际风险低，但至少要显式限制凭证等级或在文档/响应中声明。（`app/api/memories/export.py:95-186`）
5. **stack-backup 同步阻塞事件循环**[C]：`async def` 内直接跑全库快照+zip+fsync+整包 `read_bytes()`，大库时 SSE 聊天全部停摆。应 `to_thread` + 流式响应。（`export.py:154, 178`）
6. **outbox 零测试 + 工作区门禁不绿**[T]：`chat_finalize` 相关无任何测试；`test_provider_management.py::test_setup_is_ready_only_when_every_chat_route_is_usable` 因 status 新增 `live_probe`/`upstream_ready` 字段而失败（1 failed / 1022 passed / 9 skipped）。

### model-gateway（存量）

7. **流式断连时 usage 记录丢失**[S]：`_streaming_response.body()` 的记账在 `finally` 内 await，客户端断连的 `GeneratorExit`/`CancelledError` 上下文中可能整条丢失（`except Exception` 接不住 `CancelledError`），已计费请求无账；无断连测试。建议 `asyncio.shield` 或独立后台记账。（`service.py:1229-1283`）
8. **每请求在事件循环内同步抢跨进程排他文件锁**[C]：`snapshot()`（17 处调用）每次 `flock(LOCK_EX)`+可能的恢复/重载，任何持锁 CLI 卡住会冻结整个网关（含 /health）；读读之间也互斥。至少挪进 `to_thread`，或数据面走 last-known-good 无锁快照。（`config_store.py:588-609`）

### Web Console

9. **复制 chat token 失败被静默吞掉**[C]：`NewChannelWizard` 的 `copyClientSettings` 无 try/catch，而局域网 HTTP（本产品典型部署）下 `copyText` 恰恰会失败——用户以为复制成功，token 没进剪贴板。（`NewChannelWizard.tsx:538-554`；`KnowledgeLibraryPage.tsx:702` 同类）
10. **apply 中/一次性 token 展示屏可随意关闭**[C]：token 明文只在组件 state，误触关闭永久丢失；apply 进行中关闭不知道是否提交成功；向导里填了供应商 key 关闭也静默丢弃（对比 ProvidersPage 有 unsaved guard，向导没有）。（`NewChannelWizard.tsx:566, 221`）
11. **legacy key 迁移引导是断头路**[C]：三处提示都说"迁移后关闭 legacy key"，全 UI 没有任何一处说明怎么关（无命令、无链接）。非专业用户永远停在黄色警告上。
12. **多密钥场景 401 文案误导**[C]：所有 401 统一映射为"核对 Console token"；把 Console token 粘进 admin key 框时，反馈区的指引是错的。用户全程要接触 5 种凭据，错误提示必须能区分"拿错钥匙"。（`utils/format.ts:112-124`）

### 部署与文档

13. **ingress relay 的静默限流**[C]：`MAX_CONNECTIONS_PER_SOURCE=32` 按源 IP 计数，而 Docker Desktop 的 userland proxy 让所有客户端共享同一源 IP——32 连接是全家桶总额度，超限直接 close 且刻意零日志，"偶尔连不上、毫无线索"不可诊断。（`ingress_relay.py:11-12, 93-95, 130-132`；relay 整体去留见过度设计节）
14. **install.sh 不打印局域网地址**[C]：`client-setup.md:42` 与 `stack-operations.md:71` 承诺会打印手机可用地址，只有 Windows 版实现了；手机接入是文档主推场景。
15. **安装目录丢失（卷仍在）时重装死路**[C]：孤儿卷检测只查 legacy 卷，split 四卷保留时按 fresh 路径走到 `_secure_credentials` 抛错，错误信息不可行动，也无文档出路（其实把两个 key 文件放回 `credentials/` 即可）。README "重复运行修复"的承诺在最可能的场景下不成立。（`install.sh:872-887`；`init_stack.py:278-291`）
16. **Windows 安装器未经实机验证但对外无标注**[C]：`windows-installer-drill.md:9` 自述未在真实 NTFS/Docker Desktop 验证，README 却将其作为正式路径推荐。要么先演练，要么标注实验性。
17. **主要目标用户网络环境下安装必败**[C]：安装依赖 raw.githubusercontent.com、GitHub Releases、GHCR、Sigstore 四类端点，文档全中文、预设渠道是 DashScope/Kimi/DeepSeek 的目标人群恰恰最难直连；无镜像源、无降级提示。

## 三、P2 与体验问题（选摘，完整清单见各分报告）

### 实测发现（用户可感知）

- **多意图消息召回稀释**[T]："新事实+提问"混在一条消息时（"我养了猫……我哪天运动？"），整句作为召回 query 未命中羽毛球记忆；单一意图提问正常。建议召回前做轻量意图分句/query 改写，或按句多路召回取并集。
- **live-probe 声明 60s 缓存但 `POST /providers/live-probe` 写死 `force=True`**[T]：每次点击真实花钱（~4.5s），无冷却；与 CHANGELOG 口径不符。且该端点被 `_is_irreversible` 误分类为不可逆操作，与真正的破坏性操作共享 10 次/分钟预算。[C]
- **记忆分类噪声**[T]：配色偏好被归为 `emotional`（valence 0.8）；实体拆出无意义的 "Dark"；编辑正文后 `source_message` 仍是旧原文且无"已偏离来源"提示。
- **`/knowledge/import` 的 `filename` 走 query 参数**[T]：JSON body + query 参数混搭，422 只报"缺 query.filename"，API 使用者困惑。

### 后端

- 版本常量三处重复无单一事实源（P0-1 的结构性根因）；mcp/console token 请求 `memory_access="read"` 被静默改写为 read-write，应 422；偏好软路径 haystack 拼整条 user_message 且英文 marker 无词边界，无关候选也被降档；启动恢复在 lifespan yield 前串行跑最多 10 次 LLM 调用，可能拖垮 healthcheck 并烧 attempts；`memory-gateway` 直接 `import model_gateway.models` 违反服务边界；claim_key 500 截断在 legacy user_id 无长度校验时可碰撞[S]；usage prune 只在启动时执行，常驻数月不重启则保留策略失效（model-gateway 同）。
- model-gateway：`_destination_validation_message` if/else 两分支相同（本轮 diff 的死代码）；`_validated_admin_body` 对无密钥载荷也一刀切"格式无效"，而安全的 `_safe_validation_message` 已存在却未用于 body 解析；portable-config 不带 revision；409 文案"请刷新页面"对 CLI 语境不符。
- 上轮遗留：PATCH 元数据不同步**已修复**[T]；激活度正反馈回路、线性扫描+手工权重**仍存在**（已声明为已知限制）。

### UI/UX

- 自动创建的 chat token 每次 apply 无条件新增、无幂等提示，会无声累积同名 token；fake-ip 错误启发式过宽（"私网""安全校验"命中即引导放开 198.18/15，反向安全引导）；整栈备份的 admin key 用完不清空；live-probe 结果被后续 load() 覆盖回"尚未探测"；Dashboard 首屏双份全库体检扫描；搜索 20 条上限×客户端筛选=误导性计数；编辑记忆每次弹确认（低风险操作的纯摩擦）而危险等级 warning/danger 使用不一致；简洁模式下 action card 可跳到不在导航里的页面造成"迷路"；deployment/revision/CAS 等术语裸露；"接入信息/客户端接入/接入"三种命名；`ProvidersPage.tsx` 1323 行 12 组件、`NewChannelWizard` 25+ useState、`api.ts`/`types.ts` 各约 1400 行需拆分；401 无全局处理（token 被撤销后满屏 toast 而不是引导重新登录）；`setup.next_action="connect_client"` 后端有值前端没消费——CLI 配好模型的用户打开 UI 没有任何"创建第一个 chat token"引导；整栈备份无对应恢复入口与说明。
- 移动端：pages.css 780px 断点下 35 行 sidebar 样式被 morning-crystal.css 覆盖成死代码；首配阶段底栏没有"模型"入口且角标不可见。

### 部署与文档

- "journal 已保留"系列错误措辞不可行动且口径不一（正确操作几乎都是"重跑同一命令"，但没告诉用户）；"复验备份"实际只查文件非空；一次升级双份全量备份+按文件数保留=历史深度仅约 2.5 次；自动跳端口后所有文档的 2026 失效无提醒；`MEMORY_HOST` 不能绑定特定 IP（如 Tailscale）；锁 PID 复用死锁无恢复提示；安装目录自动发现可被同名 service 的无关 Compose 项目劫持；`deploy/entrypoint.sh` 是死代码且示范被禁止的 secrets 注入环境模式；README 快速开始混入 digest/离线迁移等实现术语；版本号手动维护无"去哪查"指引；英文文档链断（client-setup/ai-install 无英文版）；恢复流程 restore.zip 残留卷内未提清理；setup.sh 非 TTY 静默降级 install-only 易漏看。

## 四、过度设计评估

原则：以"个人电脑 Docker Desktop、单用户、可信 LAN"为基准。

**建议删除/移出用户路径：**

| 项 | 判断 |
| --- | --- |
| **ingress relay**（宿主 2026 由 model 容器转发回 memory） | 收益近零：memory 本来就必须能调 model-gateway，被攻破后仍可经其外传数据，"零出口"名不副实；代价是 184 行自维护 TCP relay、双进程保活、两阶段发布流程、`docker ps` 反直觉拓扑，以及 P1-13 的静默限流故障。**建议直接给 memory-gateway 发布端口**，接受微小残余风险 |
| **cosign 签名验证在用户安装路径** | 信任根与明文下载的 install.sh 本身相同，增量安全被自举方式削弱；却是大陆用户安装失败的最大来源。默认跳过、`MEMORY_VERIFY_SIGNATURES=1` 可选 |
| **validate_compose.py 双重白名单校验** | 防御对象是"自己发布流程出 bug"，属 CI/release gate 职责，不该在每个用户机器上跑两遍 |
| **legacy 单卷自动迁移的全部分支** | 降级为文档化"备份→全新安装→恢复"三步；升级流程近半复杂度来自它 |
| **在线+停写双份备份** | 停写失败流程本来就恢复旧栈退出，在线份可删 |
| **pricing 层**（tier 校验、research 管线、每事件快照） | 个人用户大概率永远不录价格，费用列永远 unknown，整套机制是死重；做成可选插件 |
| **outbox 的 `kind` 泛化** | 只有一种 kind；且机制只做了一半（见 P1），要么补齐 drainer+清理，要么退回"崩溃丢一轮提取"的简单语义——个人助手丢一轮的代价很低 |
| **`_is_irreversible` 手工路径分类表** | 每加端点都要记得维护，本轮已漏 live-probe |

**确认合理、保留：** 四私有卷、read_only/cap_drop/独立 UID、digest 固定（回滚正确性依赖）、stack-init 离线凭据交付（修复了旧版 key 进日志的真问题，但其三重文件安全检查可简化为 O_EXCL+0600）、journal 三阶段单向门、claim 双层结构、config_store 的原子写+CAS（实现是同类最严谨的）、`/docs` 默认关闭、read-only clamp 的单点落位、危险操作 preview→token 提交、SQLite 不换 PostgreSQL 的决策。

量化：安装体系约 5600 行（sh 1480 + ps1 2368 + 部署 Python 约 1800），比许多同类项目整个后端还大；上表落地估计可砍 40-50%，用户可感知可靠性几乎不变。

## 五、用户视角适用性

**非专业用户当前会在哪里流失（按流程顺序）：**

1. **安装**：大陆网络四类 GitHub 端点（P1-17）→ 失败即弃。
2. **凭据**：跑通第一条消息前要接触 5 种凭据（gateway.key/admin.key/供应商 key/chat token/mcp token），README 没有"你现在拿着什么、下一步用它换什么"的三行导览。对比 LiteLLM：一条命令 + 浏览器内 master key 登录 + 内置模型目录里"选择而不是输入"+ UI 里点一下生成 virtual key——同类产品已把这条路径压到 3 步。
3. **配置**：quickstart JSON 路径体验良好（1 命令生成约 11 个对象）；但偏离 quickstart 后手动路径要 12 条 CLI 命令，UI 向导的完成屏仍讲 "deployment/revision/CAS"。
4. **出错**：粘错钥匙时 401 文案指错方向（P1-12）；探测结果被刷新吞掉；"journal 已保留"看不懂。
5. **日常**：记忆治理（搜索/回收站/危险确认）是全站最扎实的部分，达到"运维级"防护；Dashboard 却把黄金位置给了情绪象限/生命力轨道而没有搜索框。

**结论**：对"愿意折腾的技术型个人用户"，当前形态可用且可信；对文档瞄准的 Chatbox/RikkaHub 普通用户，安装网络、凭据认知、错误引导三关的流失率会非常高。产品的工程可靠性投入与新手引导投入严重不成比例。

## 六、与业界对比及高投入改进方向

架构定位（对照 2026 年格局：Mem0=抽取管线、Letta=agent runtime、Zep/Graphiti=时态知识图、若干 local-first 新秀）：本项目"网关自动记忆 + 本地治理 + 统一模型路由"的组合仍是差异化的，逐字 grounding 门控严于 Mem0 的 ADD/UPDATE/DELETE 范式（实测决策日志确实拦住了模型编造）。值得投入的高成本改进：

1. **凭据与首登体验重构**（成本中，收益最高）：首次打开 /ui/ 用 gateway.key 登录后，引导流内完成"输 admin key→配渠道→自动生成 chat token→展示三行客户端配置"一屏到底；README 快速开始压缩为"装→开浏览器→照引导做"。这是把流失率打下来的单点最大杠杆。
2. **安装链路国内可达**（成本中）：镜像源/代理变量支持 + cosign 默认跳过 + GHCR 镜像替代源；否则中文文档的目标人群装不上。
3. **检索升级**（成本高）：候选生成迁移 SQLite FTS5 + `sqlite-vec` 一类嵌入式 ANN（保持零外部依赖），保留现有解释字段；"检索曝光"与"确认有用"分开计数，消除激活度正反馈；多意图 query 分句召回（本次实测已复现真实漏召）。
4. **提取质量迭代**（成本中）：分类器噪声（emotional 误标、实体碎片）可用小规模标注集回归；`source_message` 与编辑后正文的偏离在 UI 显式标注。
5. **明确"轻图"边界**（决策）：要么停在当前 evidence/temporal 边，要么认真引入 Graphiti 类后端做可选插件，避免半套 KG 无限膨胀（沿用上轮结论，本轮未见膨胀，维持现状即可）。

## 七、修复优先级清单

**合并前（阻塞）**：P0-1 备份版本常量+夹具；P1-1/2/3 outbox 状态机+drainer+payload 清理（并补测试）；修 `test_provider_management` 失败；UI #9/#10（复制失败反馈、apply 中禁关+token 确认）。

**近期（1-2 周）**：P1-4 stack-backup 权限声明；P1-5 to_thread；P1-8 snapshot 出事件循环；P1-11 legacy 迁移操作指引；P1-12 401 文案分型；P1-13 relay 决策（建议直接删）；P1-14 sh 打印 LAN 地址；P1-15 安装目录丢失出路（代码或文档）；live-probe force/分类修正；死代码清理（`_destination_validation_message`、`deploy/entrypoint.sh`、pages.css 断点、`providersStatus liveProbe` 参数）。

**中期**：过度设计表的简化项；凭据首登重构；安装国内可达；检索升级；版本常量单一事实源;组件拆分（ProvidersPage/api.ts/types.ts）；英文文档补齐。
