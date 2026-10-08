#!/usr/bin/env bash
# 一键启动本地 HTTPS 入口：https://localhost:8443 -> http://127.0.0.1:2026
# 用法：
#   scripts/mcp-https/mcp-https.sh start    启动（首次会自动信任本地证书，可能弹授权框）
#   scripts/mcp-https/mcp-https.sh stop     停止
#   scripts/mcp-https/mcp-https.sh status   查看状态
# 之后在 MCP 客户端里填：https://localhost:8443/mcp （Streamable HTTP，Bearer MCP 密钥）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CADDYFILE="$HERE/Caddyfile"
CADDY_BIN="$(command -v caddy || true)"
[ -z "$CADDY_BIN" ] && CADDY_BIN="/opt/homebrew/bin/caddy"

cmd="${1:-start}"

case "$cmd" in
  start)
    if curl -sk -o /dev/null --max-time 2 https://localhost:8443/; then
      echo "已在运行：https://localhost:8443"
      exit 0
    fi
    "$CADDY_BIN" start --config "$CADDYFILE" --adapter caddyfile
    # 首次运行需要把本地根证书装进用户钥匙串信任（caddy trust 需要 sudo，这里改用 security 免 sudo）
    if ! curl -s -o /dev/null --max-time 2 https://localhost:8443/; then
      echo "首次运行，安装本地根证书到用户钥匙串信任……"
      security add-trusted-cert -d -r trustRoot -k "$HOME/Library/Keychains/login.keychain-db" \
        "$HOME/Library/Application Support/Caddy/pki/authorities/local/root.crt" || true
    fi
    sleep 1
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 https://localhost:8443/mcp)"
    echo "OK: https://localhost:8443/mcp -> HTTP $code（401 属正常，需要 Bearer MCP 密钥）"
    ;;
  stop)
    "$CADDY_BIN" stop
    echo "已停止"
    ;;
  status)
    curl -s -o /dev/null -w 'https://localhost:8443/mcp -> HTTP %{http_code}\n' --max-time 3 https://localhost:8443/mcp || echo "未运行"
    ;;
  *)
    echo "用法: $0 [start|stop|status]" >&2
    exit 1
    ;;
esac
