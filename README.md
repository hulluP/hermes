# Hermes Agent Configuration

This repository holds the configuration, profiles, plugins, and tooling for the local Hermes multi-agent setup backed by the HAI (Hyperspace AI) desktop proxy.

---

## Repository layout

```
~/.hermes/
├── config.yaml                  # Root/default agent profile
├── SOUL.md                      # Root agent system prompt
├── profiles/                    # Per-role agent profiles
│   ├── product_manager/
│   ├── researcher/
│   ├── code-reviewer/
│   └── ...
├── plugins/
│   └── model-providers/custom/  # Custom LiteLLM provider plugin
├── hai-continuation-proxy/
│   ├── proxy.py                 # HAI continuation proxy (see below)
│   └── tests.py                 # Test suite for the proxy
├── debug.sh                     # Python runner using hermes venv
└── skills/                      # User-invocable /skills
```

---

## The infrastructure stack

```
Hermes (OpenAI SDK, stream=True)
        │
        ▼  port 7655
HAI Continuation Proxy  ◄── proxy.py
        │
        ▼  port 6655
HAI LiteLLM Proxy  (Hyperspace AI.app)
        │
        ▼
Anthropic / Gemini / etc.
```

### Why the continuation proxy exists

The HAI desktop app bundles a LiteLLM proxy that hard-caps all model output at **4096 tokens**, regardless of the `max_tokens` value the client sends. This is caused by LiteLLM bug [#8835](https://github.com/BerriAI/litellm/issues/8835): SAP-prefixed model names like `anthropic--claude-4.7-opus` don't match LiteLLM's internal metadata map, so it falls back to a 4096-token default. Any response longer than ~3000 words is silently truncated with `finish_reason='length'`.

Without the proxy, Hermes sees truncated `write_file` tool calls (broken JSON mid-argument) and retries the identical request — which always truncates again at the same point.

---

## HAI Continuation Proxy (`hai-continuation-proxy/proxy.py`)

A single-file Python HTTP proxy that listens on **port 7655** and forwards to HAI on **port 6655**.

### What it does

**Transparent passthrough**: Requests that complete normally pass straight through with no overhead.

**Tool call continuation** — when a tool call is truncated (`finish_reason='length'` + `tool_calls` present):

1. Inspects how far the JSON got:
   - *Only `path`, no `content` yet*: Asks the model for the file content as plain text, then reconstructs `{"path": "...", "content": "..."}` itself.
   - *Partial JSON content started*: Asks the model to continue the JSON fragment, appending until `json.loads()` succeeds.
2. Repeats up to 8 rounds (32 768 tokens total per response).
3. Returns a single stitched response with `finish_reason='tool_calls'` and valid JSON arguments.

**SSE streaming conversion**: Hermes uses the OpenAI Python SDK in `stream=True` mode and expects Server-Sent Events (`data: {...}\n\n`). The proxy always fetches non-streaming JSON from HAI (cleaner, no partial-parse issues), then converts the complete response to SSE format via `json_to_sse()` before returning it to Hermes.

**Text continuation**: Plain-text responses truncated mid-answer are also continued and stitched transparently.

### Running the proxy manually

```bash
# Uses hermes venv Python (required — system Python is 3.9, proxy needs 3.11)
bash ~/.hermes/debug.sh ~/.hermes/hai-continuation-proxy/proxy.py
```

### Running the tests

```bash
bash ~/.hermes/debug.sh ~/.hermes/hai-continuation-proxy/tests.py
```

Tests: 4 unit (no network) + 5 integration (require both proxies running). All 9 pass.

---

## start.sh (`/Users/D048098/SAPDevelop/2026LangChainHome/start.sh`)

The master startup script for the entire Hermes workspace. Run it from the workspace directory:

```bash
./start.sh          # start everything
./start.sh stop     # gracefully stop everything
./start.sh sync-key # re-sync the HAI API key into all profile .env files, then exit
```

### What it starts (in order)

1. **Guards**: Checks that the HAI LiteLLM proxy is running on `:6655`. Exits with instructions if not.

2. **HAI Continuation Proxy** on `:7655`: Kills any stale instance, starts `proxy.py` in the background using the hermes venv Python. Logs to `~/.hermes/logs/continuation-proxy.log`. Falls back to direct HAI on `:6655` if the proxy fails to start.

3. **Weekly snapshot**: Archives `start.sh`, workspace config, and VS Code settings into `~/.hermes/runtime/`. Commits them to a `snapshot` branch in this repo and pushes to origin. Runs at most once per calendar week (stamped by `.snapshot_week`).

4. **API key sync**: Reads the current HAI API key from `~/.claude/settings.json` (placed there by `hai configure claude-code`) and writes it as `OPENAI_API_KEY` into the `.env` file of every profile under `~/.hermes/profiles/`. This keeps all gateways authenticated after HAI rotates the key.

5. **All profile gateways**: Starts a `hermes gateway run` process for every profile directory found under `~/.hermes/profiles/`, plus the root `~/.hermes` gateway. Each gateway binds to the port defined in its `.env` (`API_SERVER_PORT`). The active profile's gateway starts first (the web UI connects there).

6. **Hermes Dashboard** on the port from `DASHBOARD_PORT` (default `:9119`).

7. **Workspace UI** (Vite dev server on `:1972`) — runs in the foreground. Ctrl-C stops everything via a `trap cleanup INT TERM`.

### Backup mechanism

`start.sh` automatically snapshots the workspace to this git repository weekly:

- Copies `start.sh`, workspace `.env`, `pnpm-workspace.yaml`, `.npmrc`, `hermes-config/`, and VS Code settings into `~/.hermes/runtime/`
- Stages all changes in `~/.hermes` with `git add -A`
- Commits to a dedicated `snapshot` branch (never touches `main`)
- Pushes to `origin` — so the snapshot branch is always current on the remote

This means even if the workspace directory is wiped, everything needed to restore the Hermes setup lives in this repo.

---

## Profile structure

Each profile under `profiles/<name>/` contains:

| File | Purpose |
|------|---------|
| `config.yaml` | Model, MCP servers, agent settings |
| `SOUL.md` | Agent system prompt / persona |
| `.env` | `OPENAI_API_KEY`, `API_SERVER_PORT`, gateway settings |
| `logs/` | Per-profile gateway and session logs |

All profiles point their `base_url` at `http://localhost:7655/litellm/v1` (the continuation proxy).

---

## Adding a new profile

1. Create `~/.hermes/profiles/<name>/`
2. Add `config.yaml` with `base_url: http://localhost:7655/litellm/v1`
3. Add `SOUL.md` with the agent's persona
4. Add `.env` with a unique `API_SERVER_PORT`
5. `start.sh` will pick it up automatically on next run
