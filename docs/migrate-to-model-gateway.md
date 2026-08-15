# 迁移到 Model Gateway 唯一路径

Memory Gateway **不再支持** 通过 `UPSTREAM_*` / `LLM_*` / 本地 `models.json` 直连上游模型。

所有聊天、记忆提取、知识 Agent 与 embedding 请求必须经由 **Model Gateway**（`MODEL_GATEWAY_BASE_URL` + `MODEL_GATEWAY_API_KEY`）。

## 你需要做什么

### Docker 用户

无需改客户端。确认安装器或 Compose 已启动 Memory 与 Model 两个容器，并在 Console「模型与路由」中配置渠道。

### 源码 / CLI 用户（曾使用 direct-provider）

1. 安装并配置 Model Gateway：

   ```bash
   scripts/setup.sh
   # 或
   memgw stack install
   modelgw quickstart --config examples/quickstart.example.json
   ```

2. 在 Memory 侧只保留中央网关凭据（写入 `settings.env` 或环境变量）：

   - `MODEL_GATEWAY_BASE_URL`（例如 `http://127.0.0.1:2030/v1`）
   - `MODEL_GATEWAY_API_KEY`（backend client key）
   - 可选：`MODEL_GATEWAY_EMBEDDING_SPACE_ID`（启用向量检索时）

3. **删除或忽略** 下列配置（已不再参与路由）：

   - `UPSTREAM_BASE_URL` / `UPSTREAM_API_KEY` / `UPSTREAM_MODEL`
   - `LLM_MIMO_*` / `LLM_KIMI_*` / `LLM_DEEPSEEK_*` / `LLM_PROVIDER_PRIORITY`
   - `MODEL_CATALOG_PATH` / `MODEL_ROUTES_PATH`（已删除；路由只在 Model Gateway）
   - `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL`（改用中央 embedding route + `MODEL_GATEWAY_EMBEDDING_SPACE_ID`）
   - `PRICING_CATALOG_PATH`（已移除；用量与价格只在 Model Gateway）

4. 不要再运行 `memgw model` / `memgw route` / `memgw pricing` 或
   `memgw secret set/delete mimo|kimi|deepseek|upstream|embedding`：这些命令只会打印迁移提示并以状态码 2 退出。
   模型与价格一律用 `modelgw` 或 Console「模型与路由」。

5. 重启 Memory Gateway。未配置 Model Gateway 时，`/readyz` 返回错误码 `model_runtime_configuration_error`，运行时数据端点返回 503 同码；`memgw doctor` 与上述 CLI 迁移提示都会给出本文档链接。

## 行为变化摘要

| 之前 | 现在 |
| --- | --- |
| Memory 可直连供应商 | 仅 Model Gateway |
| `memgw model/route/pricing` 管理本地目录 | 使用 `modelgw` / Console |
| 本地 `usage` 记录直连用量 | 用量在 Model Gateway 侧汇总 |
| embedding 可无 space 直连 | 需配置 `MODEL_GATEWAY_EMBEDDING_SPACE_ID` 才启用向量 |

## 备份包

栈备份中的 `memory/models.json`、`memory/routes.json`、`memory/pricing.json` 可能仍存在于旧包中；恢复时不再作为运行时真相。模型配置以 `model-gateway/config.json` 为准。
