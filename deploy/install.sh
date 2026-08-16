#!/usr/bin/env sh
# Memory Platform release installer (macOS/Linux).
# Long-lived services never receive credentials through Compose environment or
# daemon logs.  Generated access values are delivered only as host 0600 files.
set -eu

RELEASE="${MEMORY_PLATFORM_VERSION:-v0.5.1}"
printf '%s\n' "$RELEASE" \
  | awk '$0 ~ /^v[0-9]+\.[0-9]+\.[0-9]+$/ { valid=1 } END { exit !valid }' \
  || { printf 'error: MEMORY_PLATFORM_VERSION 必须是 vX.Y.Z 形式的发布版本。\n' >&2; exit 1; }
REPO_RAW="https://raw.githubusercontent.com/SparkHello/Memory_Platform/$RELEASE"
COMPOSE_NAME="docker-compose.user.yml"
INSTALL_DIR="${MEMORY_PLATFORM_DIR:-}"
# Sigstore verification is opt-in: it needs four extra GitHub endpoints
# (cosign binary, bundle, Fulcio/Rekor) that are unreachable in several
# target networks, while images are already pulled by immutable digest.
VERIFY_SIGNATURES=${MEMORY_VERIFY_SIGNATURES:-0}
case "$VERIFY_SIGNATURES" in 0|1) ;; *)
  printf 'error: MEMORY_VERIFY_SIGNATURES 只允许 0 或 1。\n' >&2; exit 1 ;;
esac
# GHCR is unreachable in some regions.  MEMORY_IMAGE_REGISTRY replaces only
# the registry host (e.g. ghcr.nju.edu.cn); repository paths and digest
# pinning stay identical, so the isolation contract is unchanged.
IMAGE_REGISTRY=${MEMORY_IMAGE_REGISTRY:-ghcr.io}
case "$IMAGE_REGISTRY" in
  ''|*[!A-Za-z0-9._:-]*|*/*)
    printf 'error: MEMORY_IMAGE_REGISTRY 只能是 registry 主机名（可带端口），如 ghcr.nju.edu.cn。\n' >&2
    exit 1 ;;
esac

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }
# Failures that leave the durable cutover journal behind are always resumable:
# the next run of the same installer command recovers idempotently.
fail_journal() {
  printf 'error: %s\n' "$*" >&2
  printf '升级 journal 已保留：重跑同一条安装命令即可自动恢复，数据不会丢。\n' >&2
  exit 1
}

valid_host_ip() {
  # Any dotted-quad IPv4 the operator wants to bind (loopback, all
  # interfaces, or a specific local address).
  printf '%s\n' "$1" | awk -F. '
    NF != 4 { exit 1 }
    {
      for (i = 1; i <= 4; i++) {
        if ($i !~ /^[0-9]+$/ || length($i) > 3 || $i + 0 > 255) exit 1
        if (length($i) > 1 && substr($i, 1, 1) == "0") exit 1
      }
    }
  '
}

host_probe_address() {
  if [ "$1" = 0.0.0.0 ]; then
    printf '127.0.0.1\n'
  else
    printf '%s\n' "$1"
  fi
}

# Legacy variables would remain visible in docker inspect even though the v2
# compose ignores them.  Refuse them instead of silently creating that residue.
if [ -n "${GATEWAY_API_KEY:-}" ] || [ -n "${MEMORY_CONSOLE_ADMIN_KEY:-}" ]; then
  fail "新版安装器不接受环境变量中的密钥；请让离线初始化写入 credentials/*.key。"
fi
unset GATEWAY_API_KEY MEMORY_CONSOLE_ADMIN_KEY 2>/dev/null || true
# Copy user-facing selection inputs once, then remove every variable that
# Docker Compose itself would otherwise let override the old or journalled
# `.env`. Candidate helpers re-inject only these validated private copies.
REQUESTED_COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-}
REQUESTED_MEMORY_HOST=${MEMORY_HOST:-}
REQUESTED_MEMORY_PORT=${MEMORY_PORT:-}
unset COMPOSE_PROJECT_NAME COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE \
  COMPOSE_PROFILES COMPOSE_FILE COMPOSE_PATH_SEPARATOR \
  MEMORY_HOST MEMORY_PORT MEMORY_CREDENTIAL_DIR HOST_UID HOST_GID \
  2>/dev/null || true
# Compose gives exported shell variables precedence over values in .env.  Image
# references are installer-managed persistent state, so inherited values must
# not be able to override either the old stack during rollback or the staged
# candidate during validation.
unset MEMORY_PLATFORM_INIT_IMAGE MEMORY_PLATFORM_MODEL_IMAGE \
  MEMORY_PLATFORM_MEMORY_IMAGE 2>/dev/null || true

command -v curl >/dev/null 2>&1 || fail "未找到 curl。"
command -v docker >/dev/null 2>&1 || fail "未找到 Docker。"
docker info >/dev/null 2>&1 || fail "Docker 尚未运行。"
docker compose version >/dev/null 2>&1 || fail "需要 Docker Compose v2。"

existing_install_dirs() {
  for service in model-gateway memory-gateway memory-platform; do
    docker ps -a --filter "label=com.docker.compose.service=$service" \
      --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null || true
  done | awk 'NF && !seen[$0]++'
}

if [ -z "$INSTALL_DIR" ]; then
  EXISTING_DIRS=$(existing_install_dirs)
  EXISTING_COUNT=$(printf '%s\n' "$EXISTING_DIRS" | awk 'NF {n++} END {print n+0}')
  [ "$EXISTING_COUNT" -le 1 ] || fail "检测到多套安装；请显式设置 MEMORY_PLATFORM_DIR。"
  if [ "$EXISTING_COUNT" -eq 1 ]; then
    INSTALL_DIR=$EXISTING_DIRS
  else
    INSTALL_DIR="${HOME:?无法确定用户目录}/memory-platform"
  fi
fi
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
INSTALL_DIR=$(pwd)

# Hold one installer-wide transaction lock before recovery, environment reads
# that can later be committed, backups, or any mutation. A SIGKILL leaves the
# directory behind; only a well-formed lock whose PID is no longer alive may
# be atomically quarantined and replaced. PID reuse intentionally fails closed.
INSTALL_LOCK="$INSTALL_DIR/.memory-platform-install.lock"
INSTALL_LOCK_HELD=0
release_install_lock() {
  [ "$INSTALL_LOCK_HELD" = 1 ] || return 0
  [ -d "$INSTALL_LOCK" ] && [ ! -L "$INSTALL_LOCK" ] || return 1
  [ -f "$INSTALL_LOCK/owner" ] && [ ! -L "$INSTALL_LOCK/owner" ] || return 1
  [ "$(sed -n '1p' "$INSTALL_LOCK/owner")" = "$$" ] || return 1
  rm -f "$INSTALL_LOCK/owner" || return 1
  rmdir "$INSTALL_LOCK" || return 1
  INSTALL_LOCK_HELD=0
}
acquire_install_lock() {
  if mkdir -m 700 "$INSTALL_LOCK" 2>/dev/null; then
    if ! printf '%s\n' "$$" >"$INSTALL_LOCK/owner" \
      || ! chmod 600 "$INSTALL_LOCK/owner"; then
      rm -f "$INSTALL_LOCK/owner"
      rmdir "$INSTALL_LOCK" 2>/dev/null || true
      return 1
    fi
    INSTALL_LOCK_HELD=1
    return 0
  fi
  [ -d "$INSTALL_LOCK" ] && [ ! -L "$INSTALL_LOCK" ] \
    || fail "安装事务锁不是安全目录；拒绝继续"
  [ -f "$INSTALL_LOCK/owner" ] && [ ! -L "$INSTALL_LOCK/owner" ] \
    || fail "安装事务锁不完整；请确认没有安装器运行后人工检查"
  lock_owner=$(sed -n '1p' "$INSTALL_LOCK/owner")
  case "$lock_owner" in ''|*[!0-9]*) fail "安装事务锁 owner 无效；拒绝继续" ;; esac
  if kill -0 "$lock_owner" 2>/dev/null; then
    fail "另一安装器仍在运行（PID ${lock_owner}）；本次未修改任何状态"
  fi
  stale_lock="$INSTALL_LOCK.stale.$$"
  [ ! -e "$stale_lock" ] || fail "安装事务 stale lock 路径已存在；拒绝继续"
  mv "$INSTALL_LOCK" "$stale_lock" \
    || fail "安装事务锁刚被另一进程接管；请稍后重试"
  [ -f "$stale_lock/owner" ] && [ ! -L "$stale_lock/owner" ] \
    || fail "stale 安装事务锁不安全；拒绝自动清理"
  rm -f "$stale_lock/owner" && rmdir "$stale_lock" \
    || fail "无法清理已终止安装器留下的 stale lock"
  mkdir -m 700 "$INSTALL_LOCK" \
    || fail "另一安装器已取得事务锁；本次未修改任何状态"
  if ! printf '%s\n' "$$" >"$INSTALL_LOCK/owner" \
    || ! chmod 600 "$INSTALL_LOCK/owner"; then
    rm -f "$INSTALL_LOCK/owner"
    rmdir "$INSTALL_LOCK" 2>/dev/null || true
    fail "无法写入安装事务锁 owner"
  fi
  INSTALL_LOCK_HELD=1
}
acquire_install_lock || fail "无法取得安装事务锁"
ORIGINAL_ENV_SNAPSHOT=""
cleanup_base() {
  [ -z "${ORIGINAL_ENV_SNAPSHOT:-}" ] || rm -f "$ORIGINAL_ENV_SNAPSHOT"
  release_install_lock || true
}
trap cleanup_base EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p credentials backups
chmod 700 credentials backups

compose_env_value() {
  [ -f .env ] || return 0
  awk -F= -v key="$1" '
    $1 == key { value=substr($0,length(key)+2); sub(/\r$/, "", value) }
    END { if (value != "") print value }
  ' .env
}

set_compose_env_value() {
  key=$1 value=$2
  temporary=$(mktemp .env.tmp.XXXXXX) || fail "无法安全更新 .env"
  if [ -f .env ]; then
    awk -v key="$key" -v value="$value" '
      BEGIN { found=0 }
      index($0,key "=")==1 { if (!found) print key "=" value; found=1; next }
      { print }
      END { if (!found) print key "=" value }
    ' .env >"$temporary"
  else
    printf '%s=%s\n' "$key" "$value" >"$temporary"
  fi
  chmod 600 "$temporary"
  mv "$temporary" .env
}

remove_compose_env_value() {
  [ -f .env ] || return 0
  temporary=$(mktemp .env.tmp.XXXXXX) || fail "无法安全清理 .env"
  awk -v key="$1" 'index($0,key "=")!=1 { print }' .env >"$temporary"
  chmod 600 "$temporary"
  mv "$temporary" .env
}

restore_compose_env_value() {
  key=$1 value=$2
  if [ -n "$value" ]; then
    set_compose_env_value "$key" "$value"
  else
    remove_compose_env_value "$key"
  fi
}

CUTOVER_JOURNAL="$INSTALL_DIR/.memory-platform-cutover"

journal_value() {
  awk -F= -v key="$1" '
    $1 == key { value=substr($0,length(key)+2) }
    END { if (value != "") print value }
  ' "$CUTOVER_JOURNAL/metadata"
}

valid_sha256_image_id() {
  case "$1" in
    sha256:*) journal_digest=${1#sha256:} ;;
    *) return 1 ;;
  esac
  [ "${#journal_digest}" -eq 64 ] || return 1
  case "$journal_digest" in *[!0-9a-f]*) return 1 ;; esac
  return 0
}

valid_old_image_ref() {
  # $2 is the repository path without registry host（如
  # sparkhello/memory-platform-init）。允许任意 registry 主机（GHCR 或镜像
  # 加速站），digest 固定保证不可变性不变。
  old_image_ref=$1
  old_image_repository=$2
  if valid_sha256_image_id "$old_image_ref"; then
    return 0
  fi
  case "$old_image_ref" in
    */*) ;;
    *) return 1 ;;
  esac
  old_image_host=${old_image_ref%%/*}
  case "$old_image_host" in
    ''|*[!A-Za-z0-9._:-]*) return 1 ;;
  esac
  case "${old_image_ref#*/}" in
    "$old_image_repository"@sha256:*)
      valid_sha256_image_id "sha256:${old_image_ref#*@sha256:}"
      ;;
    *) return 1 ;;
  esac
}

