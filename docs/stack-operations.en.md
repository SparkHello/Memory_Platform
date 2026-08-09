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
| Model Gateway base URL | `http://127.0.0.1:2030/v1` |

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
docker compose -f docker-compose.user.yml exec memory-platform memgw stack doctor

docker compose -f docker-compose.user.yml restart
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml start
```

Read first-run output and follow later logs with:

```bash
docker compose -f docker-compose.user.yml logs memory-platform
docker compose -f docker-compose.user.yml logs -f memory-platform
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
# macOS / Linux
MEMORY_HOST=0.0.0.0 sh -c "$(curl -fsSL https://raw.githubusercontent.com/SparkHello/Memory_Platform/main/deploy/install.sh)"
```

```powershell
# Windows PowerShell
$env:MEMORY_HOST="0.0.0.0"; irm https://raw.githubusercontent.com/SparkHello/Memory_Platform/main/deploy/install.ps1 | iex
```

Source installs already listen on all interfaces by default.

Do this only on trusted home networks or Tailscale. The API still requires `GATEWAY_API_KEY`, but never expose the service to the public internet.

## Keys and identity

The stack has several keys with separate responsibilities. Do not reuse them:

| Key | Purpose | Stored in |
| --- | --- | --- |
| Memory Gateway API key | Client access to `/v1`, MCP, REST, and Web Console APIs | Memory Gateway user config directory |
| Model Gateway backend key | Memory Gateway calling permitted `memory.*` / `knowledge.*` routes | A file outside the repository on each side |
| Model Gateway admin key | Changing channel secrets and route configuration | Admin-side only; never persisted by Memory Gateway |
| Provider API key | Calling the real upstream provider | Model Gateway `secrets.env` outside the repository |

The first installation prints the client `GATEWAY_API_KEY` and Model Gateway admin key once each. Lost values cannot be displayed again; reset them instead:

```bash
scripts/memgw secret set gateway
modelgw secret set memory-console-admin
```

If `modelgw` is not on `PATH`, a source installation can use the shared virtual environment:

```bash
services/memory-gateway/.venv/bin/modelgw secret set memory-console-admin
```

These commands read the new value without echoing it. For automation, prefer each command's `--stdin` option. Never put secrets in command-line arguments, configuration recipes, example `.env` files, or shell history.

`GATEWAY_API_KEY` is bound to a fixed `GATEWAY_USER_ID` by default, so callers cannot rewrite the namespace with `X-User-Id`. `GATEWAY_ALLOW_USER_ID_HEADER=true` exists only for migration from an older shared-key setup and is not recommended on untrusted networks.

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
- Model Gateway's admin interface listens on loopback by default, and cross-host exposure must sit behind HTTPS;
- for strong isolation, use separate credentials and instances per user instead of a shared key with mutable `X-User-Id`.

For complete contracts, see the [Memory Gateway security boundary](../services/memory-gateway/README.md#安全边界) and [Model Gateway core boundary](../services/model-gateway/AGENTS.md#核心边界). Report vulnerabilities privately through the [security policy](../SECURITY.md).

## Where data lives

The repository holds only source code and non-sensitive examples. Runtime data lives in the user configuration directories or the Docker `memory-platform-data` volume, including:

- long-term memory, recent context, and branch nodes;
- the isolated knowledge base and document versions;
- Model Gateway configuration, usage database, and price snapshots;
- each service's own key file and logs.

Never commit `.env`, real SQLite files, logs, evaluation snapshots, or portable backups.

## Backup, restore, and migration

Re-running either Docker one-line installer creates a portable backup under the install directory's `backups/` folder before pulling a new image. If backup fails, the upgrade stops without replacing the existing service. Use the commands below for manual backups, migration, and restore.

### Source installation

Create a portable archive from the repository root:

```bash
scripts/memgw stack backup --output memory-stack.zip
```

The archive contains the migratable memory database, knowledge database, redacted Model Gateway configuration, usage database, and non-secret settings. It excludes provider keys, the admin key, the backend key, and the Memory Gateway API key.

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
docker compose -f docker-compose.user.yml exec memory-platform \
  memgw stack backup --output /data/memory-stack.zip
docker compose -f docker-compose.user.yml cp \
  memory-platform:/data/memory-stack.zip ./memory-stack.zip
```

Restore must run while the main services are stopped. Copy the archive into the container, stop the service, and use a one-off container for restore:

```bash
docker compose -f docker-compose.user.yml cp \
  ./memory-stack.zip memory-platform:/data/memory-stack.zip
docker compose -f docker-compose.user.yml stop
docker compose -f docker-compose.user.yml run --rm --entrypoint memgw \
  memory-platform stack restore /data/memory-stack.zip --yes
docker compose -f docker-compose.user.yml up -d
```

The portable archive neither brings in nor overwrites the target's existing `GATEWAY_API_KEY`, admin key, or provider API keys; restore only rewires the backend key internally. Continue using the access keys saved during that target's first start. If any are missing, reset them as described in [Keys and identity](#keys-and-identity) and re-enter provider API keys as needed. Do not restore with `docker compose exec` inside the running main container: its foreground services still own the databases and ports, so restore correctly refuses to overwrite them.

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
