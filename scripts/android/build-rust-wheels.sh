#!/usr/bin/env bash
# Cross-compile the two Rust-backed dependencies that have no Android wheels
# anywhere (PyPI, Chaquopy repository, python-for-android): pydantic-core and
# rpds-py. Output goes to apps/android/wheels with Chaquopy-style platform tags.
#
# NOT yet exercised end to end: this machine has neither Rust nor an NDK. The
# steps follow the maturin/pyo3 cross-compilation contract; expect to adjust
# the linker names for your NDK release the first time.
#
# One-time host setup (macOS/Linux):
#   rustup target add aarch64-linux-android
#   cargo install cargo-ndk maturin
#   pip install wheel
#   export ANDROID_NDK_HOME=~/Library/Android/sdk/ndk/<version>
#   # Target Python headers/libs for pyo3 (same Python version as chaquopy { version }):
#   #   https://repo.maven.apache.org/maven2/com/chaquo/python/target/<3.13.x-y>/target-<3.13.x-y>-arm64-v8a.zip
#   export CHAQUOPY_TARGET=~/chaquopy-target-3.13   # unzipped directory containing include/ and lib/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/apps/android/wheels"
WORK="${WORK:-$ROOT/apps/android/build/rust-wheels}"
PY_VERSION="${PY_VERSION:-3.14}"
API="${ANDROID_API:-24}"
TARGET=aarch64-linux-android
PLATFORM_TAG="android_${API}_arm64_v8a"

: "${ANDROID_NDK_HOME:?set ANDROID_NDK_HOME}"
: "${CHAQUOPY_TARGET:?set CHAQUOPY_TARGET (unzipped Chaquopy target Python)}"
HOST_TAG="$(ls "$ANDROID_NDK_HOME/toolchains/llvm/prebuilt" | head -1)"
TOOLCHAIN="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/$HOST_TAG/bin"

export CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER="$TOOLCHAIN/aarch64-linux-android${API}-clang"
export CARGO_TARGET_AARCH64_LINUX_ANDROID_AR="$TOOLCHAIN/llvm-ar"
export CC_aarch64_linux_android="$TOOLCHAIN/aarch64-linux-android${API}-clang"
export AR_aarch64_linux_android="$TOOLCHAIN/llvm-ar"
export PYO3_CROSS_PYTHON_VERSION="$PY_VERSION"
export PYO3_CROSS_LIB_DIR="$CHAQUOPY_TARGET/lib"

versions() { grep -E "^(pydantic-core|rpds-py)==" "$ROOT/apps/android/requirements-embedded.txt" | sed -E 's/[[:space:]]+#.*$//'; }

mkdir -p "$WORK" "$OUT"
cd "$WORK"
for spec in $(versions); do
  name="${spec%%==*}"
  echo "== $spec"
  python3 -m pip download --quiet --no-deps --no-binary :all: --dest . "$spec"
  sdist="$(ls "${name//-/_}"-*.tar.gz | head -1)"
  dir="${sdist%.tar.gz}"
  rm -rf "$dir" && tar xf "$sdist"
  (
    cd "$dir"
    # rpds-py is abi3; pydantic-core needs the exact interpreter version.
    maturin build --release --target "$TARGET" --interpreter "python$PY_VERSION" --out ../dist
  )
done

# maturin tags Android builds as linux_aarch64; Chaquopy's pip matches
# android_<api>_<abi>. Retag in place.
for whl in dist/*.whl; do
  python3 -m wheel tags --remove --platform-tag "$PLATFORM_TAG" "$whl"
done
cp dist/*"$PLATFORM_TAG".whl "$OUT"/
ls -1 "$OUT"
