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
- **Provider untouched**: `model.provider`, credentials and `base_url` stay
  exactly as you configured them — only the `model` id inside the request
  changes, and only when the classifier answered.
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
    "low": ["xiaomi/mimo-v2.6-flash"],
    "medium": ["anthropic/claude-haiku-5.5"],
    "high": ["moonshotai/kimi-k3.1-pro"]
  },
  "taskRules": {},
  "defaultModel": "xiaomi/mimo-v2.6-flash"
}
```

Model ids must be valid **for your provider** (for OpenRouter:
`provider/model`, e.g. `anthropic/claude-haiku-5.5`).

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
| `globalTiers` | Empty; arrays for `low`, `medium`, `high` |
| `taskRules` | Empty; task label → model string/array or tier object with `low`, `medium`, `high`, `any` (`models` is an alias for `any`) |
| `defaultModel` | Unset; last candidate and classifier-error fallback |
| `echoRouting` | `false`; log each routing decision (`task=…;score=…;model=…`) |

Priority for a classified turn: the task's models **for the resolved tier**
first; at `high` the global `high` chain precedes the task's `any` models
(escalation is never undercut), at `low`/`medium` the task's `any` models go
first; `defaultModel` is the last candidate. Duplicates are dropped, empty
entries ignored. The classifier decides the tier — decisions are used 1:1.

## Slash command

```text
/cortiq-router status            config, keys, last routing decision
/cortiq-router stats             today/week/all table + all-time breakdowns
/cortiq-router stats today|week|all
/cortiq-router stats recent [N]  newest N decisions (default 10, max 200)
/cortiq-router stats --json      full machine-readable aggregate
/cortiq-router stats reset       safe confirmation prompt
/cortiq-router stats reset yes   clear statistics, preserve last_decision
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
today   3      1         0.4287     543.2       1500.0
week    3      1         0.4287     543.2       1500.0
all     3      1         0.4287     543.2       1500.0

All time · By tier
Name    Count
------  -----
(none)  1
high    1
low     1

All time · By task
Name          Count
------------  -----
code          1
translation   1
unclassified  1

All time · By model
Name                      Count
------------------------  -----
xiaomi/mimo-v2.6-flash    2
moonshotai/kimi-k3.1-pro  1

All time · By status
Name                Count
------------------  -----
ok                  2
classifier_timeout  1
```

`stats today`, `stats week`, or `stats all` selects one period for both the
summary and all four breakdowns. Recent output is newest first:

```text
/cortiq-router stats recent 2

cortiq-hermes-router recent decisions (newest first, UTC)
Timestamp                 Status              Task          Score   Tier  Model                     Fallback  Cls ms  Original model
------------------------  ------------------  ------------  ------  ----  ------------------------  --------  ------  ----------------------
2026-10-25T12:00:02.000Z  classifier_timeout  unclassified  -       -     xiaomi/mimo-v2.6-flash    yes       1500.0  deepseek/deepseek-v3.2
2026-10-25T12:00:01.000Z  ok                  code          0.6061  high  moonshotai/kimi-k3.1-pro  no        87.4    deepseek/deepseek-v3.2
```

`stats --json` emits the complete `version`, `timezone`, `totals`, `daily`, and
`recent` object. Abridged `totals` example (the daily buckets use the same
aggregate shape):

```json
{
  "calls": 3,
  "by_tier": {"low": 1, "high": 1, "(none)": 1},
  "by_task": {"translation": 1, "code": 1, "unclassified": 1},
  "by_model": {"xiaomi/mimo-v2.6-flash": 2, "moonshotai/kimi-k3.1-pro": 1},
  "by_status": {"ok": 2, "classifier_timeout": 1},
  "fallback_count": 1,
  "avg_score": 0.4287,
  "score_count": 2,
  "avg_classifier_latency_ms": 543.167,
  "classifier_latency_count": 3,
  "max_classifier_latency_ms": 1500.0
}
```

Each recent JSON entry has the following fields (shown in full):

```json
{
  "timestamp": "2026-10-25T12:00:00.000Z",
  "task": "translation",
  "score": 0.2513,
  "tier": "low",
  "model": "xiaomi/mimo-v2.6-flash",
  "originalModel": "deepseek/deepseek-v3.2",
  "fallback": false,
  "status": "ok",
  "classifier_latency_ms": 42.1
}
```

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
python3 -m unittest discover -s tests -t .     # selection/config/classify/persistent stats logic
cd standalone && npm run check                 # lint + build + 61 unit tests
hermes plugins validate .                      # catalog gate: probe + security scan
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