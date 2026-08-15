#!/usr/bin/env sh
set -eu

SCRIPT_PATH=$0
while [ -L "$SCRIPT_PATH" ]; do
  LINK_TARGET=$(readlink "$SCRIPT_PATH")
  case "$LINK_TARGET" in
    /*) SCRIPT_PATH=$LINK_TARGET ;;
    *) SCRIPT_PATH=$(dirname -- "$SCRIPT_PATH")/$LINK_TARGET ;;
  esac
done

PLATFORM_ROOT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)
MEMORY_SERVICE="$PLATFORM_ROOT/services/memory-gateway"
MODEL_SERVICE="$PLATFORM_ROOT/services/model-gateway"
PYTHON="$MEMORY_SERVICE/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "development environment is missing; run scripts/bootstrap.sh first" >&2
  exit 1
fi

"$PYTHON" -m pip check

TEST_RUNTIME_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/memory-platform-tests.XXXXXX")
case "$TEST_RUNTIME_ROOT" in
  "${TMPDIR:-/tmp}"/memory-platform-tests.*) ;;
  *)
    echo "refusing unsafe test runtime directory" >&2
    exit 1
    ;;
esac
chmod 700 "$TEST_RUNTIME_ROOT"
cleanup_test_runtime() {
  case "$TEST_RUNTIME_ROOT" in
    "${TMPDIR:-/tmp}"/memory-platform-tests.*) rm -rf -- "$TEST_RUNTIME_ROOT" ;;
  esac
}
trap cleanup_test_runtime EXIT HUP INT TERM

mkdir -m 700 \
  "$TEST_RUNTIME_ROOT/tmp" \
  "$TEST_RUNTIME_ROOT/memory-home" \
  "$TEST_RUNTIME_ROOT/memory-secrets" \
  "$TEST_RUNTIME_ROOT/model-home"
umask 077
MEMORY_SETTINGS_FILE="$TEST_RUNTIME_ROOT/memory-secrets/settings.env"
: > "$MEMORY_SETTINGS_FILE"
chmod 600 "$MEMORY_SETTINGS_FILE"

(
  cd "$MEMORY_SERVICE"
  env -i \
    PATH="${PATH:-/usr/bin:/bin}" \
    LANG="${LANG:-C}" \
    TMPDIR="$TEST_RUNTIME_ROOT/tmp" \
    PYTHONDONTWRITEBYTECODE=1 \
    MEMGW_HOME="$TEST_RUNTIME_ROOT/memory-home" \
    MEMGW_SETTINGS_PATH="$MEMORY_SETTINGS_FILE" \
    MEMGW_PROJECT_ROOT="$MEMORY_SERVICE" \
    DATABASE_PATH="$TEST_RUNTIME_ROOT/memory.db" \
    AUTH_DATABASE_PATH="$TEST_RUNTIME_ROOT/auth.db" \
    KNOWLEDGE_DATABASE_PATH="$TEST_RUNTIME_ROOT/knowledge.db" \
    USAGE_DATABASE_PATH="$TEST_RUNTIME_ROOT/memory-usage.db" \
    EVAL_DIR="$TEST_RUNTIME_ROOT/eval" \
    MODEL_GATEWAY_HOME="$TEST_RUNTIME_ROOT/model-home" \
    MODEL_GATEWAY_SECRETS_PATH= \
    GATEWAY_API_KEY= \
    GATEWAY_SIGNING_SECRET=pytest-only-signing-secret-32-bytes-minimum \
    GATEWAY_LEGACY_API_KEY_ENABLED=true \
    MODEL_GATEWAY_API_KEY= \
    MODEL_GATEWAY_BASE_URL= \
    KNOWLEDGE_AGENT_EGRESS_POLICY=none \
    ALLOW_SENSITIVE_EGRESS=false \
    "$PYTHON" -m pytest
)
(
  cd "$MODEL_SERVICE"
  env -i \
    PATH="${PATH:-/usr/bin:/bin}" \
    LANG="${LANG:-C}" \
    TMPDIR="$TEST_RUNTIME_ROOT/tmp" \
    PYTHONDONTWRITEBYTECODE=1 \
    MODEL_GATEWAY_HOME="$TEST_RUNTIME_ROOT/model-home" \
    MODEL_GATEWAY_SECRETS_PATH= \
    "$PYTHON" -m pytest
)
(
  cd "$MEMORY_SERVICE/ui"
  npm test
  npm run build
)
