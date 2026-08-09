#!/usr/bin/env sh
# Memory Platform 一键安装（Docker 路径，macOS / Linux 终端）：
#   curl -fsSL https://raw.githubusercontent.com/SparkHello/Memory_Platform/main/deploy/install.sh | sh
#
# 脚本会：检查 Docker → 下载用户版 Compose → 避开已占用端口 → 启动并等待就绪
# → 打印两枚一次性密钥和下一步指引。重复运行即升级到最新镜像，数据保留在
# memory-platform-data 卷中。
#
# Windows 用户：请使用同目录的 install.ps1。
# 可选环境变量：
#   MEMORY_PLATFORM_DIR  安装目录（默认 ~/memory-platform；会自动识别已有安装）
#   MEMORY_NO_OPEN       设为 1 时安装完成后不自动打开浏览器
#   MEMORY_PORT          对外端口（默认 2026；被占用时自动顺延）
#   MEMORY_HOST          监听地址（默认 127.0.0.1；手机/局域网设备访问用 0.0.0.0，
#                        仅限可信家庭网络，不要暴露到公网）
#   GATEWAY_API_KEY      自定义客户端访问密钥（留空则自动生成；至少 16 个字符）。
#                        只在首次安装时生效，之后改密钥用 memgw secret set gateway。
#   MEMORY_CONSOLE_ADMIN_KEY
#                        自定义 Web 配置管理密钥（同上）。它权限更高，只在浏览器
#                        里用，不需要填进客户端，也不需要传到手机上。
set -eu

REPO_RAW="https://raw.githubusercontent.com/SparkHello/Memory_Platform/main"
COMPOSE_NAME="docker-compose.user.yml"
INSTALL_DIR="${MEMORY_PLATFORM_DIR:-}"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

# 自带密钥先在本地过一遍下限，免得拉完几百 MB 镜像才在容器里失败。容器内
# memgw stack install 会再做一次完整校验。
check_custom_key() {
  [ -n "$2" ] || return 0
  case "$2" in
    *[[:space:]]*) fail "$1 不能包含空格或换行。" ;;
  esac
  [ "${#2}" -ge 16 ] || fail "$1 至少需要 16 个字符，当前只有 ${#2} 个。不设置该变量则自动生成一枚高强度密钥。"
}
check_custom_key GATEWAY_API_KEY "${GATEWAY_API_KEY:-}"
check_custom_key MEMORY_CONSOLE_ADMIN_KEY "${MEMORY_CONSOLE_ADMIN_KEY:-}"
# 只放进本次 compose 进程的环境，不写入 .env——密钥不落盘在安装目录里。
export GATEWAY_API_KEY="${GATEWAY_API_KEY:-}"
export MEMORY_CONSOLE_ADMIN_KEY="${MEMORY_CONSOLE_ADMIN_KEY:-}"

say "==> 检查运行环境"
command -v curl >/dev/null 2>&1 || fail "未找到 curl，请先安装 curl 后重试。"
command -v docker >/dev/null 2>&1 || fail "未找到 Docker。请先安装并启动 Docker Desktop（https://docs.docker.com/get-docker/），再重新运行本命令。"
docker info >/dev/null 2>&1 || fail "Docker 已安装但尚未运行。请启动 Docker Desktop 后重试。"
docker compose version >/dev/null 2>&1 || fail "未找到 docker compose 插件，请升级 Docker Desktop 后重试。"

if [ -z "$INSTALL_DIR" ]; then
  EXISTING_DIRS=$(docker ps -a \
    --filter label=com.docker.compose.service=memory-platform \
    --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null \
    | awk 'NF && !seen[$0]++')
  EXISTING_COUNT=$(printf '%s\n' "$EXISTING_DIRS" | awk 'NF {count++} END {print count + 0}')
  if [ "$EXISTING_COUNT" -gt 1 ]; then
    fail "检测到多套 Memory Platform。请指定要升级的目录：
  MEMORY_PLATFORM_DIR=/原安装目录 sh -c \"\$(curl -fsSL $REPO_RAW/deploy/install.sh)\""
  elif [ "$EXISTING_COUNT" -eq 1 ]; then
    INSTALL_DIR=$EXISTING_DIRS
    say "    已找到现有安装：$INSTALL_DIR"
  else
    INSTALL_DIR="${HOME:?无法确定用户目录}/memory-platform"
  fi
