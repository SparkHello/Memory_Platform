# Model Gateway 收敛方案

日期：2026-08-03
状态：待决策，未动代码

## 结论摘要

当前 `model_gateway_enabled` 在 **14 处**分叉，每个功能实现两遍。收敛后可移除约 **1,619 行独立模块** + **423 行 CLI 代码** + 14 处 else 分支体。

但**不建议立即执行**。理由：网关路径是全项目最新、最不成熟的一层——2026-08-03 一次会话中发现的 6 个缺陷里有 2 个出自它（上线第一天）。收敛意味着拆掉退路，应当在网关跑稳之后进行。前置条件见第四节。

## 一、14 个分支点逐条评估

### A 类：纯模式分派（删 else 即可）——6 处

| 位置 | 网关分支 | 直连 else 分支 | 收敛后 |
|---|---|---|---|
| `openai_compat/gateway_client.py:162` `complete()` | 直接委派 `_complete_via_model_gateway` | 整套 provider 循环 + failover | 删 else |
| `openai_compat/gateway_client.py:230` `open_stream()` | 委派 `_open_model_gateway_stream` | 同上，流式版本 | 删 else |
| `llm/client.py:55` `create_chat_completion()` | 委派 `_create_via_model_gateway` | `providers_for_operation` + failover 循环 | 删 else |
| `api/deps.py:103` `get_embedding_client()` | 构造网关 client（带 `expected_space_id`） | 构造直连 client + `direct_embedding_space_id()` | 删 else，`direct_embedding_space_id()` 随之死亡 |
| `api/deps.py:170` `get_knowledge_search_agent()` | 6 行网关配置 | **约 40 行** MKD 配置拼装（`fast_by_code` / `fast_priority` / `pro_provider` 反转取值） | 删 else，**收益最大的一处** |
| `knowledge/agent.py:175` `_post()` | 委派 `_post_model_gateway` | 按 flash/pro 手工选 provider | 删 else |

这 6 处的共同形态是「网关分支直接 return，else 是一整套并行实现」。删除是机械操作，风险低。

### B 类：元数据与状态上报（分支消失后形态单一）——4 处

| 位置 | 收敛后变化 |
|---|---|
| `gateway_client.py:144` `list_models()` | 只报网关别名，不再调 `providers_for_route("chat")` |
| `api/deps.py:57` `embedding_runtime_enabled()` | 只看 `model_gateway_embedding_space_id`，不再检查 `embedding_api_key`/`embedding_model` |
| `api/knowledge.py:130` `_knowledge_runtime_status()` | 两个约 20 行的 dict 合并为一个；伪 provider code `"G"` 可以取消 |
| `knowledge/agent.py:1386` `_configured_provider_codes()` | 整个函数可能可以删除（网关模式只返回 `["G"]`） |

注意 `"G"` 这个伪 provider code 是为了让网关模式塞进 MKD 的数据结构而发明的。收敛后它没有存在理由，连带 UI 上相关展示要跟着改。

### C 类：网关模式独有的增量校验（删 else 后变无条件）——2 处

| 位置 | 说明 |
|---|---|
| `knowledge/agent.py:760` | 校验响应里的 deployment 亲和性。收敛后变成无条件执行，实际是**加强**而非削弱 |
| `knowledge/agent.py:1061` | 网关模式才附加 `required_deployment`。收敛后无条件附加，`completion_kwargs` 的动态拼装可以改成静态 |

这两处收敛后代码更简单且约束更强，是净收益。

### D 类：CLI 与运维（需改写，不能直接删）——2 处

| 位置 | 说明 |
|---|---|
| `cli.py:553` `_run_model_gateway_check()` | `if not enabled: raise` 的守卫。收敛后永远 enabled，守卫可去掉 |
| `cli.py:967` `doctor` 输出 | else 分支打印 MKD 路由表。收敛后只报网关路由 |

另有 `cli.py:1003` 的 `and not settings.model_gateway_enabled` 检查项一并消失。

## 二、收敛后可以移除的东西

### 独立模块（仅服务直连模式）

| 文件 | 行数 | 说明 |
|---|---|---|
| `app/model_catalog.py` | 287 | MKD 目录解析、路由解析、provider 映射 |
| `app/model_probe.py` | 298 | `memgw model check` 的探测逻辑 |
| `app/llm/routing.py` | 106 | `ProviderCooldowns`、优先级归一化、429 冷却 |
| `app/usage/pricing.py` | 268 | 本地价格目录（网关模式下 `use_local_pricing=False`） |
| `app/catalog/*.json` | 237 | 内置模型/路由/价格目录 |
| **小计** | **1,196** | |

### CLI 子命令（`app/cli.py`，共 1,471 行）

`memgw model` / `memgw route` / `memgw pricing` 三组命令及其处理函数合计 **423 行**，占 cli.py 的 29%。其中 `_resolve_route_models`（56 行）和 `_cmd_pricing_research`（52 行）最大。

这些命令的 help 文本已经写着「新部署推荐使用 modelgw」，说明意图上早已准备退役。