remove_cutover_journal() {
  [ -e "$CUTOVER_JOURNAL" ] || return 0
  [ -d "$CUTOVER_JOURNAL" ] && [ ! -L "$CUTOVER_JOURNAL" ] || return 1
  [ -f "$CUTOVER_JOURNAL/phase" ] && [ ! -L "$CUTOVER_JOURNAL/phase" ] \
    || return 1
  [ "$(sed -n '1p' "$CUTOVER_JOURNAL/phase")" = committed ] || return 1
  for journal_file in metadata old-compose.yml old.env; do
    [ ! -e "$CUTOVER_JOURNAL/$journal_file" ] \
      || rm -f "$CUTOVER_JOURNAL/$journal_file" \
      || return 1
  done
  # Make deletion of all rollback material durable while the committed marker
  # still survives. A crash can therefore only resume cleanup, never infer
  # that an already accepted new stack must be rolled back.
  sync
  rm -f "$CUTOVER_JOURNAL/phase" || return 1
  rmdir "$CUTOVER_JOURNAL" || return 1
  sync
}

commit_cutover_journal() {
  # This helper is deliberately independent from the current installer
  # layout.  It is also used during start-up recovery, before LAYOUT has been
  # discovered and before the later cutover helpers have been defined.
  committed_phase=$(mktemp "$CUTOVER_JOURNAL/.phase.XXXXXX") || return 1
  if ! printf 'committed\n' >"$committed_phase" \
    || ! chmod 600 "$committed_phase" \
    || ! mv "$committed_phase" "$CUTOVER_JOURNAL/phase"; then
    rm -f "$committed_phase"
    return 1
  fi
  # Once the committed marker is visible, the accepted state (which may be a
  # successfully restored old stack) must never be rolled back merely because
  # best-effort journal cleanup was interrupted.  A later installer run will
  # resume cleanup from this marker.
  sync || say "warning: 无法确认 committed journal 已同步到磁盘；保留当前已验收栈"
  if ! remove_cutover_journal; then
    say "warning: 已验收升级的 journal 将在下次安装时继续清理"
  fi
  return 0
}

journal_volume_for() {
  docker volume ls \
    --filter "label=com.docker.compose.project=$1" \
    --filter "label=com.docker.compose.volume=$2" \
    --format '{{.Name}}' | awk 'NF {print; exit}'
}

recover_interrupted_cutover() {
  [ -e "$CUTOVER_JOURNAL" ] || return 0
  [ -d "$CUTOVER_JOURNAL" ] && [ ! -L "$CUTOVER_JOURNAL" ] \
    || fail "升级事务 journal 不是安全目录；拒绝继续"
  if [ ! -e "$CUTOVER_JOURNAL/phase" ]; then
    if [ -z "$(find "$CUTOVER_JOURNAL" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
      rmdir "$CUTOVER_JOURNAL" || fail "无法清理已完成的升级事务目录"
      sync
      return 0
    fi
    fail "升级事务 journal 不完整；拒绝覆盖当前状态"
  fi
  [ -f "$CUTOVER_JOURNAL/phase" ] && [ ! -L "$CUTOVER_JOURNAL/phase" ] \
    || fail "升级事务 journal 阶段文件不安全"
  journal_phase=$(sed -n '1p' "$CUTOVER_JOURNAL/phase")
  if [ "$journal_phase" = committed ]; then
    # Metadata is removed only after the committed candidate has already been
    # published and passed host liveness. If it still exists, a crash happened
    # between durable commit and port publication; finish the new stack and
    # never restore the pre-upgrade backup.
    if [ -f "$CUTOVER_JOURNAL/metadata" ] \
      && [ ! -L "$CUTOVER_JOURNAL/metadata" ]; then
      committed_version=$(journal_value version)
      committed_project=$(journal_value project)
      committed_host=$(journal_value publish_host)
      committed_port=$(journal_value publish_port)
      if [ "$committed_version" = 2 ]; then
        case "$committed_project" in
          ''|*[!a-z0-9_-]*) fail "committed journal 的项目名无效" ;;
        esac
        valid_host_ip "$committed_host" || fail "committed journal 的监听地址无效"
        case "$committed_port" in ''|*[!0-9]*) fail "committed journal 的端口无效" ;; esac
        [ "$committed_port" -ge 1 ] && [ "$committed_port" -le 65535 ] \
          || fail "committed journal 的端口超出范围"
        say "==> 完成已提交升级的宿主端口发布"
        MEMORY_HOST="$committed_host" MEMORY_PORT="$committed_port" \
          docker compose --env-file "$INSTALL_DIR/.env" \
          -p "$committed_project" -f "$COMPOSE_NAME" up -d \
          >/dev/null || fail_journal "已提交新栈无法完成启动；已验收数据不会回滚"
        committed_probe_host=$(host_probe_address "$committed_host")
        committed_wait=0
        until curl -fsS "http://$committed_probe_host:$committed_port/health" \
          >/dev/null 2>&1; do
          committed_wait=$((committed_wait+1))
          [ "$committed_wait" -lt 180 ] \
            || fail_journal "已提交新栈无法恢复宿主 liveness；已验收数据不会回滚"
          sleep 1
        done
      fi
    fi
    say "==> 清理已验收升级遗留的 committed journal"
    remove_cutover_journal || fail "无法清理已验收升级的 journal"
    return 0
  fi
  for required_journal_file in metadata old-compose.yml old.env; do
    [ -f "$CUTOVER_JOURNAL/$required_journal_file" ] \
      && [ ! -L "$CUTOVER_JOURNAL/$required_journal_file" ] \
      || fail "升级事务 journal 不完整；拒绝覆盖当前状态"
  done
  journal_version=$(journal_value version)
  journal_project=$(journal_value project)
  journal_layout=$(journal_value layout)
  journal_backup=$(journal_value backup)
  journal_init_image=$(journal_value old_init_image)
  journal_model_image=$(journal_value old_model_image)
  journal_memory_image=$(journal_value old_memory_image)
  journal_old_env_exists=$(journal_value old_env_exists)
  case "$journal_version" in 1) journal_old_env_exists=1 ;; 2) ;; *) fail "升级事务 journal 版本不受支持" ;; esac
  case "$journal_old_env_exists" in 0|1) ;; *) fail "升级事务 journal 的旧环境状态无效" ;; esac
  case "$journal_project" in
    ''|*[!a-z0-9_-]*) fail "升级事务 journal 的项目名无效" ;;
  esac
  # Legacy single-volume cutovers are owned by deploy/legacy_cutover.py; an
  # interrupted legacy journal from an older installer fails closed here so
  # its rollback material is never silently discarded.
  case "$journal_layout" in split) ;; legacy) fail "升级事务 journal 来自旧版安装器的 legacy 迁移；请先用 deploy/legacy_cutover.py 或旧版安装器完成恢复" ;; *) fail "升级事务 journal 的布局无效" ;; esac
  case "$journal_phase" in prepared|data_may_change) ;; *) fail "升级事务 journal 的阶段无效" ;; esac
  case "$journal_backup" in
    pre-upgrade-*.zip) ;;
    pending)
      # `pending` is written before the quiesced backup exists and is always
      # replaced before the data_may_change phase; recovery from `prepared`
      # never restores data, so no archive is required.
      [ "$journal_phase" = prepared ] \
        || fail "升级事务 journal 在数据阶段缺少备份引用"
      ;;
    *) fail "升级事务 journal 的备份名无效" ;;
  esac
  valid_old_image_ref "$journal_init_image" sparkhello/memory-platform-init \
    && valid_old_image_ref "$journal_model_image" sparkhello/memory-platform-model \
    && valid_old_image_ref "$journal_memory_image" sparkhello/memory-platform-memory \
    || fail "升级事务 journal 的旧镜像引用无效"
  journal_backup_path="$INSTALL_DIR/backups/$journal_backup"
  if [ "$journal_backup" != pending ]; then
    [ -s "$journal_backup_path" ] || fail "升级事务 journal 对应的备份不存在"
  fi

  say "==> 检测到中断的升级事务，先幂等恢复旧栈"
  journal_containers=$(docker ps -aq \
    --filter "label=com.docker.compose.project=$journal_project")
  for journal_container in $journal_containers; do
    docker stop "$journal_container" >/dev/null \
      || fail_journal "无法停止中断事务中的容器"
  done

  recovery_compose=$(mktemp ".$COMPOSE_NAME.recovery.XXXXXX") \
    || fail "无法创建恢复 Compose 临时文件"
  recovery_env=$(mktemp .env.recovery.XXXXXX) \
    || { rm -f "$recovery_compose"; fail "无法创建恢复环境临时文件"; }
  cp "$CUTOVER_JOURNAL/old-compose.yml" "$recovery_compose" \
    && cp "$CUTOVER_JOURNAL/old.env" "$recovery_env" \
    || { rm -f "$recovery_compose" "$recovery_env"; fail "无法读取升级事务快照"; }
  chmod 600 "$recovery_compose" "$recovery_env"
  mv "$recovery_compose" "$COMPOSE_NAME" \
    || fail_journal "无法原子恢复旧 Compose"
  if [ "$journal_old_env_exists" = 1 ]; then
    mv "$recovery_env" .env \
      || fail_journal "无法原子恢复旧 .env"
  else
    rm -f .env || fail_journal "无法恢复旧 .env 缺失状态"
    rm -f "$recovery_env"
  fi

  if [ "$journal_phase" = data_may_change ]; then
    journal_memory_data=$(journal_volume_for "$journal_project" memory-data)
    journal_memory_secrets=$(journal_volume_for "$journal_project" memory-secrets)
    journal_model_data=$(journal_volume_for "$journal_project" model-data)
    [ -n "$journal_memory_data" ] && [ -n "$journal_memory_secrets" ] \
      && [ -n "$journal_model_data" ] \
      || fail_journal "无法定位中断事务的分卷"
    docker run --rm --network none --read-only \
      --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
      -e RESTORE_ARCHIVE=/backup/restore.zip \
      --mount "type=volume,source=$journal_memory_data,target=/data" \
      --mount "type=volume,source=$journal_memory_secrets,target=/secrets" \
      --mount "type=volume,source=$journal_model_data,target=/model-data" \
      --mount "type=bind,source=$journal_backup_path,target=/backup/restore.zip,readonly" \
      --tmpfs /tmp:rw,noexec,nosuid,size=134217728 \
      --entrypoint python "$journal_init_image" \
      /usr/local/libexec/memory-platform/restore_split.py >/dev/null \
      || fail_journal "中断事务的数据恢复失败"
  fi

  MEMORY_PLATFORM_INIT_IMAGE="$journal_init_image" \
    MEMORY_PLATFORM_MODEL_IMAGE="$journal_model_image" \
    MEMORY_PLATFORM_MEMORY_IMAGE="$journal_memory_image" \
    docker compose -p "$journal_project" -f "$COMPOSE_NAME" \
    up -d --pull never >/dev/null \
    || fail_journal "旧栈重启失败"
  commit_cutover_journal || fail "旧栈已恢复，但无法提交升级事务 journal"
  say "    中断升级已恢复；继续重新执行发布校验。"
}

recover_interrupted_cutover

# Snapshot the exact pre-install bytes only after interrupted recovery has
# completed. Candidate hygiene and host settings are staged separately; a
# failed download, validation or cutover therefore leaves the live file byte
# for byte unchanged (or absent if it was absent).
OLD_ENV_EXISTS=0
[ ! -f .env ] || OLD_ENV_EXISTS=1
ORIGINAL_ENV_SNAPSHOT=$(mktemp .env.original.XXXXXX) \
  || fail "无法创建旧环境快照"
if [ "$OLD_ENV_EXISTS" = 1 ]; then
  cp .env "$ORIGINAL_ENV_SNAPSHOT" || fail "无法读取旧环境文件"
else
  : >"$ORIGINAL_ENV_SNAPSHOT"
fi
chmod 600 "$ORIGINAL_ENV_SNAPSHOT"

