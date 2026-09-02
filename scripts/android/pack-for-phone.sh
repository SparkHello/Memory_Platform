#!/usr/bin/env bash
# Pack the working tree (including the untracked ui/dist build) into one
# tarball and serve it on the LAN, so a phone running Termux can fetch it with
# curl. Local secrets and databases are excluded.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$HOME/Downloads/memory-platform-phone}"
PORT="${PORT:-8000}"
mkdir -p "$OUT"
if [ ! -f "$ROOT/services/memory-gateway/ui/dist/index.html" ]; then
  echo "缺少 Web 控制台构建：先在 services/memory-gateway/ui 里运行 npm ci && npm run build" >&2
  exit 1
fi
tar czf "$OUT/mp.tgz" -C "$ROOT" \
  --exclude=.git --exclude=node_modules --exclude=.venv --exclude=__pycache__ \
  --exclude=test-results --exclude=.jspace --exclude=build --exclude='*.egg-info' \
  --exclude=.env --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude=services/memory-gateway/data --exclude=apps/android/wheels \
  .
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
echo "打包完成：$(du -h "$OUT/mp.tgz" | cut -f1)"
echo
echo "手机 Termux 里依次运行（保持这个窗口开着）："
echo "  pkg install -y curl"
echo "  curl -o mp.tgz http://${IP:-<Mac的IP>}:${PORT}/mp.tgz"
echo "  mkdir -p Memory_Platform && tar xzf mp.tgz -C Memory_Platform"
echo "  bash Memory_Platform/scripts/android/termux-verify.sh"
echo
echo "按 Ctrl+C 结束分享。"
cd "$OUT" && exec python3 -m http.server "$PORT" --bind 0.0.0.0
