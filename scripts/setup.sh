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
  --json          Keep stdout machine-readable; progress and private
                  credential file paths are written to stderr.
  --configure-only
                  Reuse an installed stack; skip dependency and stack install.

The recipe must never contain API keys or other secrets.
First-access credentials are generated locally and written only to private
mode-0600 files. GATEWAY_API_KEY, GATEWAY_SIGNING_SECRET,
MODEL_GATEWAY_API_KEY, and MEMORY_CONSOLE_ADMIN_KEY environment variables are
rejected; existing private settings files remain the migration source.
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

# Keep this first-access credential rejection list in sync with
# services/memory-gateway/app/cli.py (_stack_install); change both together.
SECRET_ENV_NAMES=""
[ -z "${GATEWAY_API_KEY:-}" ] || SECRET_ENV_NAMES="$SECRET_ENV_NAMES GATEWAY_API_KEY"
[ -z "${GATEWAY_SIGNING_SECRET:-}" ] || SECRET_ENV_NAMES="$SECRET_ENV_NAMES GATEWAY_SIGNING_SECRET"
[ -z "${MODEL_GATEWAY_API_KEY:-}" ] || SECRET_ENV_NAMES="$SECRET_ENV_NAMES MODEL_GATEWAY_API_KEY"
[ -z "${MEMORY_CONSOLE_ADMIN_KEY:-}" ] || SECRET_ENV_NAMES="$SECRET_ENV_NAMES MEMORY_CONSOLE_ADMIN_KEY"
if [ -n "$SECRET_ENV_NAMES" ]; then
  echo "refusing first-access credentials from process environment:$SECRET_ENV_NAMES" >&2
  echo "remove them; setup writes generated credentials only to private files" >&2
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

# 端口可以在 stack install 时改（存放在 project.json），接入信息必须跟着它走，
# 不能假定 2026，否则打印出来的 base URL 会指向一个没有服务的端口。
MEMORY_PORT=$("$RUNTIME_PYTHON" -c '
try:
    from app.cli_config import cli_paths, read_json
    print(int(read_json(cli_paths().project_file).get("port") or 2026))
except Exception:
    print(2026)
' 2>/dev/null) || MEMORY_PORT=2026
[ -n "$MEMORY_PORT" ] || MEMORY_PORT=2026

if [ "$MODE" = config ] && [ "$JSON_OUTPUT" -eq 1 ]; then
  CURRENT_STEP=finalize
  printf '%s\n' "$QUICKSTART_OUTPUT" | MEMORY_PORT="$MEMORY_PORT" "$RUNTIME_PYTHON" -c '
import json, os, sys
payload = json.load(sys.stdin)
port = os.environ.get("MEMORY_PORT", "2026")
payload["setup_verified"] = True
payload["client"] = {
    "base_url": f"http://127.0.0.1:{port}/v1",
    "model": "memory-auto",
    "web_console": f"http://127.0.0.1:{port}/ui/",
    "mcp": f"http://127.0.0.1:{port}/mcp",
}
json.dump(payload, sys.stdout, ensure_ascii=False)
sys.stdout.write("\n")
'
else
  say ""
  say "============================================"
  say "Memory Platform 已配置并通过检查"
  say ""
  say "  Web Console（管理台）  http://127.0.0.1:$MEMORY_PORT/ui/"
  say "  客户端 Base URL        http://127.0.0.1:$MEMORY_PORT/v1"
  say "  客户端模型名           memory-auto"
  say "============================================"
  say ""
  say "127.0.0.1 指运行本服务的这台电脑。客户端里填以 /v1 结尾的 Base URL，"
  say "默认只监听 127.0.0.1。局域网使用请显式运行："
  say "scripts/memgw stack restart --host 0.0.0.0"
  say "Web Console token 与 Model admin key 位于安装输出列出的 0600 文件。"
  say "聊天客户端另建最小权限 token：scripts/memgw token create --name DEVICE --role chat"
  say "MCP 客户端使用 --role mcp；不要复用 Console token。"
fi
