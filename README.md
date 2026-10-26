# Automatic model selection for Hermes

[Русский](README.ru.md)

[![CI](https://github.com/infosave2007/cortiq-hermes-router/actions/workflows/ci.yml/badge.svg)](https://github.com/infosave2007/cortiq-hermes-router/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Hermes plugin](https://img.shields.io/badge/Hermes-plugin-6f42c1.svg)](https://hermes-agent.nousresearch.com/docs)
[![Node.js](https://img.shields.io/badge/Node.js-%3E%3D22-339933?logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)

**A Hermes plugin that picks the right model for every turn.**
Your rules send routine requests to an inexpensive model and hard problems to a
strong one — [AllaiGate](https://api.allaigate.com/) classifies the request
(task type + complexity 0…1), the plugin applies the family routing schema
(`taskRules` → `globalTiers` → `defaultModel`) and rewrites the outgoing
model **inside the Hermes process**. No proxy, no port, no second daemon.

Example rules — you choose the models:

| Task and estimate | Your choice |
| --- | --- |
| Routine request, low estimated complexity | Your lower-cost model |
| Coding task, high estimated complexity | Your stronger coding model |

In a [live test on 25 October 2026](docs/live-verification.md) three real
prompts were routed low → medium → high: `xiaomi/mimo-v2.6-flash`
(score 0.2513) → `anthropic/claude-haiku-5.5` (0.3393) →
`moonshotai/kimi-k3.1-pro` (0.6061) — each completion verified by the
`model` field of the upstream SSE response.

## How it works

```text
   you: "translate this…"                       you: "design exactly-once dedup…"
         │                                              │
         ▼                                              ▼
┌──────────────────────── Hermes turn ───────────────────────────────┐
│  cortiq-hermes-router · llm_request middleware (in-process)        │
│                                                                    │
│   1. classify   POST {routerUrl}/v1/route   ← CORTIQ_ROUTER_KEY    │
│        → { task, complexity.score, complexity.tier }                │
│   2. select     taskRules → globalTiers → defaultModel              │
│        (complexityBands optional; default = router tier)            │
│   3. rewrite    request.model = chosen model                        │
│        classifier down → defaultModel → your current model          │
└───────────────┬────────────────────────────────────┬───────────────┘
                ▼                                    ▼
      xiaomi/mimo-v2.6-flash              moonshotai/kimi-k3.1-pro
        (low tier)                              (high tier)
                │                                    │
                └──────────► OpenRouter ◄────────────┘
                          (your provider, unchanged)
```

- **Classification is bounded**: only the last user message goes to the
  classifier, truncated to `maxChars`, with a `timeoutMs` deadline; repeated
  turns reuse an in-process decision cache.
- **Explicit routing modes**: strings keep Hermes's current provider, `native: true`
  selects another configured Hermes provider, and endpoint objects perform a direct
  call with their own key/URL. The schema never silently interprets a provider
  object as a model string.
- **Fail-open**: classifier timeout/HTTP error/invalid payload → your
  `defaultModel`; no `defaultModel` → the current model; invalid config → one
  clear warning and routing pauses. The turn never fails because of routing.
- **Nothing leaves your machine except the classification text** (capped at
  `maxChars`) to the router you configured.

## Install

Requires a working [Hermes Agent](https://hermes-agent.nousresearch.com/docs)
and an AllaiGate API key.

### One command (recommended)

```sh
hermes plugins install infosave2007/cortiq-hermes-router
hermes plugins enable cortiq-hermes-router
```

`install` accepts a Git URL or the `owner/repo` shorthand, runs the security
scan and the capability probe, and shows the consent screen for the declared
capability `llm.model_override` (“choose which model host-owned LLM calls
use”). Pin an immutable commit with `--ref <40-char SHA>`; non-interactive
installs leave capabilities ungranted until you run
`hermes plugins capabilities cortiq-hermes-router`.

### Manual (Git checkout)

```sh
git clone https://github.com/infosave2007/cortiq-hermes-router.git \
  "${HERMES_HOME:-$HOME/.hermes}/plugins/cortiq-hermes-router"
hermes plugins enable cortiq-hermes-router
```

### Into the official catalog

The curated catalog lives in [`NousResearch/hermes-agent`
`plugin-catalog/`](https://github.com/NousResearch/hermes-agent). The entry
`plugin-catalog/cortiq-hermes-router.yaml` (fields: `name`, `repo`, `sha` —
a 40-character commit pin — `description`, `maintainer`, `tier`, `category`,
`docs_url`, `version`, `capabilities`) is submitted and awaiting review in
[PR #144419](https://github.com/NousResearch/hermes-agent/pull/144419).
After it merges, install by name: `hermes plugins install cortiq-hermes-router`.
Until then the two install paths above work today.

If the gateway is already running, restart it **from an external shell**:
`hermes gateway restart`. Gateway lifecycle commands are blocked inside a
chat session — do not run them from chat.

## Configure

Get a free AllaiGate API key at [api.allaigate.com](https://api.allaigate.com/)
(the router's classifier key, separate from any model-provider keys). Put it
in the environment that starts Hermes (never in this repository):

```sh
echo 'CORTIQ_ROUTER_KEY=your-allaigate-key' >> ~/.hermes/.env
```

Routing settings live in `~/.config/cortiq-hermes-router/config.json`
(`CORTIQ_ROUTER_CONFIG` selects another file). Start from
[cortiq-router.example.json](cortiq-router.example.json):

```json
{
  "globalTiers": {
    "low": ["gpt-6-luna"],
    "medium": ["gpt-5.6-terra"],
    "high": [{ "provider": "openrouter", "model": "moonshotai/kimi-k3.1-pro" }]
  },
  "taskRules": {},
  "defaultModel": "gpt-6-luna"
}
```

This is a safe mixed configuration for an active `openai-codex` / `codex_responses`
Hermes runtime: low and medium stay on the logged-in ChatGPT/Codex provider, while
high performs a real OpenRouter request and reads `OPENROUTER_API_KEY` only when it
executes. Never put that key in this JSON.

Model ids are forwarded unchanged to Hermes' active provider. OpenRouter uses
`provider/model` ids (for example `anthropic/claude-haiku-5.5`); ChatGPT/Codex
uses bare ids such as `gpt-6-luna` and `gpt-5.6-terra`.

| Setting | Default / meaning |
| --- | --- |
| `enabled` | `true`; explicit `false` disables future routing |
| `apiKeyEnv` | `CORTIQ_ROUTER_KEY` |
| `routerUrl` | `https://router.allaigate.com`; `/v1/route` is appended |
| `taxonomyId` | `data-assistant` |
| `timeoutMs` | `15000`; integer 1–120000 ms |
| `maxChars` | `12000`; integer 1–1000000 UTF-16 code units |
| `routerProfile` | `balanced`, `cost-saver`, or `quality-first` |
| `complexityBands` | Omitted (default): use the router tier. Optional `{ "low": 0.35, "medium": 0.65 }` overrides score thresholds |
| `globalTiers` | Empty; arrays for `low`, `medium`, `high` of model strings or endpoint objects (see [Providers](#providers-and-local-models)) |
| `taskRules` | Empty; task label → model string/array or tier object with `low`, `medium`, `high`, `any` (`models` is an alias for `any`) |
| `defaultModel` | Unset; last candidate and classifier-error fallback |
| `echoRouting` | `false`; log each routing decision (`task=…;score=…;model=…`) |

Priority for a classified turn: the task's models **for the resolved tier**
first; at `high` the global `high` chain precedes the task's `any` models
(escalation is never undercut), at `low`/`medium` the task's `any` models go
first; `defaultModel` is the last candidate. Duplicates are dropped, empty
entries ignored. The classifier decides the tier — decisions are used 1:1.

## Providers and local models

A tier entry has three explicit forms:

1. a **string** — use that model through the current Hermes provider (backward compatible);
2. a **direct endpoint object** — execute against its own `baseUrl`/provider preset;
3. a **native provider override** — ask Hermes to resolve another configured provider without a URL:

```json
{ "provider": "openai-codex", "model": "gpt-6-luna", "native": true }
```

`native: true` may contain only `provider`, `model`, optional `timeoutMs`, and
`native`; it cannot contain `baseUrl`, `apiKeyEnv`, `apiMode`, or `extraBody`.
That strict split prevents a silent provider/endpoint mismatch. Native overrides
use Hermes's configured credentials and require `llm.provider_override`; direct
objects read only their declared environment-variable key at call time.

A direct endpoint object names its own endpoint:

```json
{
  "globalTiers": {
    "low": [
      { "provider": "ollama", "model": "qwen3:8b" },
      "xiaomi/mimo-v2.6-flash"
    ],
    "medium": [
      { "provider": "lmstudio", "model": "qwen2.5-coder-14b-instruct" },
      "anthropic/claude-haiku-5.5"
    ],
    "high": [
      { "provider": "cortiq", "model": "qwen38-27b-q4tp", "baseUrl": "http://127.0.0.1:8080/v1" },
      { "provider": "openai", "model": "gpt-4.1", "apiKeyEnv": "OPENAI_API_KEY" }
    ]
  },
  "taskRules": {
    "coding": {
      "low": [{ "provider": "vllm", "model": "Qwen/Qwen2.5-Coder-7B", "baseUrl": "http://127.0.0.1:9000/v1" }],
      "any": ["anthropic/claude-haiku-5.5"]
    }
  },
  "defaultModel": "xiaomi/mimo-v2.6-flash"
}
```

For `cortiq`, use the id that the server lists at `GET /v1/models`. The
`cortiq` preset is `http://127.0.0.1:8080/v1` (the `cortiq serve` default port).

| Field | Meaning |
| --- | --- |
| `provider` | required; a preset name or any label for an explicit `baseUrl` |
| `native` | optional `true`: resolve this configured Hermes provider; cannot be combined with endpoint/auth fields |
| `model` | required; the id the endpoint expects (no `provider/` prefix needed for local servers) |
| `baseUrl` | `http(s)://…/v1`; defaults from the preset; **required** for `vllm`, `llamacpp` and unknown providers |
| `apiKeyEnv` | **name** of an environment variable holding the key — never the key itself |
| `apiMode` | `chat_completions` (default) or `anthropic_messages` |
| `timeoutMs` | default `60000`; maximum silence allowed between bytes, 1–120000 |
| `extraBody` | object merged into the request body; cannot set `model`, `messages` or `stream` |

Presets (`provider` → `baseUrl`, key variable):

| Provider | Base URL | Key variable |
| --- | --- | --- |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `ollama` | `http://127.0.0.1:11434/v1` | — |
| `lmstudio` | `http://127.0.0.1:1234/v1` | — |
| `cortiq` | `http://127.0.0.1:8080/v1` | — |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `deepseek` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `together` | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` |
| `anthropic` | `https://api.anthropic.com` (set `apiMode: anthropic_messages`) | `ANTHROPIC_API_KEY` |
| `vllm`, `llamacpp` | none — give `baseUrl` | optional |

**Validation** is strict, like the rest of the config: unknown fields, a
`baseUrl` without `http(s)://`, credentials in a URL, a `model` containing
spaces, and a plain-`http` endpoint with `apiKeyEnv` on a non-loopback host are
configuration errors (routing pauses and the previous model is kept). A missing
key variable is a **warning** and the call falls back at request time. Duplicate
entries are warned about. Keys are never written to config, logs, statistics or
command output.

**How direct calls run (`llm_execution`).** Object entries are executed by the
plugin against their own endpoint, with tool calls supported on both API modes.
Fail-open is unconditional: any failure re-runs the request through the model
Hermes was going to use, and the reason is counted in statistics:
`timeout`, `connection`, `http_<status>`, `invalid_json`, `invalid_stream`,
`stream_error`, `invalid_response`, `empty_response`, `missing_key`,
`unsupported_request`, `provider_override_not_granted`, `internal_error`.

- Hermes asks for streaming by default. Direct calls also stream upstream, but the
  reply is assembled before Hermes receives it, so routed direct calls do not show
  live token-by-token output.
- `anthropic_messages` calls are non-streaming upstream; image content is not converted.
- With an active `codex_responses` transport, direct Chat Completions and Anthropic
  requests are translated from `instructions`/`input`/Responses tools and their
  replies are adapted back to a Responses-compatible `output` object. Unsupported
  item types fail open as `unsupported_request`; Responses-only fields never reach
  the direct Chat Completions endpoint.
- Native provider overrides use Hermes's `ctx.llm` facade for text-only turns.
  They fail open as `native_tools_unsupported` when the active turn has tools,
  rather than silently dropping the tool contract.
- Direct endpoints need the **`llm.provider_override`** capability. It is declared
  at install (consent screen). An install upgraded from 0.2.x must be granted it:
  `hermes plugins capabilities cortiq-hermes-router`. Until then direct entries are
  skipped with a one-time warning, and the model is **not** rewritten to a local id.
- The standalone proxy in `standalone/` accepts string entries only.

**Checking endpoints without spending tokens**

```text
/cortiq-router providers         GET {baseUrl}/models, 3 s timeout, no completion;
                                 key shown only as "set" or "MISSING"
/cortiq-router test <tier|provider>   one prompt, max_tokens=8; runs only when you type it
```

## Cost control

A user turn can make dozens of provider calls (tool loops). The router classifies
the turn once and does not re-classify each tool step:

| Call within a turn (`api_call_count`) | Model choice |
| --- | --- |
| `1` (first call) | classify, then tier and `taskRules` as usual |
| `2` and later (tool-loop continuation) | **no classifier call**; the first `globalTiers.low` entry, or `defaultModel` if `low` is empty; `taskRules` are not consulted |
| new turn (new `turn_id`) | classify again |

Retries inside the same turn reuse that turn's decision from the cache. When
`api_call_count` is absent, the call is treated as a first call.

Choose the `low` entry with care: it serves every tool step of long turns. Pick
the cheapest model that still produces reliable tool calls. Statistics report
tool steps as `step_continuation_count` (totals and per day), a `Tool step`
column in `stats recent`, and status `step_continuation`.

## Slash command

```text
/cortiq-router status            config, keys, last routing decision
/cortiq-router stats             today/week/all table + all-time breakdowns
/cortiq-router stats today|week|all
/cortiq-router stats recent [N]  newest N decisions (default 10, max 200)
/cortiq-router stats --json      full machine-readable aggregate
/cortiq-router stats reset       safe confirmation prompt
/cortiq-router stats reset yes   clear statistics, preserve last_decision
/cortiq-router providers               configured endpoints: GET {baseUrl}/models, no completion, keys hidden
/cortiq-router test <tier|provider>   one cheap prompt through that endpoint (explicit only)
/cortiq-router decide <prompt>   classify without routing (prints JSON)
/cortiq-router on | off          toggle routing for this profile
/cortiq-router help              usage
```

`decide` output shape:

```json
{
  "task": "qa", "score": 0.3393, "tier": "medium",
  "model": "anthropic/claude-haiku-5.5",
  "candidates": ["anthropic/claude-haiku-5.5"],
  "fallback": false, "classifier": "ok",
  "source": "router", "requestId": "req_…", "profile": "balanced"
}
```

## Statistics

Every `llm_request` middleware invocation adds one **text-free decision record**:
UTC timestamp, task, score, tier, selected and original models, fallback flag,
status, and classifier latency when a classifier call was made. Cached decisions
are counted too; their per-call classifier latency is `null`. Prompt and response
text is never stored. State writes use Hermes's atomic plugin-state bridge;
a state error leaves routing unaffected and the in-memory statistics intact.

The aggregate contains all-time totals, per-UTC-day totals, and a 200-entry
recent ring buffer. `week` means the current UTC day plus the previous six days.
A v0.1 `last_decision` is migrated once without deleting it; `stats reset yes`
clears only statistics and also preserves `last_decision`.

Statuses are `ok`, `classifier_error` (HTTP, missing key, or invalid response),
`classifier_timeout`, `invalid_config`, and `disabled` (routing off, capability
not granted, or no classifiable request). These are plugin routing decisions,
not upstream delivery receipts; Hermes's own log may still name the original
model.

```text
/cortiq-router stats

cortiq-hermes-router statistics (UTC; week = today + previous 6 days)

Period  Calls  Fallback  Avg score  Avg cls ms  Max cls ms
------  -----  --------  ---------  ----------  ----------
today   685    0         0.4176     1731.1      9840.2
week    1485   0         0.4184     1558.5      9840.2
all     1485   0         0.4184     1558.5      9840.2

All time · By tier
Name    Count
------  -----
low     1445
medium  29
high    11

All time · By task
Name      Count
--------  -----
(none)    1354
chitchat  102
code      22
qa        7

All time · By model
Name                        Count
--------------------------  -----
xiaomi/mimo-v2.6-flash      1445
anthropic/claude-haiku-5.5  29
moonshotai/kimi-k3.1-pro    11

All time · By status
Name               Count
-----------------  -----
step_continuation  1354
ok                 131
```

`stats today`, `stats week`, or `stats all` selects one period for both the
summary and every breakdown. Recent output is newest first:

```text
/cortiq-router stats recent 2

cortiq-hermes-router recent decisions (newest first, UTC)
Timestamp                 Status  Task      Score   Tier    Model                       Fallback  Cls ms  Original model          Provider  Tool step
------------------------  ------  --------  ------  ------  --------------------------  --------  ------  ----------------------  --------  ---------
2026-10-26T07:09:18.742Z  ok      code      0.4796  medium  anthropic/claude-haiku-5.5  no        2855.1  xiaomi/mimo-v2.6-flash  -         no
2026-10-26T07:04:35.657Z  ok      chitchat  0.4522  medium  anthropic/claude-haiku-5.5  no        9840.2  xiaomi/mimo-v2.6-flash  -         no
```

`stats --json` emits the complete `version`, `timezone`, `totals`, `daily`, and
`recent` object. Abridged `totals` example (the daily buckets use the same
aggregate shape):

```json
{
  "calls": 1485,
  "by_tier": {"low": 1445, "medium": 29, "high": 11},
  "by_task": {"(none)": 1354, "chitchat": 102, "code": 22, "qa": 7},
  "by_model": {"xiaomi/mimo-v2.6-flash": 1445, "anthropic/claude-haiku-5.5": 29, "moonshotai/kimi-k3.1-pro": 11},
  "by_status": {"step_continuation": 1354, "ok": 131},
  "by_provider": {"openai-codex": 1445, "openrouter": 40},
  "by_route_kind": {"native": 1445, "direct": 40},
  "fallback_count": 0,
  "avg_score": 0.418444,
  "score_count": 131,
  "avg_classifier_latency_ms": 1558.541,
  "classifier_latency_count": 131,
  "max_classifier_latency_ms": 9840.241
}
```

Each recent JSON entry has the following fields (shown in full):

```json
{
  "timestamp": "2026-10-26T07:09:18.742Z",
  "task": "code",
  "score": 0.47955617,
  "tier": "medium",
  "model": "anthropic/claude-haiku-5.5",
  "originalModel": "xiaomi/mimo-v2.6-flash",
  "fallback": false,
  "status": "ok",
  "classifier_latency_ms": 2855.147,
  "provider": null,
  "route_kind": "native",
  "step_continuation": false
}
```

Tool-loop steps and endpoint outcomes are visible in the aggregate: `by_provider`
uses the actual selected provider (including the active Hermes provider for
strings), `by_route_kind` separates `native`, `direct`, `unchanged`, and
`fallback`, and `execution_fallback_count`, `fallback_reasons`, and
`fallback_by_provider` count direct/native attempts that fell open. Recent
records include both provider and route kind. Stored statistics from 0.2.x/0.3.x
are upgraded in place without discarding counters.

## Standalone OpenAI-compatible proxy (optional)

For harnesses that are not Hermes — Codex, Cline, OpenCode, scripts — the same
mechanics ship as a dependency-free Node proxy in [standalone/](standalone/):

```sh
cd standalone && npm ci
node dist/server.js serve            # http://127.0.0.1:8791
```

- `POST /v1/chat/completions` is classified and re-routed; every other
  `/v1/*` path passes through untouched (`GET /v1/models` works).
- Responses stream back **byte-for-byte** (`text/event-stream`), adding only
  the `X-Cortiq-Router: task=…;score=…;model=…` response header.
- Upstream connection refused/timeout → `502` with
  `{"error":{…,"code":"upstream_unavailable"}}`; the process stays up.
- Subcommands: `setup` (prints the exact `hermes config set` commands that
  point Hermes at the proxy — with your current values shown, keys masked, and
  a backup written to `~/.config/cortiq-hermes-router/hermes-backup.json`;
  `--apply` executes them), `decide "<prompt>"` (dry-run JSON), `status`
  (config/env/upstream/classifier checks). Flags: `--port`, `--host`,
  `--config`; config env: `CORTIQ_ROUTER_CONFIG`.
- Extra config keys understood **by the proxy only**: `host` (default
  `127.0.0.1`), `port` (default `8791`), `upstream` `{ "url":
  "https://openrouter.ai/api/v1", "apiKeyEnv": "OPENROUTER_API_KEY",
  "timeoutMs": 0 }` (0 = no overall deadline). The upstream key is read from
  the environment — never from files in this repository.

For Hermes users the plugin is the recommended path: no port, no second
process, no `base_url` rewrite.

## Tests and validation

```sh
python3 -m unittest discover -s tests -t .     # selection, config, mixed providers, HTTP execution, cost control, stats, slash
cd standalone && npm run check                 # lint + build + 61 unit tests
hermes plugins validate .                      # catalog gate: probe + security scan
hermes plugins doctor .                        # real runtime contracts (import + registration)
```

Full real-world evidence — three tiers, concurrent streams, empty/oversized/
message-less requests, dead upstream, dead classifier, invalid config,
graceful shutdown — is in [docs/live-verification.md](docs/live-verification.md).

## Contributing

Issues and PRs welcome. Keep the two test suites green, run
`hermes plugins validate .` before opening a PR, never commit secrets (only
environment variable names), and keep the README pair in sync — English here,
Russian in [README.ru.md](README.ru.md).

## License

[MIT](LICENSE) © 2026 Cortiq Team.