fi

say "==> 下载 Compose 文件到 $INSTALL_DIR/"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
INSTALL_DIR=$(pwd)
curl -fsSL "$REPO_RAW/deploy/$COMPOSE_NAME" -o "$COMPOSE_NAME" \
  || fail "下载 $COMPOSE_NAME 失败，请检查网络后重试。"

compose_env_value() {
  [ -f .env ] || return 0
  awk -F= -v key="$1" '
    $1 == key {
      value = substr($0, length(key) + 2)
      sub(/\r$/, "", value)
    }
    END { if (value != "") print value }
  ' .env
}

set_compose_env_value() {
  key=$1
  value=$2
  temporary=$(mktemp .env.tmp.XXXXXX) || fail "无法安全更新 .env"
  if [ -f .env ]; then
    awk -v key="$key" -v value="$value" '
      BEGIN { updated = 0 }
      index($0, key "=") == 1 {
        if (!updated) print key "=" value
        updated = 1
        next
      }
      { print }
      END { if (!updated) print key "=" value }
    ' .env > "$temporary"
  else
    printf '%s=%s\n' "$key" "$value" > "$temporary"
  fi
  chmod 600 "$temporary"
  mv "$temporary" .env
}

port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$1" >/dev/null 2>&1
  else
    return 1
  fi
}

compose_owns_port() {
  published=$(docker compose -f "$COMPOSE_NAME" port memory-platform 2026 2>/dev/null || true)
  [ -n "$published" ] && [ "${published##*:}" = "$1" ]
}

PORT_CONFIGURED=0
PORT_FROM_ENV=0
EXISTING_PORT=$(compose_env_value MEMORY_PORT)
if [ -n "${MEMORY_PORT:-}" ]; then
  PORT=$MEMORY_PORT
  PORT_CONFIGURED=1
  PORT_FROM_ENV=1
elif [ -n "$EXISTING_PORT" ]; then
  PORT=$EXISTING_PORT
  PORT_CONFIGURED=1
else
  PORT=2026
fi
case "$PORT" in
  *[!0-9]*|'') fail "MEMORY_PORT 必须是 1–65535 的整数" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] \
  || fail "MEMORY_PORT 必须是 1–65535 的整数"

if port_in_use "$PORT" && ! compose_owns_port "$PORT"; then
  if [ "$PORT_CONFIGURED" -eq 1 ]; then
    # 通过 curl | sh 运行时 $0 是 "sh"，不能用它拼重跑命令，否则用户复制到的是
    # 一条跑不起来的 "sh sh"。这里给出可以直接粘贴的完整命令。
    fail "端口 $PORT 已被占用。请换一个空闲端口重试，例如：
  MEMORY_PORT=3026 sh -c \"\$(curl -fsSL $REPO_RAW/deploy/install.sh)\""
  fi
  CANDIDATE=$((PORT + 1))
  while port_in_use "$CANDIDATE"; do
    CANDIDATE=$((CANDIDATE + 1))
    [ "$CANDIDATE" -lt 2100 ] || fail "2026–2099 端口均被占用，请手动指定：MEMORY_PORT=<空闲端口>"
  done
  say "    默认端口 $PORT 已被占用，改用 $CANDIDATE。"
  PORT=$CANDIDATE
fi
if [ "$PORT" != "2026" ] || [ "$PORT_FROM_ENV" -eq 1 ] || [ -n "$EXISTING_PORT" ]; then
  set_compose_env_value MEMORY_PORT "$PORT"
fi

HOST_FROM_ENV=0
EXISTING_HOST=$(compose_env_value MEMORY_HOST)
if [ -n "${MEMORY_HOST:-}" ]; then
  HOST=$MEMORY_HOST
  HOST_FROM_ENV=1