HOST_UID_VALUE=$(id -u 2>/dev/null || true)
HOST_GID_VALUE=$(id -g 2>/dev/null || true)
case "$HOST_UID_VALUE" in *[!0-9]*|'') HOST_UID_VALUE="" ;; esac
case "$HOST_GID_VALUE" in *[!0-9]*|'') HOST_GID_VALUE="" ;; esac

INVOCATION_PROJECT=$REQUESTED_COMPOSE_PROJECT_NAME
STORED_PROJECT=$(compose_env_value COMPOSE_PROJECT_NAME)
discover_projects_for_install_directory() {
  docker ps -a \
    --filter "label=com.docker.compose.project.working_dir=$INSTALL_DIR" \
    --format '{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}' \
    2>/dev/null \
    | awk -F'|' '
        $2=="memory-platform" || $2=="memory-gateway" ||
        $2=="model-gateway" || $2=="stack-init" { if ($1!="") print $1 }
      ' \
    | sort -u
}
DISCOVERED_PROJECTS=$(discover_projects_for_install_directory)
DISCOVERED_PROJECT_COUNT=$(printf '%s\n' "$DISCOVERED_PROJECTS" \
  | awk 'NF {count++} END {print count+0}')
[ "$DISCOVERED_PROJECT_COUNT" -le 1 ] \
  || fail "安装目录对应多个旧 Compose project；拒绝猜测数据归属"
DISCOVERED_PROJECT=$(printf '%s\n' "$DISCOVERED_PROJECTS" | awk 'NF {print; exit}')
if [ -n "$DISCOVERED_PROJECT" ]; then
  [ -z "$INVOCATION_PROJECT" ] || [ "$INVOCATION_PROJECT" = "$DISCOVERED_PROJECT" ] \
    || fail "COMPOSE_PROJECT_NAME 与旧容器 project 身份冲突；旧栈未修改"
  [ -z "$STORED_PROJECT" ] || [ "$STORED_PROJECT" = "$DISCOVERED_PROJECT" ] \
    || fail ".env 的 COMPOSE_PROJECT_NAME 与旧容器身份冲突；拒绝迁移"
  PROJECT=$DISCOVERED_PROJECT
else
  if [ -n "$INVOCATION_PROJECT" ] && [ -n "$STORED_PROJECT" ] \
    && [ "$INVOCATION_PROJECT" != "$STORED_PROJECT" ]; then
    fail "本次 COMPOSE_PROJECT_NAME 与现有 .env 冲突；拒绝切换数据 project"
  fi
  PROJECT=${INVOCATION_PROJECT:-$STORED_PROJECT}
  if [ -z "$PROJECT" ]; then
    PROJECT=$(basename "$INSTALL_DIR" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]//g')
  fi
fi
[ -n "$PROJECT" ] || PROJECT=memory-platform
case "$PROJECT" in *[!a-z0-9_-]*|'') fail "Compose project 名无效" ;; esac

compose() {
  compose_file=$1
  shift
  docker compose -p "$PROJECT" -f "$compose_file" "$@"
}

# Validate/pull a candidate without exporting its image references into the
# installer process.  A global export would also override the restored .env
# when Compose starts the old stack during rollback.
compose_with_images() {
  compose_image_file=$1
  compose_init_image=$2
  compose_model_image=$3
  compose_memory_image=$4
  shift 4
  MEMORY_PLATFORM_INIT_IMAGE="$compose_init_image" \
    MEMORY_PLATFORM_MODEL_IMAGE="$compose_model_image" \
    MEMORY_PLATFORM_MEMORY_IMAGE="$compose_memory_image" \
    docker compose -p "$PROJECT" -f "$compose_image_file" "$@"
}

compose_candidate_with_images() {
  compose_image_file=$1
  compose_init_image=$2
  compose_model_image=$3
  compose_memory_image=$4
  shift 4
  MEMORY_CREDENTIAL_DIR=./credentials \
    HOST_UID="$HOST_UID_VALUE" HOST_GID="$HOST_GID_VALUE" \
    MEMORY_HOST="$HOST" MEMORY_PORT="$PORT" \
    MEMORY_PLATFORM_INIT_IMAGE="$compose_init_image" \
    MEMORY_PLATFORM_MODEL_IMAGE="$compose_model_image" \
    MEMORY_PLATFORM_MEMORY_IMAGE="$compose_memory_image" \
    docker compose --env-file "$CANDIDATE_EMPTY_ENV" \
      -p "$PROJECT" -f "$compose_image_file" "$@"
}

compose_internal_with_images() {
  compose_image_file=$1
  compose_override_file=$2
  compose_init_image=$3
  compose_model_image=$4
  compose_memory_image=$5
  shift 5
  MEMORY_CREDENTIAL_DIR=./credentials \
    HOST_UID="$HOST_UID_VALUE" HOST_GID="$HOST_GID_VALUE" \
    MEMORY_HOST="$HOST" MEMORY_PORT="$PORT" \
    MEMORY_PLATFORM_INIT_IMAGE="$compose_init_image" \
    MEMORY_PLATFORM_MODEL_IMAGE="$compose_model_image" \
    MEMORY_PLATFORM_MEMORY_IMAGE="$compose_memory_image" \
    docker compose --env-file "$CANDIDATE_EMPTY_ENV" \
      -p "$PROJECT" -f "$compose_image_file" \
      -f "$compose_override_file" "$@"
}

validate_candidate_topology() {
  validation_mode=$1
  CANDIDATE_RENDERED_JSON=$(mktemp .compose.rendered.XXXXXX.json) \
    || fail "无法创建候选拓扑临时文件"
  if [ "$validation_mode" = public ]; then
    if ! compose_candidate_with_images "$CANDIDATE_COMPOSE" \
        "$INIT_IMAGE" "$MODEL_IMAGE" "$MEMORY_IMAGE" \
        --profile maintenance config --format json \
        >"$CANDIDATE_RENDERED_JSON" \
      || [ ! -s "$CANDIDATE_RENDERED_JSON" ]; then
      fail "候选 public Compose 无法渲染为可审计配置"
    fi
    validation_suffix=""
  else
    if ! compose_internal_with_images "$CANDIDATE_COMPOSE" \
        "$CANDIDATE_INTERNAL_OVERRIDE" \
        "$INIT_IMAGE" "$MODEL_IMAGE" "$MEMORY_IMAGE" \
        --profile maintenance config --format json \
        >"$CANDIDATE_RENDERED_JSON" \
      || [ ! -s "$CANDIDATE_RENDERED_JSON" ]; then
      fail "候选 internal Compose 无法渲染为可审计配置"
    fi
    validation_suffix=internal
  fi

  # Run the validator shipped in the exact candidate init image.  The
  # rendered JSON enters through stdin: the validator receives no Docker
  # socket, host mounts, volumes, network, or credential values.
  if [ -n "$validation_suffix" ]; then
    docker run --rm -i --pull never --network none --read-only \
      --cap-drop ALL --security-opt no-new-privileges:true \
      --user 65534:65534 --entrypoint python "$INIT_IMAGE" \
      /usr/local/libexec/memory-platform/validate_compose.py \
      "$INIT_IMAGE" "$MODEL_IMAGE" "$MEMORY_IMAGE" \
      "$HOST" "$PORT" "$INSTALL_DIR/credentials" "$validation_suffix" \
      <"$CANDIDATE_RENDERED_JSON" >/dev/null \
      || fail "候选 $validation_mode Compose 未通过安全拓扑校验"
  else
    docker run --rm -i --pull never --network none --read-only \
      --cap-drop ALL --security-opt no-new-privileges:true \
      --user 65534:65534 --entrypoint python "$INIT_IMAGE" \
      /usr/local/libexec/memory-platform/validate_compose.py \
      "$INIT_IMAGE" "$MODEL_IMAGE" "$MEMORY_IMAGE" \
      "$HOST" "$PORT" "$INSTALL_DIR/credentials" \
      <"$CANDIDATE_RENDERED_JSON" >/dev/null \
      || fail "候选 $validation_mode Compose 未通过安全拓扑校验"
  fi
  rm -f "$CANDIDATE_RENDERED_JSON" \
    || fail "无法清理候选拓扑临时文件"
  CANDIDATE_RENDERED_JSON=""
}

existing_service_readiness() {
  readiness_service=$1
  readiness_url=$2
  if [ "$LAYOUT" != split ]; then
    printf 'absent\n'
    return 0
  fi
  if ! readiness_containers=$(compose "$ACTIVE_COMPOSE" ps -aq \
      "$readiness_service" 2>/dev/null); then
    printf 'unknown\n'
    return 0
  fi
  readiness_count=$(printf '%s\n' "$readiness_containers" \
    | awk 'NF { count++ } END { print count+0 }')
  if [ "$readiness_count" -eq 0 ]; then
    printf 'absent\n'
    return 0
  fi
  if [ "$readiness_count" -ne 1 ]; then
    printf 'unknown\n'
    return 0
  fi
  readiness_container=$(printf '%s\n' "$readiness_containers" \
    | awk 'NF { print; exit }')
  if ! readiness_running=$(docker inspect "$readiness_container" \
      --format '{{.State.Running}}' 2>/dev/null); then
    printf 'unknown\n'
    return 0
  fi
  case "$readiness_running" in
    false) printf 'absent\n'; return 0 ;;
    true) ;;
    *) printf 'unknown\n'; return 0 ;;
  esac
  if docker exec "$readiness_container" python -c '
import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
        raise SystemExit(0 if response.status == 200 else 3)
except urllib.error.HTTPError:
    raise SystemExit(3)
except Exception:
    raise SystemExit(4)
' "$readiness_url" >/dev/null 2>&1; then
    readiness_exit=0
  else
    readiness_exit=$?
  fi
  case "$readiness_exit" in
    0) printf 'ready\n' ;;
    3) printf 'not_ready\n' ;;
    *) printf 'unknown\n' ;;
  esac
}

compose_internal() {
  docker compose --env-file "$INSTALL_DIR/.env" \
    -p "$PROJECT" -f "$COMPOSE_NAME" \
    -f "$CANDIDATE_INTERNAL_OVERRIDE" "$@"
}

compose_candidate_live() {
  docker compose --env-file "$INSTALL_DIR/.env" \
    -p "$PROJECT" -f "$COMPOSE_NAME" "$@"
}

COSIGN_VERSION=v3.0.6
COSIGN_BIN=""
COSIGN_TEMP=""

ensure_cosign() {
  [ -z "$COSIGN_BIN" ] || return 0
  if command -v cosign >/dev/null 2>&1; then
    COSIGN_BIN=$(command -v cosign)
    return 0
  fi
  cosign_os=$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')
  cosign_arch=$(uname -m 2>/dev/null)
  case "$cosign_arch" in
    x86_64|amd64) cosign_arch=amd64 ;;
    arm64|aarch64) cosign_arch=arm64 ;;
    *) fail "当前 CPU 架构缺少受支持的 cosign 验证器；请先安全安装 cosign。" ;;
  esac
  case "$cosign_os-$cosign_arch" in
    darwin-amd64) cosign_sha256=4c3e7af8372d3ca3296e62fa56f23fcbb5721cc6ac1827900d398f110d7cd280 ;;
    darwin-arm64) cosign_sha256=5fadd012ae6381a6a29ff86a7d39aa873878852f1073fc90b15995961ecfb084 ;;
    linux-amd64) cosign_sha256=c956e5dfcac53d52bcf058360d579472f0c1d2d9b69f55209e256fe7783f4c74 ;;
    linux-arm64) cosign_sha256=bedac92e8c3729864e13d4a17048007cfafa79d5deca993a43a90ffe018ef2b8 ;;
    *) fail "当前系统缺少受支持的 cosign 验证器；请先安全安装 cosign。" ;;
  esac
  COSIGN_TEMP=$(mktemp .cosign.XXXXXX) || fail "无法创建 cosign 临时文件"
  cosign_asset="cosign-$cosign_os-$cosign_arch"
  curl -fsSL \
    "https://github.com/sigstore/cosign/releases/download/$COSIGN_VERSION/$cosign_asset" \
    -o "$COSIGN_TEMP" || fail "无法下载固定版本 cosign 验证器"
  if command -v sha256sum >/dev/null 2>&1; then
    cosign_actual=$(sha256sum "$COSIGN_TEMP" | awk '{print $1}')
  elif command -v shasum >/dev/null 2>&1; then
    cosign_actual=$(shasum -a 256 "$COSIGN_TEMP" | awk '{print $1}')
  else
    fail "系统缺少 SHA-256 校验工具；拒绝执行未校验的 cosign"
  fi
  [ "$cosign_actual" = "$cosign_sha256" ] \
    || fail "cosign 固定版本 SHA-256 校验失败"
  chmod 700 "$COSIGN_TEMP"
  COSIGN_BIN=$COSIGN_TEMP
}

