#!/bin/sh
set -eu
umask 077

: "${MODEL_GATEWAY_HOME:=/data}"
: "${MODEL_GATEWAY_SECRETS_PATH:=/secrets/secrets.env}"
export MODEL_GATEWAY_HOME MODEL_GATEWAY_SECRETS_PATH

[ -r "$MODEL_GATEWAY_HOME/config.json" ] || {
  echo "[model-gateway] 缺少 config.json；请先运行初始化服务。" >&2
  exit 1
}
[ -r "$MODEL_GATEWAY_SECRETS_PATH" ] || {
  echo "[model-gateway] 缺少私有 secrets.env；请先运行初始化服务。" >&2
  exit 1
}

exec modelgw serve \
  --host 0.0.0.0 \
  --container-network \
  --port "${MODEL_PORT:-2030}" \
  --no-access-log
