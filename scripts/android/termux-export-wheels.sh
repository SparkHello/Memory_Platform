#!/usr/bin/env bash
# Run inside Termux after termux-verify.sh succeeded. Re-uses the Rust wheels
# pip already built on the phone (pydantic-core, rpds-py), retags them with the
# Android platform tag Chaquopy expects, and serves them over Wi-Fi so the Mac
# can fetch them into apps/android/wheels/.
#
# The phone's Python minor version is printed first: chaquopy { version } in
# apps/android/app/build.gradle.kts must be set to the same value.
set -euo pipefail
VENV="${VENV:-$HOME/.venvs/memory-platform}"
OUT="${OUT:-$HOME/android-wheels}"
API="${ANDROID_API:-24}"
PORT="${PORT:-8080}"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet wheel
mkdir -p "$OUT" && rm -f "$OUT"/*.whl
pip wheel --no-deps --wheel-dir "$OUT" "pydantic-core==$(pip show pydantic-core | awk '/^Version/{print $2}')" "rpds-py==$(pip show rpds-py | awk '/^Version/{print $2}')"
for whl in "$OUT"/*.whl; do
  python -m wheel tags --remove --platform-tag "android_${API}_arm64_v8a" "$whl" >/dev/null
done
echo
echo "Python 版本（填到 build.gradle.kts 的 chaquopy { version }）: $(python -c 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
echo "轮子："; ls -1 "$OUT"
IP="$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)"
echo
echo "Mac 上运行（保持本窗口开着）："
echo "  cd /Users/spark/Project/Memory_Platform/apps/android/wheels && curl -O http://${IP:-<手机IP>}:${PORT}/$(ls "$OUT" | head -1)"
for f in $(ls "$OUT" | tail -n +2); do echo "  curl -O http://${IP:-<手机IP>}:${PORT}/$f"; done
echo
cd "$OUT" && exec python -m http.server "$PORT" --bind 0.0.0.0
