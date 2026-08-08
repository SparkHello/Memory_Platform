#!/usr/bin/env sh
set -eu

# One-command first-run setup for humans and AI assistants. In a real terminal
# the default continues into the guided model quickstart. Automation can pass a
# reviewable, non-secret JSON recipe and provide the provider key on stdin.

SCRIPT_PATH=$0
while [ -L "$SCRIPT_PATH" ]; do
  LINK_TARGET=$(readlink "$SCRIPT_PATH")
  case "$LINK_TARGET" in
    /*) SCRIPT_PATH=$LINK_TARGET ;;
    *) SCRIPT_PATH=$(dirname -- "$SCRIPT_PATH")/$LINK_TARGET ;;
  esac
done

PLATFORM_ROOT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)
MODEL_CLI="$PLATFORM_ROOT/services/memory-gateway/.venv/bin/modelgw"
RUNTIME_PYTHON="$PLATFORM_ROOT/services/memory-gateway/.venv/bin/python"

MODE=auto
INSTALL_UI=1
CONFIG_FILE=""
JSON_OUTPUT=0
CONFIGURE_ONLY=0
CURRENT_STEP=arguments

usage() {
  cat <<'EOF'
usage:
  scripts/setup.sh [--skip-ui] [--guided|--install-only]
  scripts/setup.sh [--skip-ui] --config QUICKSTART.json [--json]
  scripts/setup.sh --configure-only --config QUICKSTART.json [--json]

With no mode in a terminal, setup continues into guided model configuration.
Use --install-only to prepare and start the stack without configuring a model.

AI / non-interactive mode:
  --config FILE   Non-secret recipe matching docs/ai-quickstart.schema.json.
                  The provider API key is read as one line from stdin.
  --json          Keep stdout machine-readable; progress and the one-time
                  client access key are written to stderr.
  --configure-only
                  Reuse an installed stack; skip dependency and stack install.

The recipe must never contain API keys or other secrets.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-ui)
      INSTALL_UI=0
      shift
      ;;
    --guided)
      [ "$MODE" = auto ] || { echo "choose only one setup mode" >&2; exit 2; }
      MODE=guided
      shift
      ;;
    --install-only)
      [ "$MODE" = auto ] || { echo "choose only one setup mode" >&2; exit 2; }
      MODE=install-only
      shift
      ;;
    --config)
      [ "$MODE" = auto ] || { echo "choose only one setup mode" >&2; exit 2; }
      [ "$#" -ge 2 ] || { echo "--config requires a JSON file" >&2; exit 2; }
      MODE=config
      CONFIG_FILE=$2
      shift 2
      ;;
    --json)
      JSON_OUTPUT=1
      shift
      ;;
    --configure-only)
      CONFIGURE_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

finish() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$JSON_OUTPUT" -eq 1 ]; then
    printf '{"setup_verified":false,"error":{"step":"%s","exit_code":%s}}\n' \
      "$CURRENT_STEP" "$status"
  fi
  exit "$status"
}
trap finish EXIT

if [ "$MODE" = auto ]; then
  if [ -t 0 ] && [ -t 1 ]; then
    MODE=guided
  else
    MODE=install-only
  fi
fi
if [ "$JSON_OUTPUT" -eq 1 ] && [ "$MODE" != config ]; then
  echo "--json is only available with --config" >&2
  exit 2
fi
if [ "$CONFIGURE_ONLY" -eq 1 ] && [ "$MODE" = install-only ]; then
  echo "--configure-only requires --config, or --guided in a terminal" >&2
  exit 2
fi
if [ "$MODE" = guided ] && { [ ! -t 0 ] || [ ! -t 1 ]; }; then
  echo "--guided needs an interactive terminal; AI assistants should use --config" >&2
  exit 2
fi
if [ "$MODE" = config ] && [ ! -f "$CONFIG_FILE" ]; then
  echo "quickstart config not found: $CONFIG_FILE" >&2
  exit 2
fi

say() {
  if [ "$JSON_OUTPUT" -eq 1 ]; then
    printf '%s\n' "$*" >&2
  else
    printf '%s\n' "$*"
  fi
}

run_visible() {
  if [ "$JSON_OUTPUT" -eq 1 ]; then
    "$@" >&2
  else
    "$@"
  fi
}

# Read the secret before any installer subprocess can inherit stdin. The value
# remains in this shell only and is later forwarded to modelgw over a pipe.
PROVIDER_API_KEY=""
if [ "$MODE" = config ]; then
  IFS= read -r PROVIDER_API_KEY || true
  if [ -z "$PROVIDER_API_KEY" ]; then
    echo "provider API key is required on stdin in --config mode" >&2
    exit 2
  fi
fi

if [ "$CONFIGURE_ONLY" -eq 0 ]; then
  CURRENT_STEP=bootstrap
  say "==> 准备运行环境"
  if [ "$INSTALL_UI" -eq 1 ]; then
    run_visible "$PLATFORM_ROOT/scripts/bootstrap.sh"
  else
    run_visible "$PLATFORM_ROOT/scripts/bootstrap.sh" --skip-ui
  fi

  CURRENT_STEP=stack_install
  say "==> 安装、接线并启动双服务运行栈"
  run_visible "$PLATFORM_ROOT/scripts/memgw" stack install --start
elif [ ! -x "$MODEL_CLI" ]; then
  echo "尚未安装运行环境；请移除 --configure-only 后重试" >&2
  exit 2
fi

if [ "$MODE" = install-only ]; then
  say ""
  say "运行栈已经安装。配置模型时任选一种方式："
  say "  人工引导：scripts/setup.sh --guided"
  say "  AI 配置：docs/ai-install.md"
  say "  精细菜单：scripts/memgw"
  exit 0
fi

CURRENT_STEP=quickstart
say "==> 配置一个渠道和模型"
if [ "$MODE" = guided ]; then
  "$MODEL_CLI" quickstart --memgw "$PLATFORM_ROOT/scripts/memgw"
else
  QUICKSTART_OUTPUT=$(
    printf '%s\n' "$PROVIDER_API_KEY" | \
      "$MODEL_CLI" quickstart \
        --config "$CONFIG_FILE" \
        --memgw "$PLATFORM_ROOT/scripts/memgw" \
        --json
  )
  PROVIDER_API_KEY=""
  unset PROVIDER_API_KEY
  if [ "$JSON_OUTPUT" -eq 0 ]; then
    printf '%s\n' "$QUICKSTART_OUTPUT"
  fi
fi

CURRENT_STEP=doctor
say "==> 检查完整运行栈"
run_visible "$PLATFORM_ROOT/scripts/memgw" stack doctor

if [ "$MODE" = config ] && [ "$JSON_OUTPUT" -eq 1 ]; then
  CURRENT_STEP=finalize
  printf '%s\n' "$QUICKSTART_OUTPUT" | "$RUNTIME_PYTHON" -c '
import json, sys
payload = json.load(sys.stdin)
payload["setup_verified"] = True
payload["client"] = {
    "base_url": "http://127.0.0.1:2026/v1",
    "model": "memory-auto",
    "web_console": "http://127.0.0.1:2026/ui/",
    "mcp": "http://127.0.0.1:2026/mcp",
}
json.dump(payload, sys.stdout, ensure_ascii=False)
sys.stdout.write("\n")
'
else
  say ""
  say "Memory Platform 已配置并通过检查。"
  say "Web Console：http://127.0.0.1:2026/ui/"
  say "客户端模型：memory-auto"
fi