verify_release_compose() {
  compose_file=$1
  compose_bundle=$2
  release_identity="https://github.com/SparkHello/Memory_Platform/.github/workflows/docker.yml@refs/tags/$RELEASE"
  "$COSIGN_BIN" verify-blob \
    --bundle "$compose_bundle" \
    --certificate-identity "$release_identity" \
    --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
    "$compose_file" >/dev/null 2>&1 \
    || fail "发布 Compose 的 Sigstore 签名无效"
}

verify_release_signature() {
  signed_image=$1
  release_identity="https://github.com/SparkHello/Memory_Platform/.github/workflows/docker.yml@refs/tags/$RELEASE"
  "$COSIGN_BIN" verify \
    --certificate-identity "$release_identity" \
    --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
    "$signed_image" >/dev/null 2>&1 \
    || fail "发布镜像签名无效或不是由固定 tag 的官方工作流生成"
}

# The split-topology isolation contract (ports, networks, UID, volumes) is
# enforced both in CI and, before any cutover mutation, by the validator
# shipped in the candidate init image.

stage_compose_image_environment() {
  staged_environment=$1
  staged_init_image=$2
  staged_model_image=$3
  staged_memory_image=$4
  staged_source=$5
  awk \
    -v init_image="$staged_init_image" \
    -v model_image="$staged_model_image" \
    -v memory_image="$staged_memory_image" \
    -v credential_dir="./credentials" \
    -v host_uid="$HOST_UID_VALUE" \
    -v host_gid="$HOST_GID_VALUE" \
    -v memory_host="$HOST" \
    -v memory_port="$PORT" \
    -v compose_project="$PROJECT" '
      BEGIN {
        value["MEMORY_PLATFORM_INIT_IMAGE"]=init_image
        value["MEMORY_PLATFORM_MODEL_IMAGE"]=model_image
        value["MEMORY_PLATFORM_MEMORY_IMAGE"]=memory_image
        value["MEMORY_CREDENTIAL_DIR"]=credential_dir
        value["HOST_UID"]=host_uid
        value["HOST_GID"]=host_gid
        value["MEMORY_HOST"]=memory_host
        value["MEMORY_PORT"]=memory_port
        value["COMPOSE_PROJECT_NAME"]=compose_project
        order[1]="MEMORY_PLATFORM_INIT_IMAGE"
        order[2]="MEMORY_PLATFORM_MODEL_IMAGE"
        order[3]="MEMORY_PLATFORM_MEMORY_IMAGE"
        order[4]="MEMORY_CREDENTIAL_DIR"
        order[5]="HOST_UID"
        order[6]="HOST_GID"
        order[7]="MEMORY_HOST"
        order[8]="MEMORY_PORT"
        order[9]="COMPOSE_PROJECT_NAME"
      }
      {
        parsed=$0
        sub(/^[[:space:]]*/, "", parsed)
        if (parsed ~ /^export[[:space:]]+/) {
          sub(/^export[[:space:]]+/, "", parsed)
        }
        equals=index(parsed,"=")
        key=equals ? substr(parsed,1,equals-1) : ""
        sub(/[[:space:]]+$/, "", key)
        if (key=="GATEWAY_API_KEY" || key=="MEMORY_CONSOLE_ADMIN_KEY" ||
            key=="COMPOSE_ENV_FILES" || key=="COMPOSE_DISABLE_ENV_FILE" ||
            key=="COMPOSE_PROFILES" || key=="COMPOSE_FILE" ||
            key=="COMPOSE_PATH_SEPARATOR") next
        if (key in value) {
          if (!(key in seen)) print key "=" value[key]
          seen[key]=1
          next
        }
        print
      }
      END {
        for (position=1; position<=9; position++) {
          key=order[position]
          if (!(key in seen)) print key "=" value[key]
        }
      }
    ' "$staged_source" >"$staged_environment" || return 1
  chmod 600 "$staged_environment" || return 1
}

env_file_value() {
  env_value_file=$1
  env_value_key=$2
  [ -f "$env_value_file" ] || return 0
  awk -v wanted="$env_value_key" '
    {
      parsed=$0
      sub(/^[[:space:]]*/, "", parsed)
      if (parsed ~ /^export[[:space:]]+/) sub(/^export[[:space:]]+/, "", parsed)
      equals=index(parsed,"=")
      key=equals ? substr(parsed,1,equals-1) : ""
      sub(/[[:space:]]+$/, "", key)
      if (key==wanted) {
        value=substr(parsed,equals+1)
        sub(/\r$/, "", value)
        found=1
      }
    }
    END { if (found) print value }
  ' "$env_value_file"
}

env_file_has_key() {
  env_presence_file=$1
  env_presence_key=$2
  [ -f "$env_presence_file" ] || return 1
  awk -v wanted="$env_presence_key" '
    {
      parsed=$0
      sub(/^[[:space:]]*/, "", parsed)
      if (parsed ~ /^export[[:space:]]+/) sub(/^export[[:space:]]+/, "", parsed)
      equals=index(parsed,"=")
      key=equals ? substr(parsed,1,equals-1) : ""
      sub(/[[:space:]]+$/, "", key)
      if (key==wanted) found=1
    }
    END { exit !found }
  ' "$env_presence_file"
}

sha256_stream() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print "sha256:" $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print "sha256:" $1}'
  else
    return 1
  fi
}

sha256_file() {
  [ -f "$1" ] || return 1
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print "sha256:" $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print "sha256:" $1}'
  else
    return 1
  fi
}

managed_config_digest() {
  managed_compose=$1
  managed_environment=$2
  managed_environment_exists=$3
  managed_compose_digest=$(sha256_file "$managed_compose") || return 1
  {
    printf 'version=1\n'
    printf 'compose=%s\n' "$managed_compose_digest"
    printf 'environment_exists=%s\n' "$managed_environment_exists"
    for managed_key in MEMORY_CREDENTIAL_DIR HOST_UID HOST_GID MEMORY_HOST \
      MEMORY_PORT COMPOSE_PROJECT_NAME; do
      managed_value=$(env_file_value "$managed_environment" "$managed_key")
      printf '%s=%s\n' "$managed_key" "$managed_value"
    done
    for removed_key in GATEWAY_API_KEY MEMORY_CONSOLE_ADMIN_KEY \
      COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PROFILES \
      COMPOSE_FILE COMPOSE_PATH_SEPARATOR; do
      if env_file_has_key "$managed_environment" "$removed_key"; then
        printf '%s_present=1\n' "$removed_key"
      else
        printf '%s_present=0\n' "$removed_key"
      fi
    done
  } | sha256_stream
}

normalized_image_digest() {
  case "$1" in
    *@sha256:*) normalized_digest="sha256:${1##*@sha256:}" ;;
    sha256:*) normalized_digest=$1 ;;
    *) return 1 ;;
  esac
  valid_sha256_image_id "$normalized_digest" || return 1
  printf '%s\n' "$normalized_digest"
}

current_service_digest() {
  current_service=$1
  current_env_key=$2
  current_ref=$(env_file_value "$ORIGINAL_ENV_SNAPSHOT" "$current_env_key")
  if [ -n "$current_service" ] && [ "$LAYOUT" = split ]; then
    current_containers=$(compose "$ACTIVE_COMPOSE" ps -aq \
      "$current_service" 2>/dev/null || true)
    current_count=$(printf '%s\n' "$current_containers" \
      | awk 'NF { count++ } END { print count+0 }')
    if [ "$current_count" -eq 1 ]; then
      current_container=$(printf '%s\n' "$current_containers" \
        | awk 'NF { print; exit }')
      current_runtime_ref=$(docker inspect "$current_container" \
        --format '{{.Config.Image}}' 2>/dev/null || true)
      [ -z "$current_runtime_ref" ] || current_ref=$current_runtime_ref
    fi
  fi
  normalized_image_digest "$current_ref" 2>/dev/null || printf '%s\n' '-'
}

run_install_planner() {
  planner_candidate_init=$1
  planner_candidate_model=$2
  planner_candidate_memory=$3
  planner_current_init=$4
  planner_current_model=$5
  planner_current_memory=$6
  planner_candidate_config=$7
  planner_current_config=$8
  if ! planner_line=$(docker run --rm --pull never --network none --read-only \
      --cap-drop ALL --security-opt no-new-privileges:true \
      --user 65534:65534 --entrypoint python "$INIT_IMAGE" \
      /usr/local/libexec/memory-platform/plan_install.py \
      "$LAYOUT" \
      "$planner_candidate_init" "$planner_candidate_model" \
      "$planner_candidate_memory" \
      "$planner_current_init" "$planner_current_model" \
      "$planner_current_memory" \
      "$planner_candidate_config" "$planner_current_config" \
      "$OLD_MEMORY_READINESS" "$OLD_MODEL_READINESS" tsv); then
    fail "候选 init 镜像无法生成安全安装计划"
  fi
  case "$planner_line" in *'
'*) fail "候选安装计划不是单行 typed plan" ;; esac
  planner_tab=$(printf '\t')
  planner_field_count=$(printf '%s\n' "$planner_line" \
    | awk -F "$planner_tab" '{print NF}')
  [ "$planner_field_count" -eq 7 ] \
    || fail "候选安装计划字段数量无效"
  PLAN_VERSION=$(printf '%s\n' "$planner_line" | awk -F "$planner_tab" '{print $1}')
  PLAN_ACTION=$(printf '%s\n' "$planner_line" | awk -F "$planner_tab" '{print $2}')
  PLAN_REASON=$(printf '%s\n' "$planner_line" | awk -F "$planner_tab" '{print $3}')
  PLAN_REPAIR_SCOPE=$(printf '%s\n' "$planner_line" | awk -F "$planner_tab" '{print $4}')
  PLAN_ACCEPT_MEMORY_READINESS=$(printf '%s\n' "$planner_line" | awk -F "$planner_tab" '{print $5}')
  PLAN_ACCEPT_MODEL_READINESS=$(printf '%s\n' "$planner_line" | awk -F "$planner_tab" '{print $6}')
  PLAN_ACCEPT_HOST_READINESS=$(printf '%s\n' "$planner_line" | awk -F "$planner_tab" '{print $7}')
  [ "$PLAN_VERSION" = 1 ] || fail "候选安装计划版本无效"
  case "$PLAN_ACTION" in noop|repair|upgrade) ;; *) fail "候选安装计划 action 无效" ;; esac
  case "$PLAN_REASON" in
    fresh_install|image_change|managed_config_change|image_and_config_change|already_current|service_repair) ;;
    *) fail "候选安装计划 reason 无效" ;;
  esac
  case "$PLAN_REPAIR_SCOPE" in none|memory|model|both) ;; *) fail "候选安装计划 repair scope 无效" ;; esac
  for planner_gate in "$PLAN_ACCEPT_MEMORY_READINESS" \
    "$PLAN_ACCEPT_MODEL_READINESS" "$PLAN_ACCEPT_HOST_READINESS"; do
    case "$planner_gate" in 0|1) ;; *) fail "候选安装计划 acceptance 无效" ;; esac
  done
  if [ "$PLAN_ACTION" = repair ]; then
    [ "$PLAN_REPAIR_SCOPE" != none ] || fail "repair 安装计划缺少目标服务"
  else
    [ "$PLAN_REPAIR_SCOPE" = none ] || fail "非 repair 安装计划包含目标服务"
  fi
}

restore_original_environment() {
  if [ "$OLD_ENV_EXISTS" = 1 ]; then
    restored_environment=$(mktemp .env.rollback.XXXXXX) || return 1
    cp "$ORIGINAL_ENV_SNAPSHOT" "$restored_environment" \
      && chmod 600 "$restored_environment" \
      && mv "$restored_environment" .env \
      || { rm -f "$restored_environment"; return 1; }
  else
    rm -f .env || return 1
  fi
}

