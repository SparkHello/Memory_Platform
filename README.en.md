<div align="center">

# 🧠 Memory Platform

**Add an automatic, local memory gateway to the AI client you already use.**

For ordinary chat, connect to the OpenAI-compatible `/v1` endpoint: Memory Platform recalls relevant memory, injects context, and extracts durable information after a complete answer.<br>
**No MCP setup and no extra “remember this” prompt are required.** MCP remains available for explicit memory organization and knowledge retrieval.

Memory stays on your own device, where you can inspect, edit, delete, and back it up. Providers, routes, and failover are managed server-side.

[![Release](https://img.shields.io/github/v/release/SparkHello/Memory_Platform)](https://github.com/SparkHello/Memory_Platform/releases)
[![CI](https://github.com/SparkHello/Memory_Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/SparkHello/Memory_Platform/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

[中文](README.md) · **[English](README.en.md)**

[🔀 How it works](#-two-gateway-layers-automatic-ordinary-chat) · [🧭 Why Memory Platform](#-why-memory-platform) · [🚀 Get started](#-quick-start) · [🔌 Connect a client](#-connecting-clients) · [📚 Docs](#-documentation)

</div>

![Memory Platform automatic memory gateway banner with a real view of the local Web Console](docs/images/memory-platform-hero.jpg)

<p align="center"><sub>Automatic memory gateway · Local-first · Auditable · Model-neutral · All product screens use demo data, never real user content</sub></p>

## ✨ The one-minute version

| What you may want to know first | Short answer |
| --- | --- |
| **What does it do?** | It is an automatic memory gateway between a chat client and a model: it recalls and injects relevant memory, then extracts durable information after a complete answer. |
| **Who is it for?** | People using Chatbox, RikkaHub, FLIT, or another OpenAI-compatible client who want durable preferences and project context. |
| **Where is the data?** | Memory, knowledge documents, and runtime configuration remain local; Docker stores them in the `memory-platform-data` volume. |
| **Must I change clients?** | No. Point your current client's Base URL at Memory Platform's OpenAI-compatible `/v1` endpoint. |
| **Do I need MCP or a memory prompt?** | Not for ordinary chat. The gateway handles recall and saving automatically; `/mcp` is only for explicit memory organization and knowledge retrieval. |
| **Does it lock me to a model?** | No. Clients use `memory-auto`; provider and model changes stay on the server. |
| **What is the fastest path?** | Start Docker → configure a model in the browser → enter a Base URL, API key, and model name in your client. |

Memory Platform is not a new chat client and does not include a model. Embeddings are optional; keyword retrieval works without them.

> [!IMPORTANT]
> **Local-first does not mean no network traffic.** Memory, knowledge documents, and configuration stay on your device by default. If you choose a cloud model provider, the current message you send and the context permitted for that turn are sent to that provider for inference. The default deployment targets a personal machine or trusted home network; do not expose it unauthenticated to the public internet.

## 🔀 Two gateway layers, automatic ordinary chat

![Two-gateway flow: an existing client connects through the OpenAI-compatible Memory Gateway for automatic recall and saving, then Model Gateway handles model routing and failover; MCP is optional](docs/images/gateway-flow.en.svg)

Clients connect only to Memory Gateway. For ordinary `/v1` requests, the gateway automatically recalls memory, injects context, and extracts durable information after the answer; it does not depend on the model remembering to call a tool. Model Gateway selects providers, models, and fallback order by stable purpose. Add `/mcp` only when the model should explicitly search or organize memory or retrieve knowledge.

## 🧭 Why Memory Platform

- **Gateway-managed memory instead of waiting for tool calls:** ordinary OpenAI-compatible chat gets automatic recall, injection, and saving; neither MCP nor an extra memory prompt is a prerequisite.
- **Keep your current chat entry point:** change only the Base URL, API key, and model name, then continue using the client you already know.
- **Governance before “remember more”:** every memory keeps its source and status, explains why it was recalled, and can be edited, archived, restored, or permanently deleted.
- **Memory and knowledge stay physically separate:** personal facts and preferences live in `memory.db`; imported long-form documents live in `knowledge.db` and never enter memory decay or automatic chat context.
- **Model choices stay server-side:** Model Gateway selects providers, models, and fallback order by stable purpose, so clients and memory data do not migrate with a vendor change.

These projects solve different layers of the problem. Start with the one closest to your primary goal:

| Your primary goal | Start with |
| --- | --- |
| Add a general-purpose memory SDK, server API, or managed platform to an application | [Mem0](https://github.com/mem0ai/mem0) |
| Build a temporal context graph centered on entities, fact validity, and historical queries | [Zep / Graphiti](https://github.com/getzep/graphiti) |
| Build a stateful agent runtime in which the agent manages its own memory, state, and tools | [Letta](https://github.com/letta-ai/letta) |
| Keep an existing OpenAI-compatible client while adding gateway-managed automatic memory, local deployment, auditable governance, isolated knowledge, and unified model routing | **Memory Platform** |

This is not a performance ranking. Memory Platform currently targets personal machines and trusted home networks; it does not try to replace a managed memory platform, a full temporal knowledge graph, or an agent runtime.

## 🚀 Quick start

### Easiest path: one-line installer (Docker)

You need Docker Desktop and an API key for one model provider. You do not need Python, Node.js, or a repository clone.

macOS / Linux terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/SparkHello/Memory_Platform/main/deploy/install.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/SparkHello/Memory_Platform/main/deploy/install.ps1 | iex
```

The installer checks Docker, uses a stable per-user install directory, picks a free port, starts the base service, prints `GATEWAY_API_KEY` and the admin key, and opens browser-based onboarding. First start takes 1–2 minutes. “Base service ready” does not mean chat is ready yet; choose a model provider in the browser before connecting a client.

Save both keys. Re-running the same command from any directory finds the existing installation, writes a pre-upgrade backup under `backups/`, and then upgrades the image. The default directory is `~/memory-platform` on macOS/Linux or `$HOME\memory-platform` on Windows; runtime data remains in the Docker `memory-platform-data` volume.

### Manual path (to review each step)

```bash
curl -O https://raw.githubusercontent.com/SparkHello/Memory_Platform/main/deploy/docker-compose.user.yml
docker compose -f docker-compose.user.yml up -d
```

Compose anonymously pulls the public amd64/arm64 image `ghcr.io/sparkhello/memory-platform:latest`. The [GHCR package page](https://github.com/SparkHello/Memory_Platform/pkgs/container/memory-platform) lists its versions and digests.

First start takes 1–2 minutes of internal setup, during which `http://127.0.0.1:2026/ui/` is not reachable yet — that is expected. Once ready, the container logs print `GATEWAY_API_KEY` and the admin key once each:

```bash
docker compose -f docker-compose.user.yml logs memory-platform
```

If no key has appeared yet, wait a moment and run the command again. Save both: `GATEWAY_API_KEY` connects the Web Console and clients; the admin key is used only to change model channels and routes in the browser. If port 2026 is already taken, write `MEMORY_PORT=3026` into a `.env` file next to the Compose file and restart (see the [stack operations guide](docs/stack-operations.en.md#port-2026-already-in-use)).

Then:

1. Open `http://127.0.0.1:2026/ui/` and connect with `GATEWAY_API_KEY`.
2. Open Models & Routes and use the admin key to unlock this configuration session.
3. Add a channel, enter the provider API key, and choose a model from the discovered list.
4. Follow [Connecting clients](#-connecting-clients) below for the Base URL, API key, and model name.

Image upgrades do not remove data from the `memory-platform-data` volume. Daily commands, key resets, backup, and migration are in the [stack operations guide](docs/stack-operations.en.md).

### Install from source

For macOS or Linux, with Python 3.12+, Node.js 22, and npm:

```bash
git clone https://github.com/SparkHello/Memory_Platform.git
cd Memory_Platform
scripts/setup.sh
```

The setup script prepares the environment, builds the Web Console, starts the stack, generates local keys, and opens guided model setup. Use `scripts/setup.sh --install-only` to prepare the environment only, or add `--skip-ui` if you do not need the Web Console.

An AI or agent can instead follow [Installing with an AI assistant](docs/ai-install.md) (Chinese), create a non-secret recipe, and call `scripts/setup.sh --config <file> --json`. The provider API key is passed only through standard input.

## 🔌 Connecting clients

Add a new “OpenAI-compatible” provider in Chatbox, RikkaHub, FLIT, or another client:

```text
Base URL: http://127.0.0.1:2026/v1
API Key:  the GATEWAY_API_KEY generated during installation
Model:    memory-auto
```

After one complete message, open `http://127.0.0.1:2026/ui/` to check whether a memory was created. When you later change providers or models, only the server configuration changes; the client keeps using `memory-auto`.

Ordinary chat does not require MCP or a system prompt telling the model when to save memory. In the default `read-write` mode, Memory Gateway handles relevant-memory recall, context injection, and post-answer extraction automatically.

On a phone, `localhost` and `127.0.0.1` point to the phone itself. LAN or Tailscale devices must use the address of the computer running Memory Platform; Docker deployments also need `MEMORY_HOST=0.0.0.0` in a `.env` file next to the Compose file, plus a restart, to listen on the LAN. Field locations, verification, and troubleshooting are covered in the [client setup guide](docs/client-setup.md) (Chinese).

### Optional MCP: let the model use memory and knowledge explicitly

Clients that support Streamable HTTP MCP can connect to:

```text
http://127.0.0.1:2026/mcp
```

Authenticate with the same `GATEWAY_API_KEY`. MCP is useful when the model should explicitly search, save, or organize memory and retrieve documents you deliberately imported. It is an enhancement, not a prerequisite for automatic memory. For ordinary chat, the OpenAI-compatible endpoint above is enough.

| What you want to do | Entry point | Who decides when to use memory |
| --- | --- | --- |
| Recall and save automatically in a normal chat client | `/v1` | Memory Platform handles it automatically |
| Let the model search, save, or organize explicitly | `/mcp` | The model calls tools |
| Inspect, edit, delete, import, or back up | `/ui` | You act in the browser |
| Use only unified model routing | Model Gateway `/v1` | The caller selects a purpose |

The knowledge base never enters chat context automatically. It requires an explicit MCP, REST, or Web Console search.

## 🖥️ Visible and governable

### One workspace for context, memory, and related topics

![Memory Platform memory studio showing recent context, core memory, related topics, and recall explanations](docs/images/console-studio.png)

<p align="center"><sub>See the current context, durable core memory, and the reason each item was recalled.</sub></p>

### How a conversation becomes long-term memory

![Four-step memory flow: chat normally, verify before saving, recall on demand, then inspect, edit, or delete](docs/images/memory-flow.en.svg)

The system waits for a complete answer, checks the original wording, subject, negation, and sensitivity, and only then decides whether to save. Truncated, filtered, or unfinished tool-call responses do not create memory.

You do not need to append “remember this” to each message. The system conservatively extracts durable information only from content the user actually expressed.

### Search and govern the whole memory library

![Memory Platform memory library for searching, filtering, and governing long-term memory](docs/images/console-memories.png)

<p align="center"><sub>All screens use demo data. Search, filter, pin, archive, restore, and permanently delete from the local Web Console.</sub></p>

## 🧰 Core capabilities

- **Automatic memory gateway with optional MCP:** ordinary `/v1` chat gets automatic recall, injection, and saving, with streaming, tool-call, multimodal-part, and reasoning-field compatibility plus `off` / `read` / `read-write` modes and conversation branches.
- **Long-term memory and governance:** verifiable source text, lifecycles, timelines, topic links, recall explanations, edit, merge, soft delete, restore, permanent deletion, and export.
- **Isolated knowledge base:** text, Markdown, PDF, DOCX, and EPUB with full-text/vector hybrid retrieval, immutable document versions, and exact passage citations.
- **Models, failover, and usage:** purpose-based model selection and fallback, with channel, model, token, latency, and price snapshots but no prompts, replies, tool arguments, or knowledge content in usage logs.
- **Safe fallback:** embedding space or dimension mismatches fall back to keyword retrieval; sensitive content is excluded from remote extraction, embeddings, AI review, and the knowledge agent by default.

The complete interface and behavior contracts are documented in [Memory Gateway](services/memory-gateway/README.md) and [Model Gateway](services/model-gateway/README.md).

## 🧱 Two gateways, one installation

| Service | Default address | Owns | Does not own |
| --- | --- | --- | --- |
| [Memory Gateway](services/memory-gateway/README.md) | `127.0.0.1:2026` | Long-term memory, recent context, knowledge base, MCP, OpenAI-compatible proxy, and Web Console | Provider accounts and channel pricing |
| [Model Gateway](services/model-gateway/README.md) | `127.0.0.1:2030` | Model connections, purpose routes, fallback order, secret references, usage, and price snapshots | Chat, memory, or knowledge content |

Memory behavior and model-provider configuration have different change rates and security responsibilities, so they run separately. Root commands still install, test, back up, and migrate them together. Memory Gateway calls Model Gateway only through stable routes and a separate backend key.

## 🔐 Current boundaries

- The default target is a personal machine or trusted home network, not an unhardened public multi-tenant SaaS.
- SQLite, caches, tool idempotency, and some background state are designed for one process; low-latency ANN retrieval over millions of memories is not a current goal.
- Topic, entity, and temporal links are lightweight and are not a substitute for full entity resolution, a bitemporal knowledge graph, or deep multi-hop reasoning.
- The OpenAI-compatible entry point focuses on Chat Completions; it is not a complete proxy for Responses, audio, files, or image generation.
- Backups exclude keys but still contain complete private memory and knowledge content, so they remain sensitive files.

Key boundaries, sensitive-data egress, backup and restore, and advanced model configuration are covered in the [stack operations guide](docs/stack-operations.en.md).

## 📚 Documentation

- [Client setup for Chatbox, RikkaHub, FLIT, and similar clients](docs/client-setup.md) (Chinese)
- [Stack operations, advanced configuration, backup, and migration](docs/stack-operations.en.md)
- [Installing with an AI assistant](docs/ai-install.md) (Chinese)
- [Memory Gateway full documentation](services/memory-gateway/README.md)
- [Model Gateway full documentation](services/model-gateway/README.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

## 📄 License

Licensed under the [Apache License 2.0](LICENSE). Keep the license file and copyright notices when using, modifying, or redistributing the project.
