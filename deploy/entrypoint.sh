#!/bin/sh
# Memory Platform 单容器入口：首启接线双服务运行栈，随后以前台方式运行
# Model Gateway（2030，仅容器内回环）和 Memory Gateway（2026，对外）。
set -eu

: "${XDG_CONFIG_HOME:=/data}"
: "${MEMGW_HOME:=/data/memory-gateway}"
: "${MEMGW_PROJECT_ROOT:=/app/services/memory-gateway}"
export XDG_CONFIG_HOME MEMGW_HOME MEMGW_PROJECT_ROOT

MEMORY_PORT="${MEMORY_PORT:-2026}"
MODEL_PORT="${MODEL_PORT:-2030}"

mkdir -p "$MEMGW_HOME/data"

if [ ! -f /data/.stack-installed ]; then
  echo "[memory-platform] 首次启动：安装并安全接线双服务运行栈……"
  memgw stack install
  touch /data/.stack-installed
  echo "[memory-platform] 上面的 GATEWAY_API_KEY 和 admin key 只显示这一次，请从容器日志中妥善保存。"
fi

# stack install 已把 MODEL_GATEWAY_*、GATEWAY_API_KEY 等写入 settings.env，
# 导出给前台 uvicorn 进程（与 memgw 守护进程启动时的 env 传递一致）。
set -a
# shellcheck disable=SC1091
. "$MEMGW_HOME/settings.env"
set +a

export DATABASE_PATH="${DATABASE_PATH:-$MEMGW_HOME/data/memory.db}"
export KNOWLEDGE_DATABASE_PATH="${KNOWLEDGE_DATABASE_PATH:-$MEMGW_HOME/data/knowledge.db}"
export UI_DIST_DIR="${UI_DIST_DIR:-/app/ui/dist}"

# Model Gateway 只绑容器内回环：Memory Gateway 同容器调用，
# 管理接口不暴露到容器外（其安全模式也禁止默认绑 0.0.0.0）
modelgw serve --host 127.0.0.1 &
MODEL_PID=$!

i=0
until python -c "
import sys, urllib.request
try:
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${MODEL_PORT}/healthz', timeout=2).status == 200 else 1)
except Exception:
    sys.exit(1)
"; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "[memory-platform] Model Gateway 健康检查超时，启动失败" >&2
    kill "$MODEL_PID" 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$MODEL_PID" 2>/dev/null; then
    echo "[memory-platform] Model Gateway 进程提前退出，启动失败" >&2
    exit 1
  fi
  sleep 1
done
echo "[memory-platform] Model Gateway 已就绪。"

# 在 MEMGW_HOME 下运行，日志等相对路径产物落在持久卷内
cd "$MEMGW_HOME"
python -m uvicorn app.main:app --host 0.0.0.0 --port "$MEMORY_PORT" &
MEMORY_PID=$!

terminate() {
  kill "$MEMORY_PID" "$MODEL_PID" 2>/dev/null || true
}
trap terminate TERM INT

# 任一进程退出则收摊（POSIX sh 没有 wait -n，用轮询）
while kill -0 "$MEMORY_PID" 2>/dev/null && kill -0 "$MODEL_PID" 2>/dev/null; do
  sleep 2
done
echo "[memory-platform] 有服务进程退出，正在停止整个栈……" >&2
terminate
wait 2>/dev/null || true
exit 1