create_cutover_journal() {
  [ "$LAYOUT" != fresh ] || return 0
  [ ! -e "$CUTOVER_JOURNAL" ] || fail "已有未恢复的升级事务 journal"
  cutover_pending="$CUTOVER_JOURNAL.pending.$$"
  [ ! -e "$cutover_pending" ] || fail "升级事务临时目录已存在"
  mkdir -m 700 "$cutover_pending" || fail "无法创建升级事务 journal"
  # Split upgrades create their single quiesced backup only after the old
  # stack stops writing; until then the journal records `pending`, which is
  # replaced through update_cutover_backup_reference before any data changes.
  cutover_backup=${BACKUP_PATH##*/}
  [ -n "$cutover_backup" ] || cutover_backup=pending
  if ! cp "$OLD_COMPOSE_BACKUP" "$cutover_pending/old-compose.yml" \
    || ! cp "$ORIGINAL_ENV_SNAPSHOT" "$cutover_pending/old.env"; then
    rm -f "$cutover_pending/old-compose.yml" "$cutover_pending/old.env"
    rmdir "$cutover_pending" 2>/dev/null || true
    fail "无法保存升级事务快照"
  fi
  printf '%s\n' \
    'version=2' \
    "project=$PROJECT" \
    "layout=$LAYOUT" \
    "backup=$cutover_backup" \
    "old_init_image=$OLD_INIT_IMAGE_VALUE" \
    "old_model_image=$OLD_MODEL_IMAGE_VALUE" \
    "old_memory_image=$OLD_MEMORY_IMAGE_VALUE" \
    "old_env_exists=$OLD_ENV_EXISTS" \
    "publish_host=$HOST" \
    "publish_port=$PORT" \
    >"$cutover_pending/metadata" \
    || fail "无法写入升级事务 metadata"
  printf 'prepared\n' >"$cutover_pending/phase" \
    || fail "无法写入升级事务阶段"
  chmod 600 "$cutover_pending"/*
  mv "$cutover_pending" "$CUTOVER_JOURNAL" \
    || fail "无法原子发布升级事务 journal"
  # POSIX shell has no portable directory-fsync primitive. sync makes the
  # journal and its same-filesystem rename durable before the old stack stops.
  sync
}

mark_cutover_data_may_change() {
  write_cutover_phase data_may_change
}

write_cutover_phase() {
  next_cutover_phase=$1
  [ "$LAYOUT" != fresh ] || return 0
  case "$next_cutover_phase" in data_may_change|committed) ;; *) return 1 ;; esac
  cutover_phase=$(mktemp "$CUTOVER_JOURNAL/.phase.XXXXXX") \
    || fail "无法更新升级事务阶段"
  printf '%s\n' "$next_cutover_phase" >"$cutover_phase"
  chmod 600 "$cutover_phase"
  mv "$cutover_phase" "$CUTOVER_JOURNAL/phase" \
    || fail "无法原子更新升级事务阶段"
  sync
}

finalize_cutover_journal() {
  [ "$LAYOUT" != fresh ] || return 0
  commit_cutover_journal
}

mark_cutover_committed() {
  [ "$LAYOUT" != fresh ] || return 0
  write_cutover_phase committed
}

complete_committed_cutover() {
  [ "$LAYOUT" != fresh ] || return 0
  remove_cutover_journal
}

service_in_compose() {
  compose "$1" config --services 2>/dev/null | awk -v wanted="$2" '$0==wanted {found=1} END {exit !found}'
}

if ! command -v lsof >/dev/null 2>&1 && ! command -v nc >/dev/null 2>&1; then
  say "提示：系统缺少 lsof 和 nc，无法预检测端口占用；若安装后无法访问，请检查端口冲突。"
fi

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
  [ -f "$COMPOSE_NAME" ] || return 1
  for service in model-gateway memory-gateway memory-platform; do
    published=$(compose "$COMPOSE_NAME" port "$service" 2026 2>/dev/null || true)
    [ -n "$published" ] && [ "${published##*:}" = "$1" ] && return 0
  done
  return 1
}

PORT=${REQUESTED_MEMORY_PORT:-$(compose_env_value MEMORY_PORT)}
PORT=${PORT:-2026}
case "$PORT" in *[!0-9]*|'') fail "MEMORY_PORT 必须是 1–65535 的整数" ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || fail "MEMORY_PORT 超出范围"
REQUESTED_PORT_BEFORE_SKIP=$PORT
if port_in_use "$PORT" && ! compose_owns_port "$PORT"; then
  [ -z "$REQUESTED_MEMORY_PORT" ] || fail "指定端口 $PORT 已被占用"
  candidate=$((PORT+1))
  while port_in_use "$candidate"; do
    candidate=$((candidate+1))
    [ "$candidate" -le 2099 ] || fail "2026–2099 均被占用"
  done
  PORT=$candidate
fi
if [ "$PORT" != "$REQUESTED_PORT_BEFORE_SKIP" ]; then
  say "提示：端口 $REQUESTED_PORT_BEFORE_SKIP 已被占用，本次改用 ${PORT}。"
  say "      后续文档和示例中的 $REQUESTED_PORT_BEFORE_SKIP 请替换为 ${PORT}。"
fi
HOST=${REQUESTED_MEMORY_HOST:-$(compose_env_value MEMORY_HOST)}
HOST=${HOST:-127.0.0.1}
valid_host_ip "$HOST" \
  || fail "MEMORY_HOST 必须是本机可绑定的 IPv4 地址（如 127.0.0.1、0.0.0.0 或局域网 IP）"

ACTIVE_COMPOSE=""
LAYOUT="fresh"
OLD_MEMORY_CONTAINER=""
if [ -f "$COMPOSE_NAME" ]; then
  ACTIVE_COMPOSE=$COMPOSE_NAME
  if service_in_compose "$ACTIVE_COMPOSE" memory-gateway; then
    LAYOUT=split
    OLD_MEMORY_CONTAINER=$(compose "$ACTIVE_COMPOSE" ps -aq memory-gateway 2>/dev/null || true)
  elif service_in_compose "$ACTIVE_COMPOSE" memory-platform; then
    # Legacy single-volume installs migrate through the standalone one-shot
    # tool; this installer only handles fresh and split layouts.
    fail "检测到旧单卷（legacy）布局，本安装器不再内嵌一次性迁移；旧服务与数据未修改。请先运行迁移工具（与 install.sh 同一 release）：curl -fsSL \"$REPO_RAW/deploy/legacy_cutover.py\" -o legacy-cutover.py && python3 legacy-cutover.py，完成旧单卷到四卷的迁移后再重跑本安装命令。"
  fi
fi
if [ "$LAYOUT" = fresh ] && docker volume ls \
  --filter "label=com.docker.compose.project=$PROJECT" \
  --filter "label=com.docker.compose.volume=memory-platform-data" \
  --format '{{.Name}}' | awk 'NF {found=1} END {exit !found}'; then
  fail "发现旧数据卷但安装目录没有可验证的旧 Compose；拒绝猜测迁移。若是旧单卷（legacy）数据，请先运行 $REPO_RAW/deploy/legacy_cutover.py 对应的一次性迁移工具。"
fi
# Lost install directory with the four split volumes still present: a fresh
# run would reach credential acceptance and fail with an unactionable error.
# Detect it up front and tell the user exactly how to reattach.
if [ "$LAYOUT" = fresh ]; then
  SPLIT_VOLUMES_PRESENT=1
  for split_key in memory-data memory-secrets model-data model-secrets; do
    [ -n "$(journal_volume_for "$PROJECT" "$split_key")" ] \
      || { SPLIT_VOLUMES_PRESENT=0; break; }
  done
  if [ "$SPLIT_VOLUMES_PRESENT" = 1 ] \
    && { { [ ! -s credentials/gateway.txt ] && [ ! -s credentials/gateway.key ]; } \
      || { [ ! -s credentials/admin.txt ] && [ ! -s credentials/admin.key ]; }; }; then
    fail "检测到 project '$PROJECT' 的四个数据卷仍在，但 $INSTALL_DIR/credentials/ 缺少 gateway/admin 凭据（优先 .txt，兼容旧版 .key）。数据没有丢：把原安装目录里的 credentials/gateway.txt（或 gateway.key）和 credentials/admin.txt（或 admin.key）放回 $INSTALL_DIR/credentials/ 后重跑同一条安装命令即可接回旧数据。若两枚密钥确实遗失，参见 docs/stack-operations.md 的密钥重置章节。"
  fi
fi

# Preserve the exact images that are actually running before candidate image
# references are written to .env.  Restoring only the old Compose file is not
# sufficient: its ${MEMORY_PLATFORM_*_IMAGE:-...} expressions would otherwise
# resolve to the newly downloaded candidate digests during rollback.
OLD_INIT_IMAGE_VALUE=$(compose_env_value MEMORY_PLATFORM_INIT_IMAGE)
OLD_MODEL_IMAGE_VALUE=$(compose_env_value MEMORY_PLATFORM_MODEL_IMAGE)
OLD_MEMORY_IMAGE_VALUE=$(compose_env_value MEMORY_PLATFORM_MEMORY_IMAGE)
service_image_id() {
  service=$1
  service_container=$(compose "$ACTIVE_COMPOSE" ps -aq "$service" 2>/dev/null || true)
  [ -n "$service_container" ] || return 0
  docker inspect "$service_container" --format '{{.Image}}' 2>/dev/null || true
}
if [ "$LAYOUT" = split ]; then
  actual_image=$(service_image_id stack-init)
  [ -z "$actual_image" ] || OLD_INIT_IMAGE_VALUE=$actual_image
  actual_image=$(service_image_id model-gateway)
  [ -z "$actual_image" ] || OLD_MODEL_IMAGE_VALUE=$actual_image
  actual_image=$(service_image_id memory-gateway)
  [ -z "$actual_image" ] || OLD_MEMORY_IMAGE_VALUE=$actual_image
fi

BACKUP_PATH=""
OLD_COMPOSE_BACKUP=""
BACKUP_RETENTION=${MEMORY_BACKUP_RETENTION:-5}
case "$BACKUP_RETENTION" in *[!0-9]*|'') fail "MEMORY_BACKUP_RETENTION 必须是 1–50 的整数" ;; esac
[ "$BACKUP_RETENTION" -ge 1 ] && [ "$BACKUP_RETENTION" -le 50 ] \
  || fail "MEMORY_BACKUP_RETENTION 必须是 1–50 的整数"

# Retention counts one quiesced archive per upgrade, so MEMORY_BACKUP_RETENTION
# now means "keep the last N upgrades' backups".
prune_host_backups() {
  find "$INSTALL_DIR/backups" -maxdepth 1 -type f -name 'pre-upgrade-*.zip' -print \
    | sort -r \
    | awk -v keep="$BACKUP_RETENTION" 'NR > keep { print }' \
    | while IFS= read -r stale; do
        case "$stale" in
          "$INSTALL_DIR"/backups/pre-upgrade-*.zip)
            rm -f -- "$stale" "${stale%.zip}.compose.yml"
            ;;
          *) fail "拒绝清理非预期备份路径" ;;
        esac
      done
}

if [ "$LAYOUT" = split ]; then
  if [ -z "$OLD_MEMORY_CONTAINER" ]; then
    identity_match=$(docker volume ls \
      --filter "label=com.docker.compose.project=$PROJECT" \
      --filter "label=com.docker.compose.volume=memory-data" \
      --format '{{.Name}}' | awk 'NF {print; exit}')
    [ -n "$identity_match" ] \
      || fail "现有 Compose 没有同 project 的容器或数据卷；拒绝在空 project 上迁移"
  fi
fi

say "==> 下载 $RELEASE Compose 并校验"
CANDIDATE_COMPOSE=$(mktemp ".$COMPOSE_NAME.candidate.XXXXXX") || fail "无法创建候选文件"
CANDIDATE_ENV=""
CANDIDATE_INTERNAL_OVERRIDE=""
CANDIDATE_RENDERED_JSON=""
CANDIDATE_EMPTY_ENV=$(mktemp .env.empty.XXXXXX) || fail "无法创建候选空环境文件"
chmod 600 "$CANDIDATE_EMPTY_ENV"
COMPOSE_BUNDLE=""
cleanup() {
  [ -z "${CANDIDATE_COMPOSE:-}" ] || rm -f "$CANDIDATE_COMPOSE"
  [ -z "${CANDIDATE_ENV:-}" ] || rm -f "$CANDIDATE_ENV"
  [ -z "${CANDIDATE_INTERNAL_OVERRIDE:-}" ] || rm -f "$CANDIDATE_INTERNAL_OVERRIDE"
  [ -z "${CANDIDATE_RENDERED_JSON:-}" ] || rm -f "$CANDIDATE_RENDERED_JSON"
  [ -z "${CANDIDATE_EMPTY_ENV:-}" ] || rm -f "$CANDIDATE_EMPTY_ENV"
  [ -z "${ORIGINAL_ENV_SNAPSHOT:-}" ] || rm -f "$ORIGINAL_ENV_SNAPSHOT"
  [ -z "${COSIGN_TEMP:-}" ] || rm -f "$COSIGN_TEMP"
  [ -z "${COMPOSE_BUNDLE:-}" ] || rm -f "$COMPOSE_BUNDLE"
  release_install_lock || true
}
trap cleanup EXIT
curl -fsSL "$REPO_RAW/deploy/$COMPOSE_NAME" -o "$CANDIDATE_COMPOSE" \
  || fail "下载发布版 Compose 失败；旧服务未变。raw.githubusercontent.com 在部分网络不可达：可先设置代理再重跑，例如 HTTPS_PROXY=http://127.0.0.1:7890 MEMORY_PLATFORM_VERSION=$RELEASE sh install-memory-platform.sh"
chmod 600 "$CANDIDATE_COMPOSE"
if [ "$VERIFY_SIGNATURES" = 1 ]; then
  COMPOSE_BUNDLE=$(mktemp ".$COMPOSE_NAME.sigstore.XXXXXX") \
    || fail "无法创建 Compose 签名临时文件"
  curl -fsSL \
    "https://github.com/SparkHello/Memory_Platform/releases/download/$RELEASE/$COMPOSE_NAME.sigstore.json" \
    -o "$COMPOSE_BUNDLE" \
    || fail "下载发布 Compose 的 Sigstore bundle 失败"
  chmod 600 "$COMPOSE_BUNDLE"
  say "==> 验证发布 Compose 的 Sigstore 签名"
  ensure_cosign
  verify_release_compose "$CANDIDATE_COMPOSE" "$COMPOSE_BUNDLE"
else
  say "    已按默认跳过 Sigstore 签名验证；镜像仍按不可变 digest 固定。"
  say "    如需启用，设 MEMORY_VERIFY_SIGNATURES=1 重跑同一条安装命令。"
fi

INIT_TAG="$IMAGE_REGISTRY/sparkhello/memory-platform-init:$RELEASE"
MODEL_TAG="$IMAGE_REGISTRY/sparkhello/memory-platform-model:$RELEASE"
MEMORY_TAG="$IMAGE_REGISTRY/sparkhello/memory-platform-memory:$RELEASE"
[ "$IMAGE_REGISTRY" = ghcr.io ] \
  || say "    已用 MEMORY_IMAGE_REGISTRY=$IMAGE_REGISTRY 覆盖镜像源；仓库路径与 digest 固定不变。"
compose_candidate_with_images "$CANDIDATE_COMPOSE" "$INIT_TAG" "$MODEL_TAG" "$MEMORY_TAG" \
  config >/dev/null || fail "候选 Compose 语法无效"

say "==> 拉取三枚 semver 发布镜像"
compose_candidate_with_images "$CANDIDATE_COMPOSE" "$INIT_TAG" "$MODEL_TAG" "$MEMORY_TAG" \
  pull || fail "镜像拉取失败；旧服务未变。GHCR 在部分网络不可达：可设 MEMORY_IMAGE_REGISTRY=<GHCR 镜像站域名> 重跑同一条安装命令，或为 Docker 配置代理（Docker Desktop → Settings → Resources → Proxies）"

digest_ref() {
  tag=$1 repository=${1%:*}
  docker image inspect "$tag" --format '{{range .RepoDigests}}{{println .}}{{end}}' 2>/dev/null \
    | awk -v prefix="$repository@sha256:" 'index($0,prefix)==1 {print; exit}'
}
INIT_IMAGE=$(digest_ref "$INIT_TAG")
MODEL_IMAGE=$(digest_ref "$MODEL_TAG")
MEMORY_IMAGE=$(digest_ref "$MEMORY_TAG")
[ -n "$INIT_IMAGE" ] && [ -n "$MODEL_IMAGE" ] && [ -n "$MEMORY_IMAGE" ] \
  || fail "无法把发布镜像解析为不可变 digest"
if [ "$VERIFY_SIGNATURES" = 1 ]; then
  say "==> 验证三枚镜像的 Sigstore 发布签名"
  verify_release_signature "$INIT_IMAGE"
  verify_release_signature "$MODEL_IMAGE"
  verify_release_signature "$MEMORY_IMAGE"
fi
CANDIDATE_ENV=$(mktemp .env.candidate.XXXXXX) || fail "无法创建候选环境文件"
stage_compose_image_environment \
  "$CANDIDATE_ENV" "$INIT_IMAGE" "$MODEL_IMAGE" "$MEMORY_IMAGE" \
  "$ORIGINAL_ENV_SNAPSHOT" \
  || fail "无法生成候选环境文件"
CANDIDATE_INTERNAL_OVERRIDE=$(mktemp .compose.internal.XXXXXX.yml) \
  || fail "无法创建本地验收 override"
printf '%s\n' \
  'services:' \
  '  memory-gateway:' \
  '    ports: !reset []' \
  >"$CANDIDATE_INTERNAL_OVERRIDE" \
  || fail "无法写入本地验收 override"
chmod 600 "$CANDIDATE_INTERNAL_OVERRIDE"

say "==> 用候选 init 镜像校验 public/internal 安全拓扑"
validate_candidate_topology public
validate_candidate_topology internal

OLD_MEMORY_READINESS=absent
OLD_MODEL_READINESS=absent
if [ "$LAYOUT" = split ]; then
  OLD_MEMORY_READINESS=$(existing_service_readiness \
    memory-gateway http://127.0.0.1:2026/readyz)
  OLD_MODEL_READINESS=$(existing_service_readiness \
    model-gateway http://127.0.0.1:2030/readyz)
fi
case "$OLD_MEMORY_READINESS:$OLD_MODEL_READINESS" in
  *unknown*)
    fail "无法可靠建立旧服务 readiness 基线（Memory=${OLD_MEMORY_READINESS}, Model=${OLD_MODEL_READINESS}）；旧服务未停机"
    ;;
esac

CANDIDATE_INIT_DIGEST=$(normalized_image_digest "$INIT_IMAGE") \
  || fail "候选 init 镜像 digest 无效"
CANDIDATE_MODEL_DIGEST=$(normalized_image_digest "$MODEL_IMAGE") \
  || fail "候选 model 镜像 digest 无效"
CANDIDATE_MEMORY_DIGEST=$(normalized_image_digest "$MEMORY_IMAGE") \
  || fail "候选 memory 镜像 digest 无效"
CANDIDATE_CONFIG_DIGEST=$(managed_config_digest \
  "$CANDIDATE_COMPOSE" "$CANDIDATE_ENV" 1) \
  || fail "无法计算候选 managed config digest"
CURRENT_INIT_DIGEST=-
CURRENT_MODEL_DIGEST=-
CURRENT_MEMORY_DIGEST=-
CURRENT_CONFIG_DIGEST=-
if [ "$LAYOUT" = split ]; then
  CURRENT_INIT_DIGEST=$(current_service_digest "" MEMORY_PLATFORM_INIT_IMAGE)
  CURRENT_MODEL_DIGEST=$(current_service_digest \
    model-gateway MEMORY_PLATFORM_MODEL_IMAGE)
  CURRENT_MEMORY_DIGEST=$(current_service_digest \
    memory-gateway MEMORY_PLATFORM_MEMORY_IMAGE)
  CURRENT_CONFIG_DIGEST=$(managed_config_digest \
    "$ACTIVE_COMPOSE" "$ORIGINAL_ENV_SNAPSHOT" "$OLD_ENV_EXISTS") \
    || fail "无法计算当前 managed config digest"
fi
say "==> 生成 typed 安装计划"
run_install_planner \
  "$CANDIDATE_INIT_DIGEST" "$CANDIDATE_MODEL_DIGEST" \
  "$CANDIDATE_MEMORY_DIGEST" \
  "$CURRENT_INIT_DIGEST" "$CURRENT_MODEL_DIGEST" "$CURRENT_MEMORY_DIGEST" \
  "$CANDIDATE_CONFIG_DIGEST" "$CURRENT_CONFIG_DIGEST"
HOST_PROBE=$(host_probe_address "$HOST")

resolve_credential() {
  # $1 = role: gateway | admin
  if [ -s "$INSTALL_DIR/credentials/$1.txt" ]; then
    printf '%s\n' "$INSTALL_DIR/credentials/$1.txt"
  elif [ -s "$INSTALL_DIR/credentials/$1.key" ]; then
    printf '%s\n' "$INSTALL_DIR/credentials/$1.key"
  else
    return 1
  fi
}

private_credential_mode() {
  if stat -c '%a' "$1" >/dev/null 2>&1; then
    [ "$(stat -c '%a' "$1")" = 600 ]
  elif stat -f '%Lp' "$1" >/dev/null 2>&1; then
    [ "$(stat -f '%Lp' "$1")" = 600 ]
  else
    return 1
  fi
}

private_credential_directory_mode() {
  if stat -c '%a' "$1" >/dev/null 2>&1; then
    [ "$(stat -c '%a' "$1")" = 700 ]
  elif stat -f '%Lp' "$1" >/dev/null 2>&1; then
    [ "$(stat -f '%Lp' "$1")" = 700 ]
  else
    return 1
  fi
}

live_http_check() {
  live_service=$1
  live_url=$2
  compose_candidate_live exec -T "$live_service" python -c \
    'import sys,urllib.request; response=urllib.request.urlopen(sys.argv[1],timeout=3); raise SystemExit(0 if response.status==200 else 1)' \
    "$live_url" >/dev/null 2>&1
}

wait_live_http() {
  wait_live_service=$1
  wait_live_url=$2
  wait_live_limit=$3
  wait_live_count=0
  until live_http_check "$wait_live_service" "$wait_live_url"; do
    wait_live_count=$((wait_live_count+1))
    [ "$wait_live_count" -lt "$wait_live_limit" ] || return 1
    sleep 1
  done
}

accept_existing_stack() {
  wait_live_http memory-gateway http://127.0.0.1:2026/health 180 \
    || return 1
  wait_live_http model-gateway http://127.0.0.1:2030/health 180 \
    || return 1
  if [ "$PLAN_ACCEPT_MEMORY_READINESS" = 1 ]; then
    wait_live_http memory-gateway http://127.0.0.1:2026/readyz 90 \
      || return 1
  fi
  if [ "$PLAN_ACCEPT_MODEL_READINESS" = 1 ]; then
    wait_live_http model-gateway http://127.0.0.1:2030/readyz 90 \
      || return 1
  fi
  existing_gateway_credential=$(resolve_credential gateway) || return 1
  existing_admin_credential=$(resolve_credential admin) || return 1
  private_credential_directory_mode "$INSTALL_DIR/credentials" || return 1
  private_credential_mode "$existing_gateway_credential" || return 1
  private_credential_mode "$existing_admin_credential" || return 1
  compose_candidate_live exec -T memory-gateway python -c \
    'import sys,urllib.request; key=sys.stdin.buffer.readline().strip().decode("ascii"); request=urllib.request.Request("http://127.0.0.1:2026/auth/tokens",headers={"Authorization":"Bearer "+key}); response=urllib.request.urlopen(request,timeout=5); raise SystemExit(0 if response.status==200 else 1)' \
    <"$existing_gateway_credential" >/dev/null 2>&1 || return 1
  compose_candidate_live exec -T model-gateway python -c \
    'import sys,urllib.request; key=sys.stdin.buffer.readline().strip().decode("ascii"); request=urllib.request.Request("http://127.0.0.1:2030/admin/configuration",headers={"Authorization":"Bearer "+key}); response=urllib.request.urlopen(request,timeout=5); raise SystemExit(0 if response.status==200 else 1)' \
    <"$existing_admin_credential" >/dev/null 2>&1 || return 1
  existing_wait=0
  until curl -fsS "http://$HOST_PROBE:$PORT/health" >/dev/null 2>&1; do
    existing_wait=$((existing_wait+1))
    [ "$existing_wait" -lt 180 ] || return 1
    sleep 1
  done
  if [ "$PLAN_ACCEPT_HOST_READINESS" = 1 ]; then
    existing_wait=0
    until curl -fsS "http://$HOST_PROBE:$PORT/readyz" >/dev/null 2>&1; do
      existing_wait=$((existing_wait+1))
      [ "$existing_wait" -lt 90 ] || return 1
      sleep 1
    done
  fi
  existing_memory=$(compose_candidate_live ps -q memory-gateway 2>/dev/null || true)
  existing_model=$(compose_candidate_live ps -q model-gateway 2>/dev/null || true)
  existing_memory_port=$(compose_candidate_live port \
    memory-gateway 2026 2>/dev/null || true)
  existing_model_ports=""
  [ -z "$existing_model" ] \
    || existing_model_ports=$(docker port "$existing_model" 2>/dev/null \
      | awk 'NF {print; exit}' || true)
  [ -n "$existing_memory" ] && [ "${existing_memory_port##*:}" = "$PORT" ] \
    && [ -z "$existing_model_ports" ]
}

report_existing_plan_success() {
  say ""
  say "Memory Platform ${RELEASE} 已通过 ${PLAN_ACTION} 验收（${PLAN_REASON}）"
  say "  Web Console:  http://$HOST_PROBE:$PORT/ui/"
  say "  Client URL:   http://$HOST_PROBE:$PORT/v1"
  say "  Model:        memory-auto"
  say "  Console token: $(resolve_credential gateway)"
  say "  Admin key:    $(resolve_credential admin)"
  say "密钥值没有进入本脚本输出、Compose 环境或 Docker 日志。"
  if [ "${MEMORY_NO_OPEN:-0}" != 1 ]; then
    if command -v open >/dev/null 2>&1; then
      open "http://$HOST_PROBE:$PORT/ui/" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1 \
      && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
      xdg-open "http://$HOST_PROBE:$PORT/ui/" >/dev/null 2>&1 || true
    fi
  fi
}

if [ "$PLAN_ACTION" = noop ]; then
  say "==> 当前 digest、managed config 与健康状态已满足目标；跳过 cutover"
  accept_existing_stack || fail "当前栈未通过 noop acceptance；未执行停机或备份"
  report_existing_plan_success
  exit 0
fi

if [ "$PLAN_ACTION" = repair ]; then
  say "==> 仅修复退化服务（${PLAN_REPAIR_SCOPE}），不创建全量备份或停止整栈"
  case "$PLAN_REPAIR_SCOPE" in
    model|both)
      compose_candidate_live up -d --no-deps --force-recreate model-gateway \
        || fail "Model Gateway 定向 repair 失败；未停止整栈"
      ;;
  esac
  case "$PLAN_REPAIR_SCOPE" in
    memory|both)
      compose_candidate_live up -d --no-deps --force-recreate memory-gateway \
        || fail "Memory Gateway 定向 repair 失败；未停止整栈"
      ;;
  esac
  accept_existing_stack || fail "repair 后栈未通过 typed acceptance；未执行全量回滚"
  report_existing_plan_success
  exit 0
fi

if [ "$LAYOUT" = split ]; then
  say "==> 保存旧 Compose 快照"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$
  OLD_COMPOSE_BACKUP="$INSTALL_DIR/backups/pre-upgrade-$stamp.compose.yml"
  cp "$ACTIVE_COMPOSE" "$OLD_COMPOSE_BACKUP"
  chmod 600 "$OLD_COMPOSE_BACKUP"
  # Exactly one data backup is created per upgrade: the quiesced snapshot
  # taken right after the old stack stops writing.
  BACKUP_PATH=""
  say "    升级备份将在旧服务停写后创建（每次升级一份一致性备份）"
fi

volume_for() {
  docker volume ls --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.volume=$1" --format '{{.Name}}' | awk 'NF {print; exit}'
}

update_cutover_backup_reference() {
  updated_backup=$1
  updated_name=${updated_backup##*/}
  case "$updated_name" in pre-upgrade-*.zip) ;; *) return 1 ;; esac
  [ -s "$updated_backup" ] || return 1
  updated_metadata=$(mktemp "$CUTOVER_JOURNAL/.metadata.XXXXXX") || return 1
  if ! awk -v backup="$updated_name" '
      BEGIN { seen=0 }
      index($0,"backup=")==1 {
        if (!seen) print "backup=" backup
        seen=1
        next
      }
      { print }
      END { if (!seen) print "backup=" backup }
    ' "$CUTOVER_JOURNAL/metadata" >"$updated_metadata" \
    || ! chmod 600 "$updated_metadata" \
    || ! mv "$updated_metadata" "$CUTOVER_JOURNAL/metadata"; then
    rm -f "$updated_metadata"
    return 1
  fi
  sync
}