elif [ -n "$EXISTING_HOST" ]; then
  HOST=$EXISTING_HOST
else
  HOST=127.0.0.1
fi
case "$HOST" in
  127.0.0.1|0.0.0.0) ;;
  *) fail "MEMORY_HOST 只支持 127.0.0.1（默认，仅本机）或 0.0.0.0（局域网设备可访问）" ;;
esac
if [ "$HOST" != "127.0.0.1" ] || [ "$HOST_FROM_ENV" -eq 1 ] || [ -n "$EXISTING_HOST" ]; then
  set_compose_env_value MEMORY_HOST "$HOST"
fi
if [ "$HOST" != "127.0.0.1" ]; then
  say "    已开启局域网访问（MEMORY_HOST=0.0.0.0），请只在可信家庭网络中使用。"
fi
export MEMORY_HOST="$HOST" MEMORY_PORT="$PORT"

# 密钥只在首启日志里打印一次。必须在 up -d 之前判断数据卷是否已存在，才能区分
# “这是重装、密钥沿用旧的” 和 “这是首装、但日志没解析出来”——两者的处置完全不同。
# 优先使用 Compose 自己的 project 名，并通过 Compose 写入卷的两个 label 精确判断。
# 这样既支持默认目录名，也支持用户在环境或 .env 中设置 COMPOSE_PROJECT_NAME。
COMPOSE_PROJECT=${COMPOSE_PROJECT_NAME:-$(compose_env_value COMPOSE_PROJECT_NAME)}
if [ -z "$COMPOSE_PROJECT" ]; then
  COMPOSE_PROJECT=$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]//g')
fi
PREEXISTING=0
CURRENT_CONTAINER=$(docker compose -f "$COMPOSE_NAME" ps -aq memory-platform 2>/dev/null || true)
if [ -n "$CURRENT_CONTAINER" ] ||
   docker volume ls \
     --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
     --filter "label=com.docker.compose.volume=memory-platform-data" \
     --format '{{.Name}}' 2>/dev/null | awk 'NF {found=1} END {exit !found}'; then
  PREEXISTING=1
fi

if [ "$PREEXISTING" -eq 1 ]; then
  say "==> 升级前创建安全备份"
  CURRENT_CONTAINER=$(docker compose -f "$COMPOSE_NAME" ps -q memory-platform 2>/dev/null || true)
  if [ -z "$CURRENT_CONTAINER" ]; then
    docker compose -f "$COMPOSE_NAME" up -d --pull never >/dev/null 2>&1 \
      || fail "检测到已有数据，但无法启动旧版本完成升级前备份。数据未被修改；请先运行：
  cd \"$INSTALL_DIR\" && docker compose -f $COMPOSE_NAME up -d"
    CURRENT_CONTAINER=$(docker compose -f "$COMPOSE_NAME" ps -q memory-platform 2>/dev/null || true)
  fi
  [ -n "$CURRENT_CONTAINER" ] \
    || fail "检测到已有数据，但没有找到可执行备份的容器。数据未被修改。"
  BACKUP_NAME="pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ)-$$.zip"
  mkdir -p backups
  chmod 700 backups
  docker compose -f "$COMPOSE_NAME" exec -T memory-platform \
    memgw stack backup --output "/data/$BACKUP_NAME" \
    || fail "升级前备份失败，已停止升级；现有服务和数据未被替换。"
  docker cp "$CURRENT_CONTAINER:/data/$BACKUP_NAME" "backups/$BACKUP_NAME" \
    || fail "备份已在数据卷中生成，但复制到安装目录失败，已停止升级。"
  chmod 600 "backups/$BACKUP_NAME"
  say "    备份已保存：$INSTALL_DIR/backups/$BACKUP_NAME"
fi

say "==> 拉取镜像并启动（镜像约数百 MB，首次拉取需要几分钟）"
docker compose -f "$COMPOSE_NAME" pull \
  || fail "镜像下载失败。现有数据未被删除；请检查网络或稍后重新运行本脚本。"
