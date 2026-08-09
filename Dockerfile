# 阶段一：构建 Web Console
FROM node:22-slim AS ui-build
WORKDIR /build/ui
COPY services/memory-gateway/ui/package.json services/memory-gateway/ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY services/memory-gateway/ui/ ./
RUN npm run build

# 阶段二：Python 运行时，单容器内跑 Model Gateway + Memory Gateway 两个进程
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    XDG_CONFIG_HOME=/data \
    MEMGW_HOME=/data/memory-gateway \
    MEMGW_PROJECT_ROOT=/app/services/memory-gateway \
    UI_DIST_DIR=/app/ui/dist \
    PATH="/app/services/memory-gateway/.venv/bin:$PATH"

WORKDIR /app

# gosu 只在入口阶段用于把旧版本 root 所有的持久卷迁移给运行用户，
# 随后立即 exec 为非 root 服务进程。
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/*

# 只拷贝安装两个服务所需的文件，保持镜像层最小
COPY constraints.txt ./constraints.txt
COPY services/memory-gateway/pyproject.toml services/memory-gateway/README.md services/memory-gateway/
COPY services/memory-gateway/app services/memory-gateway/app
COPY services/model-gateway/pyproject.toml services/model-gateway/README.md services/model-gateway/
COPY services/model-gateway/model_gateway services/model-gateway/model_gateway

# venv 位置与开发环境一致（services/memory-gateway/.venv），
# memgw 的运行时发现和 stack 接线都按这个布局工作；
# 用 editable 安装，app/catalog、app/providers 下的 JSON 数据文件直接从源码树读取
RUN python -m venv services/memory-gateway/.venv \
 && services/memory-gateway/.venv/bin/pip install --no-cache-dir \
    -c ./constraints.txt \
    -e ./services/memory-gateway -e ./services/model-gateway \
 && services/memory-gateway/.venv/bin/pip check

COPY --from=ui-build /build/ui/dist /app/ui/dist
COPY deploy/entrypoint.sh /usr/local/bin/memory-platform-entrypoint
RUN groupadd --gid 10001 memory-platform \
 && useradd --uid 10001 --gid memory-platform --no-create-home \
    --home-dir /nonexistent --shell /usr/sbin/nologin memory-platform \
 && chmod +x /usr/local/bin/memory-platform-entrypoint \
 && mkdir -p /data \
 && touch /data/.memory-platform-owner-10001 \
 && chown -R memory-platform:memory-platform /data

# 全部运行数据（配置、密钥、SQLite、日志）都在 /data，挂载卷即可持久化
VOLUME /data
EXPOSE 2026

ENTRYPOINT ["memory-platform-entrypoint"]
