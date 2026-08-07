#!/usr/bin/env sh
set -eu

# One-command first-run setup: build the environment, install and wire the
# two-service stack, auto-generate the client access key, and start everything.
# This wraps the individual steps so a first-time user (or an AI assistant
# following docs) does not have to remember the exact command order.

SCRIPT_PATH=$0
while [ -L "$SCRIPT_PATH" ]; do
  LINK_TARGET=$(readlink "$SCRIPT_PATH")
  case "$LINK_TARGET" in
    /*) SCRIPT_PATH=$LINK_TARGET ;;
    *) SCRIPT_PATH=$(dirname -- "$SCRIPT_PATH")/$LINK_TARGET ;;
  esac
done

PLATFORM_ROOT=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")/.." && pwd)

BOOTSTRAP_ARGS=""
if [ "${1:-}" = "--skip-ui" ]; then
  BOOTSTRAP_ARGS="--skip-ui"
  shift
fi
if [ "$#" -ne 0 ]; then
  echo "usage: scripts/setup.sh [--skip-ui]" >&2
  exit 2
fi

echo "==> 1/2 准备运行环境"
# shellcheck disable=SC2086
"$PLATFORM_ROOT/scripts/bootstrap.sh" $BOOTSTRAP_ARGS

echo "==> 2/2 安装、接线并启动双服务运行栈"
"$PLATFORM_ROOT/scripts/memgw" stack install --start

echo ""
echo "安装完成。下一步：配置一个渠道和模型。"
echo "  最快路径：services/memory-gateway/.venv/bin/modelgw quickstart"
echo "            （一步问完渠道、模型、用途并连接记忆服务）"
echo "  或交互菜单：scripts/memgw       （主菜单选“设置模型渠道、模型和用途”）"
echo "  给 AI 看：docs/ai-install.md"
echo "查看状态：scripts/memgw stack status"
