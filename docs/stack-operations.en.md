# Memory Platform stack operations

[中文](stack-operations.md) · **[English](stack-operations.en.md)**

This guide holds the daily operations, advanced model configuration, security boundaries, backup, and migration details that do not belong on the project landing page. For a first installation, start with the [root README](../README.en.md#-quick-start).

## Common addresses

| Purpose | URL |
| --- | --- |
| Web Console | `http://127.0.0.1:2026/ui/` |
| Memory Gateway health | `http://127.0.0.1:2026/health` |
| MCP | `http://127.0.0.1:2026/mcp` |
| OpenAI-compatible Memory base URL | `http://127.0.0.1:2026/v1` |
| Model Gateway base URL (inside Docker only) | `http://model-gateway:2030/v1`; no host port is published |

## Daily operation and checks

From the repository root after a source installation:

```bash
scripts/memgw stack status
scripts/memgw stack doctor

scripts/memgw stack start
scripts/memgw stack restart
scripts/memgw stack stop
```

`stack doctor` checks both services, their runtime directories, and the connection from Memory Gateway to Model Gateway. For service-specific diagnosis, continue with:

- [Memory Gateway documentation](../services/memory-gateway/README.md)
- [Model Gateway operations](../services/model-gateway/docs/operations.md)

The one-line installer stores the Compose file in `~/memory-platform` by default (`$HOME\memory-platform` on Windows). From that directory, Docker users can run:

```bash
docker compose -f docker-compose.user.yml ps
docker compose -f docker-compose.user.yml exec memory-gateway memgw stack doctor

docker compose -f docker-compose.user.yml restart
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml start
```

Follow later logs (generated credentials are never written there) with:

```bash
docker compose -f docker-compose.user.yml logs memory-gateway model-gateway
docker compose -f docker-compose.user.yml logs -f memory-gateway model-gateway
```

## Port 2026 already in use

Both the macOS/Linux `deploy/install.sh` and Windows `deploy/install.ps1` installers pick a free port automatically. For manual deployments, create a `.env` file next to `docker-compose.user.yml` with one line:

```bash
MEMORY_PORT=3026
```

Then restart with `docker compose -f docker-compose.user.yml up -d`. From then on, replace every `2026` in this repository's docs with `3026` (Web Console, Base URL, MCP address, and so on). The container-internal port stays 2026; only the host mapping changes.

## Phone and LAN access (Docker)

The Docker deployment listens on the loopback interface only, so phones and other devices cannot reach it. Add one line to a `.env` file next to `docker-compose.user.yml`:

```bash
MEMORY_HOST=0.0.0.0
```

Then restart with `docker compose -f docker-compose.user.yml up -d` and point clients at the computer's LAN IP, for example `http://192.168.1.20:2026/v1`. Installer users can re-run with LAN access enabled; the installer prints the phone-ready address:

```bash
# macOS / Linux; select an immutable release
VERSION=v0.2.0
curl -fsSL "https://raw.githubusercontent.com/SparkHello/Memory_Platform/$VERSION/deploy/install.sh" -o install-memory-platform.sh
MEMORY_HOST=0.0.0.0 MEMORY_PLATFORM_VERSION="$VERSION" sh install-memory-platform.sh
```

```powershell
# Windows PowerShell; select the same immutable release
$Version = "v0.2.0"
$env:MEMORY_HOST = "0.0.0.0"
$env:MEMORY_PLATFORM_VERSION = $Version
irm "https://raw.githubusercontent.com/SparkHello/Memory_Platform/$Version/deploy/install.ps1" -OutFile install-memory-platform.ps1
& .\install-memory-platform.ps1
```

Source installs listen on loopback by default. Pass `--host 0.0.0.0` explicitly only when LAN access is required.

Do this only on trusted home networks or Tailscale. Every entry point still requires a scoped or migration-time credential, but never expose the service to the public internet.

## Keys and identity

The stack has several keys with separate responsibilities. Do not reuse them:

| Key | Purpose | Stored in |
| --- | --- | --- |
| Per-device chat token | One chat client accessing `/v1` | Auth SQLite stores only SHA-256; plaintext is shown once |
| Per-device MCP token | One MCP client accessing `/mcp` | Same, independently revocable |
| Console token | Management REST and Web Console | A fresh install delivers the initial token to `credentials/gateway.key`; later Console tokens are local-CLI-only |
| Legacy Gateway key | One-version all-scope credential for migrated volumes only | Host `credentials/gateway.key` and Memory secret volume; disable after migration |
| Model Gateway backend key | Memory Gateway calling the eight exact configured chat/memory/knowledge/embedding routes | Each service's isolated secret file |
| Model Gateway admin key | Changing channel secrets and route configuration | Admin-side only; never persisted by Memory Gateway |
| Provider API key | Calling the real upstream provider | Model Gateway `secrets.env` outside the repository |

A fresh Docker initialization creates one `first-console` token in the Auth DB, disables legacy authentication, and writes that Console token plus the Model admin credential to host `credentials/gateway.key` and `credentials/admin.key` with owner-only permissions. Values never enter daemon logs or the long-lived process environment. A migrated legacy volume retains its old key for one transition version. Create normal device tokens after the first Console login:

```bash
scripts/memgw token create --role chat --name <device> --user <user>
scripts/memgw token create --role mcp --name <device> --user <user>
scripts/memgw token create --role console --name <browser> --user <user>
scripts/memgw token list
scripts/memgw token revoke <token-id>
```

If `modelgw` is not on `PATH`, a source installation can use the shared virtual environment:

```bash
services/memory-gateway/.venv/bin/modelgw secret set memory-console-admin
```

Reset the independent admin credential with `modelgw secret set memory-console-admin --stdin` only when necessary. Never put secrets in command-line arguments, configuration recipes, example `.env` files, shell history, or Docker environment variables.

Every scoped token is bound to its user, so callers cannot rewrite the namespace with a header. After all clients use chat/MCP/Console tokens, set `GATEWAY_LEGACY_API_KEY_ENABLED=false`; do not keep the legacy all-scope credential as a multi-device key.

The Web Console refuses to revoke a user's final active Console token and returns stable `409 last_active_console_token`. To rotate the current browser credential, first create a backup with the local `--role console` command above, save it and verify login, then revoke the old token. The browser REST API cannot mint Console tokens.

## Model quickstart and advanced configuration

Source installation opens model quickstart from `scripts/setup.sh`. Run it again later with:

```bash
services/memory-gateway/.venv/bin/modelgw quickstart
```

Quickstart includes presets for DeepSeek, Kimi China, MiMo, and DashScope Beijing. It asks only for a channel, API key, chat model, and optional semantic search. It makes a read-only `/models` request to show the exact model IDs visible to the key and does not automatically send an inference request.

| Console name | Technical name | Meaning |
| --- | --- | --- |
| Channel | connection | The provider that owns the account and API key |
| Model | deployment | The exact upstream model ID and declared capabilities |
| Purpose | route | A stable business name for chat, extraction, retrieval, and other work |
| Priority | fallback | The order used when the current model is unavailable |
| Pricing | pricing | A manually verified price snapshot tied to one deployment |

To reconfigure an existing installation without reinstalling:

```bash
scripts/setup.sh --configure-only --config <file> --json
```

The recipe must match [`ai-quickstart.schema.json`](ai-quickstart.schema.json) and must not contain any key. Pass the provider API key only through standard input. The complete non-interactive workflow is in [Installing with an AI assistant](ai-install.md) (Chinese).

If you already maintain multiple channels, fallback order, or purpose-specific routes, use the individual `modelgw` commands instead of overwriting them with quickstart:

- [Model Gateway README](../services/model-gateway/README.md)
- [Model Gateway configuration standard](../services/model-gateway/docs/configuration.md)
- [Model Gateway client protocol](../services/model-gateway/docs/client-protocol.md)

To confirm which models a key can see, prefer the free read-only discovery command:

```bash
modelgw discover --preset <id> --non-interactive --json
```

It reads only `/models`. Use `--live` only when you explicitly accept a real inference request and any resulting cost.

## Sensitive-data egress and deployment boundary

- `ALLOW_SENSITIVE_EGRESS=false` blocks locally classified private or sensitive content from remote memory extraction, embeddings, AI review, and the knowledge agent by default.
- It does not intercept the current message a user deliberately sends to the chat provider through `/v1`.
- `redact_sensitive=true` masks only the current response. It does not rewrite SQLite content or make backups redacted.
- Memory and knowledge embeddings must carry a trusted, consistent space ID. Missing or mismatched IDs fall back to keyword/FTS retrieval instead of assuming old vectors belong to the current space.
- The knowledge agent orchestrates the local index and selects citations; it does not execute instructions found inside documents.

The default deployment target is a personal computer or trusted home network:

- SQLite, caches, tool idempotency, and some background state are designed for one process;
- do not treat the default deployment as a public internet multi-tenant SaaS;
- Docker publishes only Memory Gateway. Model Gateway stays on an internal backend network and Memory cannot mount provider/admin secrets;
- the two long-lived containers run as UID 10001/10002 with separate data/secret volumes, read-only root filesystems, and no capabilities;
- use individually revocable fixed-role device tokens instead of a shared credential or mutable user header.

For complete contracts, see the [Memory Gateway security boundary](../services/memory-gateway/README.md#安全边界) and [Model Gateway core boundary](../services/model-gateway/AGENTS.md#核心边界). Report vulnerabilities privately through the [security policy](../SECURITY.md).

## Where data lives

The repository holds only source code and non-sensitive examples. Docker separates runtime state into `memory-data`, `memory-secrets`, `model-data`, and `model-secrets` volumes, including:

- long-term memory, recent context, and branch nodes;
- the isolated knowledge base and document versions;
- Model Gateway configuration, usage database, and price snapshots;
- Auth token hashes and each service's isolated secret file.

Never commit `.env`, real SQLite files, logs, evaluation snapshots, or portable backups.

## Backup, restore, and migration

Re-running either Docker release installer creates and re-verifies a portable archive under the install directory's `backups/` folder before pulling replacement images, then removes the temporary copy from the data volume. The default retention is the five newest upgrade archives (`MEMORY_BACKUP_RETENTION=1..50` changes it). Backup or safe-cleanup failure stops the upgrade before replacing the existing service.

### Source installation

Create a portable archive from the repository root:

```bash
scripts/memgw stack backup --output memory-stack.zip
```

Backup v2 requires the memory database, knowledge database, Auth token hash database, redacted Model configuration, and a manifest that explicitly marks usage present or absent. It excludes provider, admin, backend, legacy Gateway, and device-token plaintext secrets.

Even without keys, the archive contains complete private memory and knowledge content. Treat it as a sensitive file.

Restore to a new machine after installing dependencies:

```bash
git clone https://github.com/SparkHello/Memory_Platform.git
cd Memory_Platform
scripts/bootstrap.sh
scripts/memgw stack restore /path/to/memory-stack.zip --yes --start
```

Restore verifies manifest hashes, SQLite, and JSON, stops both services, and creates rollback copies outside the repository for replaced local files. If the target does not already have the required keys, re-enter the values excluded from the archive afterwards.

### Docker installation

Users with only the Compose file can create the archive inside the container and copy it into the current directory:

```bash
docker compose -f docker-compose.user.yml --profile maintenance run --rm \
  stack-maintenance --home /data/config \
  --project-root /app/services/memory-gateway stack backup \
  --model-gateway-home /model-data --output /data/memory-stack.zip
docker compose -f docker-compose.user.yml cp \
  memory-gateway:/data/memory-stack.zip ./memory-stack.zip
```

Restore must run while the main services are stopped. Copy the archive into the container, stop the service, and use a one-off container for restore:

```bash
docker compose -f docker-compose.user.yml cp \
  ./memory-stack.zip memory-gateway:/data/restore.zip
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml --profile maintenance run --rm \
  --entrypoint python stack-maintenance \
  /usr/local/libexec/memory-platform/restore_split.py
docker compose -f docker-compose.user.yml up -d
```

The portable archive never brings in or overwrites a target's secret volumes. Auth DB contains only token hashes, so known device token plaintext can migrate; the plaintext still exists only at the device. Stop both long-lived services before restore. The one-shot maintenance process validates disk space, hashes, SQLite integrity, schema, and `secrets_included=false`, then journals atomic replacement and retains a rollback copy.

### Migrating from the former two-directory layout

If an old machine still has separate `My_Memory` and `Model_Gateway` projects, use the unified stack backup already provided by `My_Memory`. Do not copy `.env` files or databases by hand:

```bash
cd /path/to/My_Memory
scripts/memgw stack backup --output /safe/path/memory-stack.zip

cd /path/to/Memory_Platform
scripts/bootstrap.sh
scripts/memgw stack restore /safe/path/memory-stack.zip --yes --start
```

Keep the old directories as read-only rollback sources at first. Never let the old and new stacks bind the same ports or write the same database. After verifying the new stack, Web Console, memory count, and knowledge documents, decide whether to archive the old directories.

## Direct-provider compatibility mode

If you are not running a separate Model Gateway yet, Memory Gateway still supports the older `UPSTREAM_*`, `LLM_*`, `memgw model`, `memgw route`, and `memgw pricing` paths.

New deployments should use Model Gateway. When `MODEL_GATEWAY_BASE_URL` and `MODEL_GATEWAY_API_KEY` are configured as a pair, chat, background memory tasks, the knowledge agent, and embeddings call stable routes only. They do not silently fall back to old `.env` provider keys when central routing fails.

See [Memory Gateway configuration](../services/memory-gateway/README.md#配置项) for the complete compatibility settings.

## Developer entry point

```bash
scripts/bootstrap.sh
scripts/test.sh
```

Code ownership, targeted tests, and pull request requirements are in [Contributing](../CONTRIBUTING.md) and the root [`AGENTS.md`](../AGENTS.md).
