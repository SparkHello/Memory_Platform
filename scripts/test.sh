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

(
  cd "$MEMORY_SERVICE"
  "$PYTHON" -m pytest
)
(
  cd "$MODEL_SERVICE"
  "$PYTHON" -m pytest
)
(
  cd "$MEMORY_SERVICE/ui"
  npm run build
)