create_quiesced_backup() {
  quiesced_stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$-quiesced
  quiesced_name="pre-upgrade-$quiesced_stamp.zip"
  quiesced_path="$INSTALL_DIR/backups/$quiesced_name"
  quiesced_runner="${PROJECT}-cutover-backup-$$"
  # A previous crashed runner is owned by the still-active journal/lock and is
  # never silently reused.
  [ -z "$(docker ps -aq --filter "name=^/$quiesced_runner$")" ] || return 1

  quiesced_memory_data=$(volume_for memory-data)
  quiesced_memory_secrets=$(volume_for memory-secrets)
  quiesced_model_data=$(volume_for model-data)
  [ -n "$quiesced_memory_data" ] \
    && [ -n "$quiesced_memory_secrets" ] \
    && [ -n "$quiesced_model_data" ] \
    && [ -n "$OLD_INIT_IMAGE_VALUE" ] || return 1
  if ! docker run --name "$quiesced_runner" --network none --read-only \
    --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    -e MEMGW_HOME=/data/config \
    -e MEMGW_SETTINGS_PATH=/secrets/settings.env \
    -e MEMGW_PROJECT_ROOT=/app/services/memory-gateway \
    -e MODEL_GATEWAY_HOME=/model-data \
    --mount "type=volume,source=$quiesced_memory_data,target=/data" \
    --mount "type=volume,source=$quiesced_memory_secrets,target=/secrets" \
    --mount "type=volume,source=$quiesced_model_data,target=/model-data" \
    --tmpfs /tmp:rw,noexec,nosuid,size=134217728 \
    --entrypoint memgw "$OLD_INIT_IMAGE_VALUE" \
    --home /data/config --project-root /app/services/memory-gateway \
    stack backup --model-gateway-home /model-data \
    --output "/data/$quiesced_name" >/dev/null; then
    docker rm -f "$quiesced_runner" >/dev/null 2>&1 || true
    return 1
  fi
  cleanup_image=$OLD_INIT_IMAGE_VALUE
  cleanup_volume=$quiesced_memory_data
  # Creation stays on the old runtime so it can read the old schema. The
  # candidate validator is authoritative for whether the archive is accepted
  # by the release we are about to install.
  quiesced_verify_image=$INIT_IMAGE

  if ! docker cp "$quiesced_runner:/data/$quiesced_name" "$quiesced_path" \
    >/dev/null 2>&1 \
    || [ ! -s "$quiesced_path" ]; then
    docker rm -f "$quiesced_runner" >/dev/null 2>&1 || true
    rm -f "$quiesced_path"
    return 1
  fi
  docker rm -f "$quiesced_runner" >/dev/null 2>&1 || return 1
  chmod 600 "$quiesced_path" || return 1
  # Re-verify the finished archive with the same manifest/hash/schema/SQLite
  # validator used by legacy cutover and the Windows installer.
  if ! docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --mount "type=bind,source=$quiesced_path,target=/backup/verify.zip,readonly" \
    --mount type=volume,target=/tmp,volume-nocopy \
    --entrypoint python "$quiesced_verify_image" \
    /usr/local/libexec/memory-platform/verify_backup.py \
    /backup/verify.zip >/dev/null 2>&1; then
    rm -f "$quiesced_path"
    return 1
  fi
  docker run --rm --network none --read-only --user 10001:10001 \
    --cap-drop ALL \
    --mount "type=volume,source=$cleanup_volume,target=/data" \
    --entrypoint python "$cleanup_image" \
    -c 'import os,sys; os.unlink(sys.argv[1])' "/data/$quiesced_name" \
    >/dev/null 2>&1 || return 1

  BACKUP_NAME=$quiesced_name
  BACKUP_PATH=$quiesced_path
  update_cutover_backup_reference "$BACKUP_PATH"
}

