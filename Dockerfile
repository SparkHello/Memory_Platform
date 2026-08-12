# The release images use immutable base-image digests. Renovation is explicit:
# update the patch version and digest together, rebuild both architectures, and
# run the complete test/smoke suite before publishing a new semver tag.
FROM node:22.23.2-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS ui-build
WORKDIR /build/ui
COPY services/memory-gateway/ui/package.json services/memory-gateway/ui/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY services/memory-gateway/ui/ ./
RUN npm run build

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS python-build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build

# Every third-party runtime/build artifact is hash-locked.  Project packages
# are then built as ordinary wheels; release containers never use editable
# installs or import the repository checkout as their primary code path.
COPY requirements-runtime.lock ./requirements-runtime.lock
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes -r requirements-runtime.lock

COPY services/memory-gateway/pyproject.toml services/memory-gateway/README.md ./memory-gateway/
COPY services/memory-gateway/app ./memory-gateway/app
COPY services/model-gateway/pyproject.toml services/model-gateway/README.md ./model-gateway/
COPY services/model-gateway/model_gateway ./model-gateway/model_gateway
RUN mkdir -p /wheels \
 && /opt/venv/bin/pip wheel \
      --no-deps --no-build-isolation --wheel-dir /wheels \
      ./memory-gateway ./model-gateway \
 && /opt/venv/bin/pip install --no-deps /wheels/*.whl \
 && /opt/venv/bin/pip check \
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime-common
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=python-build /opt/venv /opt/venv

# Keep a validated CLI project root without making it an editable install.
# `memgw` uses this path for portable backup/doctor operations in one-shot
# maintenance containers; the installed wheel remains the runtime import.
COPY services/memory-gateway/pyproject.toml /app/services/memory-gateway/pyproject.toml
COPY services/memory-gateway/app /app/services/memory-gateway/app

FROM runtime-common AS memory-runtime
ENV MEMGW_HOME=/data/config \
    MEMGW_SETTINGS_PATH=/secrets/settings.env \
    MEMGW_PROJECT_ROOT=/app/services/memory-gateway \
    UI_DIST_DIR=/app/ui/dist
COPY --from=ui-build /build/ui/dist /app/ui/dist
COPY deploy/memory-entrypoint.sh /usr/local/bin/memory-gateway-entrypoint
RUN groupadd --gid 10001 memory-gateway \
 && useradd --uid 10001 --gid 10001 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin memory-gateway \
 && chmod 0555 /usr/local/bin/memory-gateway-entrypoint \
 && mkdir -p /data /secrets \
 && chown 10001:10001 /data /secrets
USER 10001:10001
VOLUME ["/data", "/secrets"]
EXPOSE 2026
ENTRYPOINT ["memory-gateway-entrypoint"]

FROM runtime-common AS model-runtime
ENV MODEL_GATEWAY_HOME=/data \
    MODEL_GATEWAY_SECRETS_PATH=/secrets/secrets.env
COPY deploy/model-entrypoint.sh /usr/local/bin/model-gateway-entrypoint
RUN groupadd --gid 10002 model-gateway \
 && useradd --uid 10002 --gid 10002 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin model-gateway \
 && chmod 0555 /usr/local/bin/model-gateway-entrypoint \
 && mkdir -p /data /secrets \
 && chown 10002:10002 /data /secrets
USER 10002:10002
VOLUME ["/data", "/secrets"]
EXPOSE 2030
ENTRYPOINT ["model-gateway-entrypoint"]

# One-shot root image.  It is run with networking disabled, initializes or
# migrates only explicitly mounted volumes, drops credentials into a host bind
# as 0600 files, and exits before either long-lived service starts.
FROM runtime-common AS stack-init
ENV MEMGW_HOME=/memory-data/config \
    MEMGW_SETTINGS_PATH=/memory-secrets/settings.env \
    MEMGW_PROJECT_ROOT=/app/services/memory-gateway \
    MODEL_GATEWAY_HOME=/model-data \
    MODEL_GATEWAY_SECRETS_PATH=/model-secrets/secrets.env
COPY deploy/init_stack.py /usr/local/libexec/memory-platform/init_stack.py
COPY deploy/migrate_legacy.py /usr/local/libexec/memory-platform/migrate_legacy.py
COPY deploy/backup_legacy.py /usr/local/libexec/memory-platform/backup_legacy.py
COPY deploy/restore_split.py /usr/local/libexec/memory-platform/restore_split.py
COPY deploy/validate_compose.py /usr/local/libexec/memory-platform/validate_compose.py
ENTRYPOINT ["python", "/usr/local/libexec/memory-platform/init_stack.py"]

# Maintenance deliberately has no default secret mounts.  Compose grants only
# the paths required by an explicitly requested backup/restore operation.
FROM runtime-common AS stack-maintenance
ENV MEMGW_PROJECT_ROOT=/app/services/memory-gateway
ENTRYPOINT ["memgw"]
