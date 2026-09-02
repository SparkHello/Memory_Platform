#!/usr/bin/env bash
# Build SQLite with FTS5 for the Android app and drop it in as the library the
# embedded Python's _sqlite3 module links against (libsqlite3_python.so).
# Chaquopy's own copy lacks FTS5, which disables the knowledge base and the
# memory keyword index. Same SQLite version as Chaquopy ships (see status page).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SQLITE_YEAR="${SQLITE_YEAR:-2025}"
SQLITE_VERSION="${SQLITE_VERSION:-3500400}"     # 3.50.4
API="${ANDROID_API:-24}"
ANDROID_HOME="${ANDROID_HOME:-/opt/homebrew/share/android-commandlinetools}"
NDK="${ANDROID_NDK_HOME:-$(ls -d "$ANDROID_HOME"/ndk/* | sort -V | tail -1)}"
HOST_TAG="$(ls "$NDK/toolchains/llvm/prebuilt" | head -1)"
CLANG="$NDK/toolchains/llvm/prebuilt/$HOST_TAG/bin/clang"
WORK="${WORK:-$ROOT/apps/android/build/sqlite}"
OUT_DIR="$ROOT/apps/android/native/arm64-v8a"
mkdir -p "$WORK" "$OUT_DIR"
cd "$WORK"
ZIP="sqlite-amalgamation-$SQLITE_VERSION.zip"
[ -f "$ZIP" ] || curl -fsSL -o "$ZIP" "https://sqlite.org/$SQLITE_YEAR/$ZIP"
rm -rf "sqlite-amalgamation-$SQLITE_VERSION" && unzip -oq "$ZIP"
cd "sqlite-amalgamation-$SQLITE_VERSION"
# Flags: everything CPython's _sqlite3 may reference stays enabled (no OMIT_*),
# plus the full-text/json/rtree/math family that a normal distro build has.
"$CLANG" --target="aarch64-linux-android$API" -shared -fPIC -O2 -DNDEBUG \
  -Wl,-soname,libsqlite3_python.so -Wl,-z,max-page-size=16384 \
  -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS3_PARENTHESIS \
  -DSQLITE_ENABLE_FTS4 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1 -DSQLITE_ENABLE_RTREE \
  -DSQLITE_ENABLE_GEOPOLY -DSQLITE_ENABLE_MATH_FUNCTIONS -DSQLITE_ENABLE_DBSTAT_VTAB \
  -DSQLITE_ENABLE_STAT4 -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_UNLOCK_NOTIFY \
  -DSQLITE_ENABLE_DESERIALIZE -DSQLITE_SECURE_DELETE -DSQLITE_TEMP_STORE=2 \
  -DSQLITE_USE_URI=1 -DSQLITE_DEFAULT_FOREIGN_KEYS=0 -DSQLITE_ENABLE_LOAD_EXTENSION \
  -DHAVE_USLEEP=1 -DHAVE_FDATASYNC=1 -DSQLITE_MAX_VARIABLE_NUMBER=250000 \
  sqlite3.c -o "$OUT_DIR/libsqlite3_python.so" -ldl -lm -llog
"$NDK/toolchains/llvm/prebuilt/$HOST_TAG/bin/llvm-strip" --strip-unneeded "$OUT_DIR/libsqlite3_python.so"
ls -la "$OUT_DIR/libsqlite3_python.so"
echo "compile options: $(strings -n 8 "$OUT_DIR/libsqlite3_python.so" | grep -E "^(ENABLE_FTS5|ENABLE_RTREE|THREADSAFE=1)$" | tr "\n" " ")"
