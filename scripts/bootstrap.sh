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
RUNTIME_VENV="$MEMORY_SERVICE/.venv"
PYTHON_BIN=${PYTHON_BIN:-python3.12}
INSTALL_UI=1

if [ "${1:-}" = "--skip-ui" ]; then
  INSTALL_UI=0
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "usage: scripts/bootstrap.sh [--skip-ui]" >&2
  exit 2
fi

if [ ! -x "$RUNTIME_VENV/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$RUNTIME_VENV"
fi

"$RUNTIME_VENV/bin/python" -m pip install \
  -e "${MEMORY_SERVICE}[dev]" \
  -e "${MODEL_SERVICE}[dev]"

if [ "$INSTALL_UI" -eq 1 ]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm is required for the Web Console; rerun with --skip-ui to omit it." >&2
    exit 1
  fi
  (
    cd "$MEMORY_SERVICE/ui"
    npm ci
    npm run build
  )
fi

echo "Memory Platform development environment is ready."
echo "Next: scripts/memgw stack install --start"