docker compose -f "$COMPOSE_NAME" up -d \
  || fail "容器启动失败。升级前备份仍保存在 $INSTALL_DIR/backups/。"

say "==> 等待服务就绪（首次启动要完成内部安装，通常 1–2 分钟）"
i=0
until curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -lt 180 ] || fail "服务 3 分钟内未就绪。请运行：cd \"$INSTALL_DIR\" && docker compose -f $COMPOSE_NAME logs memory-platform"
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
say "Memory Platform 基础服务已启动"
say ""
say "  Web Console（管理台）  http://127.0.0.1:$PORT/ui/"
say "  客户端 Base URL        http://127.0.0.1:$PORT/v1"
say "  客户端模型名           memory-auto"
if [ "$HOST" = "0.0.0.0" ] && [ -n "$LAN_IP" ]; then
  say "  手机/其他设备地址      http://$LAN_IP:$PORT/v1"
fi
say ""
if [ -n "$GATEWAY_API_KEY" ]; then
  say "  GATEWAY_API_KEY（客户端和 Web Console 登录用）：使用了你提供的值"
elif [ -n "$GATEWAY_KEY" ]; then
  say "  GATEWAY_API_KEY（客户端和 Web Console 登录用）："
  say "    $GATEWAY_KEY"
fi
if [ -n "$MEMORY_CONSOLE_ADMIN_KEY" ]; then
  say "  admin key（浏览器里解锁模型渠道配置用）：使用了你提供的值"
elif [ -n "$ADMIN_KEY" ]; then
  say "  admin key（浏览器里解锁模型渠道配置用，权限更高）："
  say "    $ADMIN_KEY"
fi
say ""
say "  只有 GATEWAY_API_KEY 需要填进客户端（含手机）。admin key 权限更高，"
say "  只在这台电脑的浏览器里用，不要传到手机上。"
if { [ -z "$GATEWAY_KEY" ] && [ -z "$GATEWAY_API_KEY" ]; } ||
   { [ -z "$ADMIN_KEY" ] && [ -z "$MEMORY_CONSOLE_ADMIN_KEY" ]; }; then
  if [ "$PREEXISTING" -eq 1 ]; then
    say "  检测到已有安装：访问密钥沿用首次启动时生成的那一对，本次不会重新打印。"
    say "  日志还在的话可以查看：cd \"$INSTALL_DIR\" && docker compose -f $COMPOSE_NAME logs memory-platform"
    say "  已经找不回了就重新生成一对（旧 key 立即失效，所有客户端要同步更新）："
    say "    cd \"$INSTALL_DIR\""
    say "    docker compose -f $COMPOSE_NAME exec memory-platform memgw secret set gateway"
    say "    docker compose -f $COMPOSE_NAME exec memory-platform modelgw secret set memory-console-admin"
  else
    say "  这是首次安装，但没能从日志里解析出密钥（日志可能还没写完或格式有变）。"
    say "  请手动查看，日志里会各打印一次 GATEWAY_API_KEY 和 admin key："
    say "    cd \"$INSTALL_DIR\" && docker compose -f $COMPOSE_NAME logs memory-platform"
  fi
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
  say "重启命令：cd \"$INSTALL_DIR\" && docker compose -f $COMPOSE_NAME up -d"
  say "只限可信家庭网络，不要把服务暴露到公网。"
fi
say "下一步：浏览器会打开首次设置页。先粘贴 GATEWAY_API_KEY，再按页面提示"
say "验证 admin key、选择渠道并粘贴供应商 API Key。模型启用后页面会直接给出客户端配置。"
say "以后升级到最新版：重新运行本脚本即可，数据不受影响。"

if [ "${MEMORY_NO_OPEN:-0}" != "1" ]; then
  if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT/ui/" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    xdg-open "http://127.0.0.1:$PORT/ui/" >/dev/null 2>&1 || true
  fi
fi
