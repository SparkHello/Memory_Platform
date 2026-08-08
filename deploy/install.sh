#!/usr/bin/env sh
# Memory Platform 一键安装（Docker 路径，macOS / Linux 终端）：
#   curl -fsSL https://raw.githubusercontent.com/SparkHello/Memory_Platform/main/deploy/install.sh | sh
#
# 脚本会：检查 Docker → 下载用户版 Compose → 避开已占用端口 → 启动并等待就绪
# → 打印两枚一次性密钥和下一步指引。重复运行即升级到最新镜像，数据保留在
# memory-platform-data 卷中。
#
# Windows 用户：请安装 Docker Desktop 后按 README 的手工两条命令操作。
# 可选环境变量：
#   MEMORY_PLATFORM_DIR  安装目录（默认 ./memory-platform）
#   MEMORY_PORT          对外端口（默认 2026；被占用时自动顺延）
#   MEMORY_HOST          监听地址（默认 127.0.0.1；手机/局域网设备访问用 0.0.0.0，
#                        仅限可信家庭网络，不要暴露到公网）
set -eu

REPO_RAW="https://raw.githubusercontent.com/SparkHello/Memory_Platform/main"
COMPOSE_NAME="docker-compose.user.yml"
INSTALL_DIR="${MEMORY_PLATFORM_DIR:-memory-platform}"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

say "==> 检查运行环境"
command -v curl >/dev/null 2>&1 || fail "未找到 curl，请先安装 curl 后重试。"
command -v docker >/dev/null 2>&1 || fail "未找到 Docker。请先安装并启动 Docker Desktop（https://docs.docker.com/get-docker/），再重新运行本命令。"
docker info >/dev/null 2>&1 || fail "Docker 已安装但尚未运行。请启动 Docker Desktop 后重试。"
docker compose version >/dev/null 2>&1 || fail "未找到 docker compose 插件，请升级 Docker Desktop 后重试。"

say "==> 下载 Compose 文件到 $INSTALL_DIR/"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
curl -fsSL "$REPO_RAW/deploy/$COMPOSE_NAME" -o "$COMPOSE_NAME" \
  || fail "下载 $COMPOSE_NAME 失败，请检查网络后重试。"

port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$1" >/dev/null 2>&1
  else
    return 1
  fi
}

PORT="${MEMORY_PORT:-2026}"
if port_in_use "$PORT"; then
  if [ -n "${MEMORY_PORT:-}" ]; then
    fail "端口 $PORT 已被占用。请换一个空闲端口重试，例如：MEMORY_PORT=3026 sh $0"
  fi
  CANDIDATE=$((PORT + 1))
  while port_in_use "$CANDIDATE"; do
    CANDIDATE=$((CANDIDATE + 1))
    [ "$CANDIDATE" -lt 2100 ] || fail "2026–2099 端口均被占用，请手动指定：MEMORY_PORT=<空闲端口>"
  done
  say "    默认端口 $PORT 已被占用，改用 $CANDIDATE。"
  PORT=$CANDIDATE
fi
if [ "$PORT" != "2026" ]; then
  printf 'MEMORY_PORT=%s\n' "$PORT" > .env
fi
HOST="${MEMORY_HOST:-127.0.0.1}"
case "$HOST" in
  127.0.0.1|0.0.0.0) ;;
  *) fail "MEMORY_HOST 只支持 127.0.0.1（默认，仅本机）或 0.0.0.0（局域网设备可访问）" ;;
esac
if [ "$HOST" != "127.0.0.1" ]; then
  printf 'MEMORY_HOST=%s\n' "$HOST" >> .env
  say "    已开启局域网访问（MEMORY_HOST=0.0.0.0），请只在可信家庭网络中使用。"
fi
export MEMORY_HOST="$HOST" MEMORY_PORT="$PORT"

say "==> 拉取镜像并启动（镜像约数百 MB，首次拉取需要几分钟）"
docker compose -f "$COMPOSE_NAME" pull
docker compose -f "$COMPOSE_NAME" up -d

say "==> 等待服务就绪（首次启动要完成内部安装，通常 1–2 分钟）"
i=0
until curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -lt 180 ] || fail "服务 3 分钟内未就绪。请运行：cd $INSTALL_DIR && docker compose -f $COMPOSE_NAME logs memory-platform"
  sleep 1
done

LOGS=$(docker compose -f "$COMPOSE_NAME" logs --no-log-prefix memory-platform 2>/dev/null || true)
GATEWAY_KEY=$(printf '%s\n' "$LOGS" | awk '/自动生成客户端访问密钥/{f=1;next} f && /^  [^ ]/{print $1; exit}')
ADMIN_KEY=$(printf '%s\n' "$LOGS" | awk '/自动生成 Web 配置管理密钥/{f=1;next} f && /^  [^ ]/{print $1; exit}')

lan_ip() {
  if command -v ipconfig >/dev/null 2>&1; then
    ipconfig getifaddr en0 2>/dev/null || true
  elif command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | awk '{print $1}' || true
  fi
}
LAN_IP=$(lan_ip)

say ""
say "============================================"
say "Memory Platform 已就绪"
say ""
say "  Web Console（管理台）  http://127.0.0.1:$PORT/ui/"
say "  客户端 Base URL        http://127.0.0.1:$PORT/v1"
say "  客户端模型名           memory-auto"
if [ "$HOST" = "0.0.0.0" ] && [ -n "$LAN_IP" ]; then
  say "  手机/其他设备地址      http://$LAN_IP:$PORT/v1"
fi
say ""
if [ -n "$GATEWAY_KEY" ]; then
  say "  GATEWAY_API_KEY（客户端和 Web Console 登录用）："
  say "    $GATEWAY_KEY"
fi
if [ -n "$ADMIN_KEY" ]; then
  say "  admin key（浏览器里解锁模型渠道配置用，权限更高）："
  say "    $ADMIN_KEY"
fi
if [ -z "$GATEWAY_KEY" ]; then
  say "  检测到已有安装：访问密钥沿用首次启动时生成的那一对，"
  say "  可运行 cd $INSTALL_DIR && docker compose -f $COMPOSE_NAME logs memory-platform 查看首次启动日志。"
fi
say "============================================"
say ""
say "请把上面的密钥保存到密码管理器或备忘录。"
if [ "$HOST" = "127.0.0.1" ]; then
  say ""
  say "想在手机或其他设备上使用？在 $INSTALL_DIR/.env 加一行 MEMORY_HOST=0.0.0.0，"
  if [ -n "$LAN_IP" ]; then
    say "重启后改用本机局域网地址 http://$LAN_IP:$PORT/v1（API Key 和模型名不变）。"
  else
    say "重启后改用本机局域网 IP，例如 http://<电脑IP>:$PORT/v1（API Key 和模型名不变）。"
  fi
  say "重启命令：cd $INSTALL_DIR && docker compose -f $COMPOSE_NAME up -d"
  say "只限可信家庭网络，不要把服务暴露到公网。"
fi
say "下一步：打开 Web Console →「模型与路由」→ 用 admin key 解锁 → 新建渠道、"
say "填入供应商 API Key 并选择模型；然后在客户端按上面三项接入。"
say "以后升级到最新版：重新运行本脚本即可，数据不受影响。"
