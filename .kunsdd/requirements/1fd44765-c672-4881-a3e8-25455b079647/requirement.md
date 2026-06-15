# 模型缓存命中价格 &amp; 记忆体检操作按钮 &amp; 路由定价简化

## 背景

### 缓存命中价格

当前「服务商与模型」页面支持固定价格（flat）和分级价格（tiered）两种计费模式，每种模式只有 `input_price_per_million`（输入价格）和 `output_price_per_million`（输出价格）两个字段。但主流 LLM 服务商（OpenAI、Anthropic 等）对缓存命中的 prompt token 有独立定价，缺少该字段会导致用量计费不准确。

费用归属：缓存命中只影响输入侧（prompt cache hit），费用直接合并进 `input_cost`，不单独拆分字段。

### 路由定价冗余

当前路由（`RouteConfig`）有自己的 `input_price_per_million` / `output_price_per_million` 字段。创建路由时虽然从服务商模型自动填充，但保存后是独立副本，与模型价格脱钩。路由本质只是映射（virtual\_model → 提供商模型 + priority），定价应直接从关联的 `ProviderModelConfig` 读取，不应在路由层重复存储。路由表上的价格字段应移除。

### 记忆体检操作按钮

当前「记忆体检」页面（`ReviewPage.tsx`）按 `ReviewAction` 分组展示建议卡片，但只有 `delete`/`merge`/`lower` 三个 action 有对应操作按钮。`review` 类型的建议（如「记忆已到复核时间，建议自然确认是否仍然成立」「两条同类型记忆可能互相冲突」）只展示原因和记忆内容，没有可操作的按钮。用户只能看到建议却无法直接执行操作，需要手动去记忆列表页查找并处理，体验断裂。

## 目标

1. **模型配置新增缓存命中价格**：flat 和 tiered 模式均支持 `cache_hit_price_per_million`（/1M token），持久化到 SQLite，TOML 导入导出保持。计费时缓存命中费用合入 `input_cost`。
2. **路由移除独立定价**：`RouteConfig` 不再存储价格字段，计费逻辑改为通过 `provider_model_id` 读取关联 `ProviderModelConfig` 的价格（含 flat/tiered 及缓存命中价）。旧路由无 `provider_model_id` 的直接删除重配，不做兼容兜底。
3. **记忆体检每条建议有操作按钮**：`review` action 卡片增加「已确认，15天后复核」按钮，点击后延长 `review_after` 15 天。

## 验收标准

### 缓存命中价格

- [ ] 服务商模型配置表单（flat 模式）新增「缓存命中价格 / 1M tokens」输入框，位于输入/输出价格旁
- [ ] 分级价格模式下每个 tier 行也新增「缓存命中价格 / 1M tokens」输入框
- [ ] 模型卡片摘要行显示三种价格（输入/输出/缓存命中）
- [ ] SQLite `provider_model_configs` 表新增 `cache_hit_price_per_million` 列（REAL, DEFAULT 0）
- [ ] `pricing_tiers_json` 中每个 tier JSON 对象增加 `cache_hit` 字段
- [ ] API 请求/响应（`ProviderModelConfigPayload`、`ProviderModelSummary`）包含新字段
- [ ] TOML 导入导出不丢失缓存命中价格
- [ ] 存量数据兼容：未配置时默认 0，不影响现有计费
- [ ] 计费时 `input_cost` = (非缓存命中 prompt / 1M) × input\_price + (缓存命中 prompt / 1M) × cache\_hit\_price

### 路由移除独立定价

- [ ] `RouteConfig`（后端 model）移除 `input_price_per_million`、`output_price_per_million`、`currency` 字段
- [ ] `RouteSummary`（前端 type）移除对应字段
- [ ] `RouteConfigPayload` / `RoutePatchRequest` 移除价格字段
- [ ] 路由表单（RoutesPage）不展示、不编辑价格
- [ ] 路由卡片显示价格时改为从关联的 `ProviderModel` 读取
- [ ] 计费（`billing.py`）改为通过 `route.provider_model_id` 查找 `ProviderModelConfig`，使用其价格（含缓存命中价）
- [ ] 旧路由（无 `provider_model_id`）无价格兜底，SQLite route 表价格列直接删除；用户需重配路由
- [ ] `UsageEvent` 的 `currency` 从 ProviderModel 获取（非 route）

### 记忆体检操作按钮

- [ ] `review` action 的卡片上有「已确认，15天后复核」按钮
- [ ] 点击后调用 `updateMemory` 将 `review_after` 设为当前时间 + 15 天
- [ ] 操作前有 confirm 弹窗：「确认该记忆仍然有效？将 15 天后再次复核」
- [ ] 操作后卡片即时移除，无需整页刷新
- [ ] `keep` action 的卡片同样展示且有对应操作按钮（若后端产生 keep 建议）

## 技术要点

### 涉及文件

**后端：**

- `app/providers/models.py` — `ProviderModelConfig` 加 `cache_hit_price_per_million`；`RouteConfig` 移除价格/币种字段
- `app/providers/store.py` — `_ensure_column` 加 `cache_hit_price_per_million`；route 表删除价格/币种列；provider\_model CRUD 读写新字段
- `app/providers/billing.py` — 计费逻辑改为通过 `provider_model_id` 查找 ProviderModel，读取其价格（含 flat/tiered 及缓存命中价）
- `app/api/admin.py` — Model payload 加 `cache_hit_price_per_million`；Route payload 移除价格；`_resolve_route_provider_model` 不再返回价格；序列化/TOML 导出更新
- `app/providers/router.py` — `route_public_summary` 更新

**前端：**

- `ui/src/types.ts` — `ProviderModelSummary`、`ProviderModelConfigPayload` 加 `cache_hit_price_per_million`；`RouteSummary`、`RouteConfigPayload` 移除价格/币种
- `ui/src/utils/gateway.ts` — `PriceTierDraft` 加 `cache_hit_price_per_million`；`EMPTY_ROUTE_DRAFT` 移除价格；JSON 序列化/反序列化更新
- `ui/src/pages/gateway/ProvidersPage.tsx` — flat 价格区 + tier 编辑器各加一个 `DecimalInput`；模型卡片摘要加缓存命中价
- `ui/src/pages/gateway/RoutesPage.tsx` — 移除价格表单/卡片，改为从关联模型读取展示；`selectModel` 不再拷贝价格
- `ui/src/pages/memory/ReviewPage.tsx` — 为 `review`/`keep` action 卡片加按钮和交互逻辑
- `ui/src/utils/format.ts` — 可选：加价格展示辅助

### 风险

- 缓存命中价格默认 0，存量数据不破坏
- 分级计费 JSON 中 `cache_hit` 可能为 null/missing，反序列化需容错
- 路由价格字段直接删除，无 `provider_model_id` 的旧路由将无法计费，需用户重配
- 计费从 route.price 切换到 model.price 是行为变更，部署后需验证路由计费正常
- 记忆体检「确认」操作默认周期 15 天，硬编码即可

