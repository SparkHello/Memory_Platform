#!/usr/bin/env sh
# Tear down one Memory Platform Docker Compose project and its four data volumes.
# Does not run docker system prune or touch unrelated projects.
set -eu

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "未找到 Docker。"
docker info >/dev/null 2>&1 || fail "Docker 尚未运行。"
docker compose version >/dev/null 2>&1 || fail "需要 Docker Compose v2。"

YES=0
for argument in "$@"; do
  case "$argument" in
    --yes|-y) YES=1 ;;
    --help|-h)
      printf '%s\n' "用法：sh deploy/uninstall.sh [--yes]" \
        "在安装目录运行（默认 ~/memory-platform），或设置 MEMORY_PLATFORM_DIR。" \
        "只删除当前 Compose project 的容器、网络和四个数据卷。" \
        "不会执行 docker system prune，也不会删除备份目录。"
      exit 0
      ;;
    *) fail "未知参数：$argument" ;;
  esac
done

INSTALL_DIR="${MEMORY_PLATFORM_DIR:-}"
if [ -z "$INSTALL_DIR" ]; then
  if [ -f docker-compose.user.yml ] || [ -f docker-compose.yml ]; then
    INSTALL_DIR=$(pwd)
  else
    INSTALL_DIR="${HOME:?无法确定用户目录}/memory-platform"
  fi
fi
cd "$INSTALL_DIR" || fail "无法进入安装目录：$INSTALL_DIR"

COMPOSE_FILE=""
if [ -f docker-compose.user.yml ]; then
  COMPOSE_FILE=docker-compose.user.yml
elif [ -f docker-compose.yml ]; then
  COMPOSE_FILE=docker-compose.yml
else
  fail "当前目录没有 docker-compose.user.yml 或 docker-compose.yml：$INSTALL_DIR"
fi

say "将卸载：$INSTALL_DIR （$COMPOSE_FILE）"
say "会删除该 project 的容器、网络和 memory/model 数据卷；credentials/ 与 backups/ 会留在磁盘上。"
if [ "$YES" != 1 ]; then
  printf '确认卸载？输入 yes 继续： '
  read -r answer
  [ "$answer" = "yes" ] || fail "已取消。"
fi

docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans
say "已停止服务并删除该安装的四个数据卷。"
say "如需删掉凭据和备份，请自行检查后删除 $INSTALL_DIR/credentials 与 $INSTALL_DIR/backups。"
