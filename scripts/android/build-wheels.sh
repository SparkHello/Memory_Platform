#!/usr/bin/env bash
# Build the three first-party pure-Python wheels the Android app installs from
# apps/android/wheels. Run after any change to the services or the contracts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/apps/android/wheels"
PY="${PYTHON:-$(command -v python3.12 || command -v python3.13 || command -v python3)}"
mkdir -p "$OUT"
rm -f "$OUT"/model_gateway_contracts-*.whl "$OUT"/local_model_gateway-*.whl "$OUT"/memory_gateway-*.whl
"$PY" -m pip wheel --quiet --no-deps --wheel-dir "$OUT" \
  "$ROOT/packages/model-gateway-contracts" \
  "$ROOT/services/model-gateway" \
  "$ROOT/services/memory-gateway"
# setuptools leaves in-tree build/ directories behind; they are not tracked.
for pkg in packages/model-gateway-contracts services/model-gateway services/memory-gateway; do
  rm -rf "$ROOT/$pkg/build"
done
ls -1 "$OUT"