### 配置项

`config.py` 中 14 个 Field 变成死配置：`LLM_MIMO_*`、`LLM_KIMI_*`、`LLM_DEEPSEEK_*`、`UPSTREAM_*`、`LLM_PROVIDER_PRIORITY`、`LLM_RATE_LIMIT_COOLDOWN_SECONDS`、`MODEL_CATALOG_PATH`、`MODEL_ROUTES_PATH`、`PRICING_CATALOG_PATH`。

`EMBEDDING_*` 需要单独判断：`EMBEDDING_DIMENSIONS` 在网关模式下仍用于响应校验，不能删。

**总计约 1,619 行可直接移除，另加 14 处 else 分支体。**

## 三、直连模式还剩什么

三个选项：

### 方案 A：完全移除

14 个分支归零，1,619 行删除，配置项从 69 降到约 55。My_Memory 只认网关协议。

**风险**：Model Gateway 进程挂掉 = 全部模型能力不可用。

### 方案 B：保留最小回退

保留单 provider 直连，去掉 MKD 目录/路由/优先级。

**问题**：这仍然需要在 6 个 A 类分派点保留分支，拿不到「14 → 0」。收益折半，复杂度只降一半。

### 方案 C：维持现状

不收敛。代价是每个新功能继续实现两遍，且两条路径的行为差异只能靠人记住——2026-08-03 发现的 `model` 列语义分裂和敏感度双层检测，都是这么来的。

### 推荐：方案 A，但用运维手段而非并行实现来兜底

关键论证：**Model Gateway 本身已经做了多 provider 故障转移**（route 的有序 targets + `max_attempts`）。My_Memory 的直连模式提供的是**重复的冗余**。

直连模式真正防御的失败场景只有一个：*本机 modelgw 进程没在跑*。而这个风险用 launchd/supervisor 自动拉起来解决，成本远低于维护 14 个分支 + 1,619 行 + 第二套配置系统。

换句话说：**用 20 行的进程守护，换掉 1,619 行的并行实现。**

## 四、阻塞项与前置条件

执行前必须满足：

1. **网关路径的成熟度**。2026-08-03 发现的 6 个缺陷中，2 个出自网关路径（`model` 列语义分裂导致知识库向量检索全灭；`embedding_space_id` 静默降级）。它上线才一天。建议网关连续正常运行 **2 周**、期间完成至少一次知识库重建和一次记忆体检后再动手。

2. **`embedding_space_id` 的历史包袱**。现有 867 条知识向量 + 46 条记忆向量的空间 ID 是 `direct-openai-compatible-v1:c74ae26d…`，这个字符串是直连模式的推导结果，现在被写死在 modelgw 的 deployment 配置里。收敛后 `direct_embedding_space_id()` 函数会被删除，但**这个字符串会永久留在数据和配置里**。两个选择：接受这个历史名字，或者借收敛的机会重建一次向量（约 0.21 元）换一个干净的空间 ID。建议接受，不值得为命名重建。

3. **9 个测试文件涉及直连模式**（引用 `llm_provider_priority` / `providers_for_route` / `model_catalog`）。这是最大的工作量，也最需要小心：这些测试兜住的行为不能凭空消失，要逐个判断是「随直连模式一起退役」还是「应当在网关路径上重新覆盖」。特别是 failover 和 429 冷却相关的用例——网关侧有对应能力，但那是另一个项目的测试，My_Memory 这边会出现覆盖真空。

4. **CLI 的用户迁移**。`memgw model` / `route` / `pricing` 直接删除会让肌肉记忆失效。建议保留命令名但改为提示「已迁移至 modelgw，请使用 `modelgw deployment list`」，一两个版本后再彻底移除。

## 五、建议的执行顺序

每一步独立可验证、可回退：

**第 0 步（现在就能做）**：给 modelgw 加进程守护（launchd），确保开机自启与崩溃重启。这是方案 A 的安全前提，且不依赖任何代码改动。

**第 1 步**：D 类 2 处 + B 类 4 处。这 6 处只影响状态上报和 CLI 输出，不碰请求路径，风险最低。完成后 `"G"` 伪 provider code 消失。

**第 2 步**：C 类 2 处。改为无条件校验，约束加强。

**第 3 步**：A 类 6 处。删除 else 分支体。这一步之后 `providers_for_route` / `providers_for_operation` / `legacy_provider_map` 失去调用方。

**第 4 步**：删除 5 个独立模块 + 423 行 CLI + 14 个死配置项。同步处理 9 个测试文件。

**第 5 步**：清理 memgw `settings.env` 中的 4 把 provider key（此时它们已确定无用）。

## 六、明确不做的事

- 不删测试来「减少行数」。24k 行测试是目前唯一能兜住这些分支的东西。
- 不动核心记忆算法、`/v1` 网关本体（`chat_gateway.py`）、上下文重建（`conversation_context.py`）。这些是项目的价值本身。
- 不为了命名整洁重建向量。