rollback() {
  [ "$LAYOUT" != fresh ] || return 1
  say "==> 新版本未通过验收，恢复旧 Compose"
  compose "$COMPOSE_NAME" stop >/dev/null 2>&1 || true
  if [ -n "$BACKUP_PATH" ]; then
    memory_data=$(volume_for memory-data)
    memory_secrets=$(volume_for memory-secrets)
    model_data=$(volume_for model-data)
    if [ -n "$memory_data" ] && [ -n "$memory_secrets" ] && [ -n "$model_data" ]; then
      rollback_init_image=${OLD_INIT_IMAGE_VALUE:-$INIT_IMAGE}
      docker run --rm --network none --read-only \
        --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
        -e RESTORE_ARCHIVE=/backup/restore.zip \
        --mount "type=volume,source=$memory_data,target=/data" \
        --mount "type=volume,source=$memory_secrets,target=/secrets" \
        --mount "type=volume,source=$model_data,target=/model-data" \
        --mount "type=bind,source=$BACKUP_PATH,target=/backup/restore.zip,readonly" \
        --tmpfs /tmp:rw,noexec,nosuid,size=134217728 \
        --entrypoint python "$rollback_init_image" \
        /usr/local/libexec/memory-platform/restore_split.py >/dev/null \
        || return 1
    else
      return 1
    fi
  fi
  temporary=$(mktemp ".$COMPOSE_NAME.rollback.XXXXXX") || return 1
  cp "$OLD_COMPOSE_BACKUP" "$temporary" || return 1
  chmod 600 "$temporary"
  mv "$temporary" "$COMPOSE_NAME" || return 1
  restore_original_environment || return 1
  compose_with_images "$COMPOSE_NAME" \
    "$OLD_INIT_IMAGE_VALUE" "$OLD_MODEL_IMAGE_VALUE" "$OLD_MEMORY_IMAGE_VALUE" \
    up -d --pull never >/dev/null || return 1
  finalize_cutover_journal || return 1
  return 0
}

create_cutover_journal
if [ "$LAYOUT" != fresh ]; then
  if ! compose "$ACTIVE_COMPOSE" stop >/dev/null; then
    # stop may have partially stopped a multi-service stack.  Candidate image
    # refs have not touched .env, and explicit old refs prevent a repulled tag
    # from changing what this recovery start runs.
    if compose_with_images "$ACTIVE_COMPOSE" \
      "$OLD_INIT_IMAGE_VALUE" "$OLD_MODEL_IMAGE_VALUE" "$OLD_MEMORY_IMAGE_VALUE" \
      up -d --pull never >/dev/null 2>&1; then
      finalize_cutover_journal || true
    fi
    fail "无法停止旧服务；未开始迁移"
  fi
  say "==> 旧服务已停写，创建并复验最终一致性备份"
  if ! create_quiesced_backup; then
    if compose_with_images "$ACTIVE_COMPOSE" \
      "$OLD_INIT_IMAGE_VALUE" "$OLD_MODEL_IMAGE_VALUE" "$OLD_MEMORY_IMAGE_VALUE" \
      up -d --pull never >/dev/null 2>&1; then
      finalize_cutover_journal || true
      fail "停写后的最终一致性备份失败；旧服务已恢复"
    fi
    fail_journal "停写后的最终一致性备份失败且旧服务重启失败"
  fi
