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
CONTRACTS_PACKAGE="$PLATFORM_ROOT/packages/model-gateway-contracts"
RUNTIME_VENV="$MEMORY_SERVICE/.venv"
INSTALL_UI=1

# Both services declare requires-python >=3.12, so accept any interpreter at
# that version or newer instead of hard-binding a single name. Honor an
# explicit PYTHON_BIN override, but still validate it meets the floor.
python_meets_floor() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' \
    >/dev/null 2>&1
}

select_python() {
  if [ -n "${PYTHON_BIN:-}" ]; then
    if command -v "$PYTHON_BIN" >/dev/null 2>&1 && python_meets_floor "$PYTHON_BIN"; then
      printf '%s\n' "$PYTHON_BIN"
      return 0
    fi
    echo "PYTHON_BIN=$PYTHON_BIN is missing or older than Python 3.12." >&2
    return 1
  fi
  for candidate in python3.12 python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_meets_floor "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "No Python >=3.12 found. Install one, then re-run:" >&2
  echo "  macOS:  brew install python@3.12" >&2
  echo "  other:  https://www.python.org/downloads/" >&2
  echo "Or point to an existing interpreter:" >&2
  echo "  PYTHON_BIN=/path/to/python3.12 scripts/bootstrap.sh" >&2
  return 1
}

if [ "${1:-}" = "--skip-ui" ]; then
  INSTALL_UI=0
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "usage: scripts/bootstrap.sh [--skip-ui]" >&2
  exit 2
fi

if [ ! -x "$RUNTIME_VENV/bin/python" ]; then
  PYTHON_BIN=$(select_python) || exit 1
  "$PYTHON_BIN" -m venv "$RUNTIME_VENV"
fi

"$RUNTIME_VENV/bin/python" -m pip install \
  -c "$PLATFORM_ROOT/constraints.txt" \
  -e "$CONTRACTS_PACKAGE" \
  -e "${MEMORY_SERVICE}[dev]" \
  -e "${MODEL_SERVICE}[dev]"

"$RUNTIME_VENV/bin/python" -m pip check

if [ "$INSTALL_UI" -eq 1 ]; then
  if ! command -v node >/dev/null 2>&1 \
    || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' 2>/dev/null; then
    echo "Node.js 22+ is required for the Web Console (found: $(node --version 2>/dev/null || echo none))." >&2
    echo "Install it, then re-run:" >&2
    echo "  macOS:  brew install node@22" >&2
    echo "  other:  https://nodejs.org/" >&2
    echo "Or build without the Web Console: scripts/bootstrap.sh --skip-ui" >&2
    exit 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm is required for the Web Console; it ships with Node.js 22+ (https://nodejs.org/)." >&2
    echo "Or rerun with --skip-ui to omit the Web Console." >&2
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
