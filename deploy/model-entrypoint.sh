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

python /usr/local/libexec/memory-platform/ingress_relay.py &
relay_pid=$!

modelgw serve \
  --host 0.0.0.0 \
  --container-network \
  --port "${MODEL_PORT:-2030}" \
  --no-access-log &
model_pid=$!

stop_children() {
  trap - HUP INT TERM
  kill "$relay_pid" "$model_pid" 2>/dev/null || true
  wait "$relay_pid" 2>/dev/null || true
  wait "$model_pid" 2>/dev/null || true
}

on_signal() {
  stop_children
  exit "$1"
}

trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

# POSIX sh has no portable `wait -n`. Poll both direct children so failure of
# either the fixed relay or Model Gateway terminates the container and lets the
# existing restart policy recover the complete pair.
while kill -0 "$relay_pid" 2>/dev/null && kill -0 "$model_pid" 2>/dev/null; do
  sleep 1
done

stop_children
exit 1