fi
if ! mv "$CANDIDATE_COMPOSE" "$COMPOSE_NAME"; then
  if [ "$LAYOUT" != fresh ]; then
    if compose_with_images "$ACTIVE_COMPOSE" \
      "$OLD_INIT_IMAGE_VALUE" "$OLD_MODEL_IMAGE_VALUE" "$OLD_MEMORY_IMAGE_VALUE" \
      up -d --pull never >/dev/null 2>&1; then
      finalize_cutover_journal || true
    fi
  fi
  fail "无法原子换入候选 Compose"
fi
CANDIDATE_COMPOSE=""
if ! mv "$CANDIDATE_ENV" .env; then
  if [ "$LAYOUT" != fresh ]; then
    rollback || true
  else
    restore_original_environment || true
  fi
  fail "无法原子换入候选环境文件"
fi
CANDIDATE_ENV=""
mark_cutover_data_may_change

say "==> 在无宿主发布端口的隔离模式启动候选服务"
if ! compose_internal up -d; then
  rollback && fail "新栈启动失败；旧服务已恢复"
  fail "新栈启动失败且自动回滚不完整；请保留 backups/ 与旧卷"
fi

# The override is installer-owned fixed text and was canonically validated.
# Check both rendered ownership and runtime Docker port bindings before any
# acceptance request so the candidate cannot receive external writes
# pre-commit.  Only Docker's own mapping tables are consulted: a host curl
# could be answered by an unrelated third-party process on the same port.
candidate_memory=$(compose_internal ps -q memory-gateway 2>/dev/null || true)
candidate_model=$(compose_internal ps -q model-gateway 2>/dev/null || true)
candidate_published=$(compose_internal port memory-gateway 2026 2>/dev/null || true)
memory_runtime_published=""
[ -z "$candidate_memory" ] \
  || memory_runtime_published=$(docker port "$candidate_memory" 2>/dev/null | awk 'NF {print; exit}' || true)
model_runtime_published=""
[ -z "$candidate_model" ] \
  || model_runtime_published=$(docker port "$candidate_model" 2>/dev/null | awk 'NF {print; exit}' || true)
if [ -n "$candidate_published" ] || [ -n "$memory_runtime_published" ] \
  || [ -n "$model_runtime_published" ]; then
  rollback && fail "候选验收阶段意外发布了宿主端口；旧服务已恢复"
  fail "候选验收阶段意外发布宿主端口且自动回滚不完整"
fi

candidate_http_check() {
  candidate_service=$1
  candidate_url=$2
  compose_internal exec -T "$candidate_service" python -c \
    'import sys,urllib.request; response=urllib.request.urlopen(sys.argv[1],timeout=3); raise SystemExit(0 if response.status==200 else 1)' \
    "$candidate_url" >/dev/null 2>&1
}

i=0
until candidate_http_check memory-gateway http://127.0.0.1:2026/health; do
  i=$((i+1))
  if [ "$i" -ge 180 ]; then
    rollback && fail "新栈 liveness 超时；旧服务已恢复"
    fail "新栈 liveness 超时且回滚不完整"
  fi
  sleep 1
done
i=0
until candidate_http_check model-gateway http://127.0.0.1:2030/health; do
  i=$((i+1))
  if [ "$i" -ge 180 ]; then
    rollback && fail "候选 Model Gateway liveness 超时；旧服务已恢复"
    fail "候选 Model Gateway liveness 超时且自动回滚不完整"
  fi
  sleep 1
done
if [ "$PLAN_ACCEPT_MEMORY_READINESS" = 1 ]; then
  i=0
  until candidate_http_check memory-gateway http://127.0.0.1:2026/readyz; do
    i=$((i+1))
    if [ "$i" -ge 90 ]; then
      rollback && fail "新栈 readiness 退化；旧服务和数据已恢复"
      fail "新栈 readiness 退化且回滚不完整"
    fi
    sleep 1
  done
fi
if [ "$PLAN_ACCEPT_MODEL_READINESS" = 1 ]; then
  i=0
  until candidate_http_check model-gateway http://127.0.0.1:2030/readyz; do
    i=$((i+1))
    if [ "$i" -ge 90 ]; then
      rollback && fail "候选 Model Gateway readiness 退化；旧服务和数据已恢复"
      fail "候选 Model Gateway readiness 退化且回滚不完整"
    fi
    sleep 1
  done
fi

credentials_accepted=1
GATEWAY_CRED_FILE=""
ADMIN_CRED_FILE=""
if ! GATEWAY_CRED_FILE=$(resolve_credential gateway) \
  || ! ADMIN_CRED_FILE=$(resolve_credential admin); then
  credentials_accepted=0
else
  chmod 600 "$GATEWAY_CRED_FILE" "$ADMIN_CRED_FILE" || credentials_accepted=0
fi
if [ "$credentials_accepted" -eq 1 ]; then
  if ! compose_internal exec -T memory-gateway python -c \
      'import sys,urllib.request; key=sys.stdin.buffer.readline().strip().decode("ascii"); request=urllib.request.Request("http://127.0.0.1:2026/auth/tokens",headers={"Authorization":"Bearer "+key}); response=urllib.request.urlopen(request,timeout=5); raise SystemExit(0 if response.status==200 else 1)' \
      <"$GATEWAY_CRED_FILE" >/dev/null 2>&1; then
    credentials_accepted=0
  fi
fi
if [ "$credentials_accepted" -eq 1 ]; then
  if ! compose_internal exec -T model-gateway python -c \
      'import sys,urllib.request; key=sys.stdin.buffer.readline().strip().decode("ascii"); request=urllib.request.Request("http://127.0.0.1:2030/admin/configuration",headers={"Authorization":"Bearer "+key}); response=urllib.request.urlopen(request,timeout=5); raise SystemExit(0 if response.status==200 else 1)' \
      <"$ADMIN_CRED_FILE" >/dev/null 2>&1; then
    credentials_accepted=0
  fi
fi
if [ "$credentials_accepted" -ne 1 ]; then
  if [ "$LAYOUT" != fresh ] && rollback; then
    fail "初始化未交付安全的 credentials；旧服务和数据已恢复"
  fi
  if [ "$LAYOUT" = fresh ]; then
    compose "$COMPOSE_NAME" stop >/dev/null 2>&1 || true
  fi
  fail "初始化未交付安全的 credentials；自动回滚不完整"
fi

if ! mark_cutover_committed; then
  rollback && fail "候选已隔离验收，但无法持久提交事务；旧服务和数据已恢复"
  fail "候选已隔离验收，但无法持久提交事务且自动回滚不完整"
fi

say "==> 事务已持久提交，发布宿主 Memory 端口"
if ! compose_candidate_live up -d --no-deps --force-recreate memory-gateway; then
  fail_journal "新栈已提交但宿主端口发布失败；数据不会回滚"
fi
i=0
until curl -fsS "http://$HOST_PROBE:$PORT/health" >/dev/null 2>&1; do
  i=$((i+1))
  [ "$i" -lt 180 ] \
    || fail_journal "新栈已提交但宿主 liveness 失败；数据不会回滚"
  sleep 1
done
if [ "$PLAN_ACCEPT_HOST_READINESS" = 1 ]; then
  i=0
  until curl -fsS "http://$HOST_PROBE:$PORT/readyz" >/dev/null 2>&1; do
    i=$((i+1))
    [ "$i" -lt 90 ] \
      || fail_journal "新栈已提交但宿主 readiness 失败；数据不会回滚"
    sleep 1
  done
fi
public_memory=$(compose_candidate_live ps -q memory-gateway 2>/dev/null || true)
public_model=$(compose_candidate_live ps -q model-gateway 2>/dev/null || true)
public_memory_port=$(compose_candidate_live port memory-gateway 2026 2>/dev/null || true)
public_model_ports=""
[ -z "$public_model" ] \
  || public_model_ports=$(docker port "$public_model" 2>/dev/null | awk 'NF {print; exit}' || true)
[ -n "$public_memory" ] && [ -n "$public_memory_port" ] \
  && [ -z "$public_model_ports" ] \
  || fail_journal "新栈已提交但最终端口契约不成立；数据不会回滚"

if ! complete_committed_cutover; then
  fail "新栈已验收并发布，但 committed journal 清理失败；下次安装将幂等清理"
fi

detect_lan_ip() {
  if command -v ipconfig >/dev/null 2>&1; then
    for lan_interface in en0 en1; do
      lan_candidate=$(ipconfig getifaddr "$lan_interface" 2>/dev/null || true)
      [ -z "$lan_candidate" ] || { printf '%s\n' "$lan_candidate"; return 0; }
    done
  fi
  if command -v hostname >/dev/null 2>&1; then
    lan_candidate=$(hostname -I 2>/dev/null | awk 'NF {print $1; exit}' || true)
    [ -z "$lan_candidate" ] || { printf '%s\n' "$lan_candidate"; return 0; }
  fi
  return 0
}

mint_console_login_url() {
  # 用宿主 credentials 里的 console token 向本机后端换取一次性登录 code。
  # 任何一步失败都返回非零，调用方保持裸 Web Console URL 输出不变；
  # token 与 code 只打印到终端，不写入任何日志文件。
  login_cred_path=$(resolve_credential gateway 2>/dev/null || printf '%s\n' "$INSTALL_DIR/credentials/gateway.txt")
  [ -s "$login_cred_path" ] || return 1
  login_token=$(tr -d '\r\n' < "$login_cred_path" 2>/dev/null || true)
  [ -n "$login_token" ] || return 1
  login_response=$(curl -fsS -X POST \
    -H "Authorization: Bearer $login_token" \
    "http://$HOST_PROBE:$PORT/auth/console-login-code" 2>/dev/null) || return 1
  login_code=$(printf '%s' "$login_response" \
    | sed -n 's/.*"code"[[:space:]]*:[[:space:]]*"\(mgc_[^"]*\)".*/\1/p')
  [ -n "$login_code" ] || return 1
  printf '%s\n' "http://127.0.0.1:$PORT/ui/#login=$login_code"
}
CONSOLE_LOGIN_URL=$(mint_console_login_url 2>/dev/null || true)

say ""
say "Memory Platform $RELEASE 已启动"
say "  Web Console:  http://$HOST_PROBE:$PORT/ui/"
if [ -n "$CONSOLE_LOGIN_URL" ]; then
  say "  一次性登录:     $CONSOLE_LOGIN_URL（5 分钟内有效，仅可使用一次）"
fi
say "  Client URL:   http://$HOST_PROBE:$PORT/v1"
say "  Model:        memory-auto"
if [ "$HOST" != 127.0.0.1 ]; then
  if [ "$HOST" = 0.0.0.0 ]; then
    LAN_IP=$(detect_lan_ip)
  else
    LAN_IP=$HOST
  fi
  if [ -n "$LAN_IP" ]; then
    say "  局域网/手机:  http://$LAN_IP:$PORT/v1（Web Console 为 http://$LAN_IP:$PORT/ui/）"
  else
    say "  局域网/手机:  http://<本机局域网IP>:$PORT/v1（macOS 用 ipconfig getifaddr en0、Linux 用 hostname -I 查询）"
  fi
fi
GATEWAY_CRED_REPORT=$(resolve_credential gateway 2>/dev/null || printf '%s\n' "$INSTALL_DIR/credentials/gateway.txt")
ADMIN_CRED_REPORT=$(resolve_credential admin 2>/dev/null || printf '%s\n' "$INSTALL_DIR/credentials/admin.txt")
say "  Console token: $GATEWAY_CRED_REPORT"
say "  Admin key:    $ADMIN_CRED_REPORT"
say "（纯文本 .txt，可用文本编辑器打开；旧版 .key 仍兼容）"
say "密钥值没有进入本脚本输出、Compose 环境或 Docker 日志。"
if [ "$HOST" != 127.0.0.1 ]; then
  say "已监听可信局域网；请确认路由器没有把端口映射到公网。"
fi
if [ -n "$BACKUP_PATH" ]; then
  say "升级前备份: $BACKUP_PATH"
fi
prune_host_backups

if [ "${MEMORY_NO_OPEN:-0}" != 1 ]; then
  if command -v open >/dev/null 2>&1; then
    open "http://$HOST_PROBE:$PORT/ui/" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    xdg-open "http://$HOST_PROBE:$PORT/ui/" >/dev/null 2>&1 || true
  fi
fi
