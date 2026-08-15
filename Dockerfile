# The release images use immutable base-image digests. Renovation is explicit:
# update the patch version and digest together, rebuild both architectures, and
# run the complete test/smoke suite before publishing a new semver tag.
FROM node:22.23.2-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS ui-build
WORKDIR /build/ui
COPY services/memory-gateway/ui/package.json services/memory-gateway/ui/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY services/memory-gateway/ui/ ./
RUN npm run build

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS python-wheelhouse
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build

# Download the union dependency graph once, with hashes enforced, into an
# offline wheelhouse.  The three runtime environments below resolve only their
# own project wheels from this trusted artifact set; they do not clone a shared
# union venv into every image.
COPY requirements-runtime.lock ./requirements-runtime.lock
RUN mkdir -p /wheelhouse \
 && python -m pip download \
      --require-hashes --only-binary=:all: \
      --dest /wheelhouse \
      -r requirements-runtime.lock

COPY packages/model-gateway-contracts/pyproject.toml ./model-gateway-contracts/pyproject.toml
COPY packages/model-gateway-contracts/model_gateway_contracts ./model-gateway-contracts/model_gateway_contracts
COPY services/memory-gateway/pyproject.toml services/memory-gateway/README.md ./memory-gateway/
COPY services/memory-gateway/app ./memory-gateway/app
COPY services/model-gateway/pyproject.toml services/model-gateway/README.md ./model-gateway/
COPY services/model-gateway/model_gateway ./model-gateway/model_gateway
RUN python -m venv /opt/build-venv \
 && /opt/build-venv/bin/pip install \
      --no-index --find-links=/wheelhouse \
      setuptools==83.0.0 wheel==0.46.3 \
 && mkdir -p /wheels \
 && /opt/build-venv/bin/pip wheel \
      --no-deps --no-build-isolation --wheel-dir /wheels \
      ./model-gateway-contracts ./memory-gateway ./model-gateway \
 && cp /wheels/*.whl /wheelhouse/

# Each target starts from a fresh venv.  pip resolves against only the
# hash-verified offline wheelhouse, so Model cannot accidentally inherit MCP,
# pypdf, or other Memory-only distributions.
FROM python-wheelhouse AS memory-python-build
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install \
      --no-index --find-links=/wheelhouse \
      /wheelhouse/memory_gateway-*.whl \
      /wheelhouse/model_gateway_contracts-*.whl \
 && /opt/venv/bin/pip check \
 && touch /opt/venv/.pip-check-ok \
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel

FROM python-wheelhouse AS model-python-build
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install \
      --no-index --find-links=/wheelhouse \
      /wheelhouse/local_model_gateway-*.whl \
      /wheelhouse/model_gateway_contracts-*.whl \
 && /opt/venv/bin/pip check \
 && touch /opt/venv/.pip-check-ok \
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel

FROM python-wheelhouse AS init-python-build
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install \
      --no-index --find-links=/wheelhouse \
      /wheelhouse/memory_gateway-*.whl \
      /wheelhouse/local_model_gateway-*.whl \
      /wheelhouse/model_gateway_contracts-*.whl \
 && /opt/venv/bin/pip check \
 && touch /opt/venv/.pip-check-ok \
 && /opt/venv/bin/pip uninstall -y pip setuptools wheel

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime-base
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

FROM runtime-base AS memory-runtime
ENV MEMGW_HOME=/data/config \
    MEMGW_SETTINGS_PATH=/secrets/settings.env \
    MEMGW_PROJECT_ROOT=/app/services/memory-gateway \
    UI_DIST_DIR=/app/ui/dist
COPY --from=memory-python-build /opt/venv /opt/venv
# Keep a validated CLI project root without making it an editable install.
# The installed wheel remains the runtime import; Model source is absent.
COPY services/memory-gateway/pyproject.toml /app/services/memory-gateway/pyproject.toml
COPY services/memory-gateway/app /app/services/memory-gateway/app
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

FROM runtime-base AS model-runtime
ENV MODEL_GATEWAY_HOME=/data \
    MODEL_GATEWAY_SECRETS_PATH=/secrets/secrets.env
COPY --from=model-python-build /opt/venv /opt/venv
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
FROM runtime-base AS stack-init
ENV MEMGW_HOME=/memory-data/config \
    MEMGW_SETTINGS_PATH=/memory-secrets/settings.env \
    MEMGW_PROJECT_ROOT=/app/services/memory-gateway \
    MODEL_GATEWAY_HOME=/model-data \
    MODEL_GATEWAY_SECRETS_PATH=/model-secrets/secrets.env
COPY --from=init-python-build /opt/venv /opt/venv
COPY services/memory-gateway/pyproject.toml /app/services/memory-gateway/pyproject.toml
COPY services/memory-gateway/app /app/services/memory-gateway/app
COPY deploy/init_stack.py /usr/local/libexec/memory-platform/init_stack.py
COPY deploy/migrate_legacy.py /usr/local/libexec/memory-platform/migrate_legacy.py
COPY deploy/backup_legacy.py /usr/local/libexec/memory-platform/backup_legacy.py
COPY deploy/restore_split.py /usr/local/libexec/memory-platform/restore_split.py
COPY deploy/validate_compose.py /usr/local/libexec/memory-platform/validate_compose.py
COPY deploy/plan_install.py /usr/local/libexec/memory-platform/plan_install.py
COPY deploy/verify_backup.py /usr/local/libexec/memory-platform/verify_backup.py
ENTRYPOINT ["python", "/usr/local/libexec/memory-platform/init_stack.py"]
