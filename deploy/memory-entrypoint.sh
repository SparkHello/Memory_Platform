#!/bin/sh
set -eu
umask 077

: "${MEMGW_SETTINGS_PATH:=/secrets/settings.env}"
: "${MEMGW_HOME:=/data/config}"
: "${MEMGW_PROJECT_ROOT:=/app/services/memory-gateway}"
export MEMGW_SETTINGS_PATH MEMGW_HOME MEMGW_PROJECT_ROOT

[ -r "$MEMGW_SETTINGS_PATH" ] || {
  echo "[memory-gateway] 缺少私有 settings.env；请先运行初始化服务。" >&2
  exit 1
}
# app.config opens this mode-0600 file directly. Never execute it as shell
# input; doing so would copy every secret into /proc/<pid>/environ.

exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${MEMORY_PORT:-2026}" \
  --no-access-log
