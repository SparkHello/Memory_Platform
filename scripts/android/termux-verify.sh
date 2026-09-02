#!/usr/bin/env bash
# Day-one feasibility check on a real phone, no Android Studio involved.
# Run inside Termux from a clone of this repository:
#   bash scripts/android/termux-verify.sh
# Exposes every unknown at once: Rust builds of pydantic-core/rpds-py on-device,
# FTS5 availability, memory footprint, and whether a chat app on the phone can
# talk to http://127.0.0.1:2026/v1.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DATA="${DATA:-$HOME/memory-platform}"
VENV="${VENV:-$HOME/.venvs/memory-platform}"

pkg update -y
# python-cryptography is prebuilt by Termux and pulls cffi in with it (there is
# no separate python-cffi package). rust/clang are only needed to compile
# pydantic-core and rpds-py once (10-30 min on a phone).
pkg install -y python python-cryptography rust clang binutils make libffi openssl git termux-api

if [ ! -x "$VENV/bin/python" ]; then
  python -m venv --system-site-packages "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip wheel
# cryptography/cffi come from the system site-packages; skip the pins for them.
grep -vE '^(cryptography|cffi|pycparser)==' "$ROOT/apps/android/requirements-embedded.txt" > "$DATA.requirements.txt" 2>/dev/null || {
  mkdir -p "$(dirname "$DATA")"; grep -vE '^(cryptography|cffi|pycparser)==' "$ROOT/apps/android/requirements-embedded.txt" > "$DATA.requirements.txt"; }
pip install -r "$DATA.requirements.txt"
pip install --no-deps "$ROOT/packages/model-gateway-contracts" "$ROOT/services/model-gateway" "$ROOT/services/memory-gateway"

UI="$ROOT/services/memory-gateway/ui/dist"
if [ ! -f "$UI/index.html" ]; then
  echo "Web console build missing at $UI."
  echo "Either copy ui/dist from a desktop build, or: pkg install nodejs && (cd services/memory-gateway/ui && npm ci && npm run build)"
  UI=""
fi

termux-wake-lock || true
echo "Starting stack; first login token: $DATA/credentials/gateway.txt"
exec python "$ROOT/apps/android/app/src/main/python/embedded_stack.py" \
  --data-dir "$DATA" ${UI:+--ui-dist "$UI"}
