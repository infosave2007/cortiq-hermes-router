# Live verification — cortiq-hermes-router

[README](../README.md) · [Русский](#кратко-по-русски)

**PASS, 2026-10-25 (UTC).** Everything below is real output captured on the
verification machine (Node.js v26.12.1 — CI runs 22.22.2, Python 3.14.9,
Hermes 0.22.5). Real keys were supplied **via environment only**
(`CORTIQ_ROUTER_KEY`, `OPENROUTER_API_KEY`) — no key appears in this file or
in the repository. Classifier decisions are used **1:1**: `complexityBands`
was omitted, so the router's own tier drives selection.

## 1. Unit tests

```text
$ python3 -m unittest discover -s tests -t .
..........................................................
----------------------------------------------------------------------
Ran 58 tests in 0.006s

OK

$ cd standalone && npm run check
ok    cortiq-router.example.json parses
ok    VERSION 0.1.0 matches package.json
ok    node --check: 16 files
built dist/server.js (54000 bytes, 8 modules, node --check ok)
ℹ tests 61
ℹ pass 61
ℹ fail 0
```

Python tests cover the pure selection logic (tiers/bands/taskRules/fallback),
the config schema (defaults table = the documented table, validation bounds,
loopback-only HTTP, unknown-setting errors) and the classifier client (payload
structure, response validation, degradation paths). Node tests mirror the same
selection semantics plus `X-Cortiq-Router` header build/parse, hop-by-hop
response-header filtering, SSE frame parsing, CLI argv parsing and the
`setup` plan (current values masked, exact `hermes config set` commands,
revert commands).

## 2. `hermes plugins validate` (isolated `HERMES_HOME=/tmp/hermes-cortiq-test`)

```text
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — not declared
✓ config schema — not declared
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no tools to check
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
◆ Isolation — runs in the plugin host (plugins.isolation: host)

Validation passed.
```

Declared capabilities (`llm.model_override`) match real registrations:
`provides_middleware: [llm_request]` ↔ `ctx.register_middleware("llm_request", …)`.
Security scan verdict: **safe** (level `dangerous` would fail admission).

## 3. Three real tiers through the proxy mechanism

Local proxy (`node standalone/dist/server.js serve --port 8791`) with the
shipped example config (owner tiers verbatim, no `complexityBands`), real
AllaiGate classifier, real OpenRouter upstream. Requests:
`POST /v1/chat/completions`, `stream: true`, `max_tokens: 300`,
original `model: deepseek/deepseek-v3.2`.

| # | Prompt (abridged) | Classify (`X-Cortiq-Router`) | Served model (upstream `model` field) | Time | Reply head (~200 chars) |
| --- | --- | --- | --- | --- | --- |
| 1 | «Переведи фразу на английский: “Как дела?”» | `task=translation;score=0.2513;model=xiaomi/mimo-v2.6-flash` (tier **low**) | `xiaomi/mimo-v2.6-flash` | 1 113 ms | `**"How are you?"** или **"How's it going?"**` |
| 2 | «Объясни разницу между TCP и UDP простыми словами и приведи примеры.» | `task=qa;score=0.3393;model=anthropic/claude-haiku-5.5` (tier **medium**) | `anthropic/claude-haiku-5.5` | 3 330 ms | `**TCP** и **UDP** — два основных протокола передачи данных по сети. Оба отправляют данные, но делают это по-разному…` |
| 3 | “Two-phase commit deadlocks under network partition — design a fix with Raft + idempotent participants, prove safety/liveness, …” | `task=code;score=0.6061;model=moonshotai/kimi-k3.1-pro` (tier **high**) | `moonshotai/kimi-k3.1-pro` | 11 473 ms | first deltas are reasoning content; the completion continues in-stream (129 437 bytes total) |

Raw headers for each request:

```text
TIER-1 low:  HTTP 200 in 1113 ms
  X-Cortiq-Router : task=translation;score=0.2513;model=xiaomi/mimo-v2.6-flash
  content-type    : text/event-stream
  served model    : xiaomi/mimo-v2.6-flash
  SSE framing     : start=data: terminator=[DONE] ok bytes=4086

TIER-2 medium: HTTP 200 in 3330 ms
  X-Cortiq-Router : task=qa;score=0.3393;model=anthropic/claude-haiku-5.5
  content-type    : text/event-stream
  served model    : anthropic/claude-haiku-5.5
  SSE framing     : terminator=[DONE] ok bytes=22785

TIER-3 high: HTTP 200 in 11473 ms
  X-Cortiq-Router : task=code;score=0.6061;model=moonshotai/kimi-k3.1-pro
  content-type    : text/event-stream
  served model    : moonshotai/kimi-k3.1-pro
  SSE framing     : start=data: terminator=[DONE] ok bytes=129437
```

Scores grew monotonically across the three prompts (0.2513 → 0.3393 → 0.6061)
and the router's tiers low/medium/high mapped 1:1 onto the configured models.
The response `model` field quoted above comes from the **upstream SSE body** —
proof that each completion was really generated by the selected model.

## 4. Three real turns through the plugin (isolated `HERMES_HOME`)

Plugin installed by the manual path into
`/tmp/hermes-cortiq-test/plugins/cortiq-hermes-router`, enabled with
`hermes plugins enable cortiq-hermes-router`, capability
`llm.model_override` granted via the legacy gate
(`plugins.entries.cortiq-hermes-router.llm.allow_model_override: true`).
Isolated config: `model.provider openrouter`,
`model.default deepseek/deepseek-v3.2` (a neutral original model, so every
swap is visible). Each turn: `hermes chat -q "<prompt>"`. Evidence is the
plugin's persisted `last_decision` (written by the middleware immediately
before it rewrites `request.model`).

```text
########## low ##########   exit=0  duration=23s
{
  "task": "translation", "score": 0.2779017, "tier": "low",
  "model": "xiaomi/mimo-v2.6-flash",
  "candidates": ["xiaomi/mimo-v2.6-flash"],
  "originalModel": "deepseek/deepseek-v3.2", "fallback": false
}

########## med ##########    exit=0  duration=11s
{
  "task": "qa", "score": 0.31463775, "tier": "low",
  "model": "xiaomi/mimo-v2.6-flash",
  "candidates": ["xiaomi/mimo-v2.6-flash"],
  "originalModel": "deepseek/deepseek-v3.2", "fallback": false
}

########## high ##########   exit=0  duration=243s
{
  "task": "code", "score": 0.7099389, "tier": "high",
  "model": "moonshotai/kimi-k3.1-pro",
  "candidates": ["moonshotai/kimi-k3.1-pro", "xiaomi/mimo-v2.6-flash"],
  "originalModel": "deepseek/deepseek-v3.2", "fallback": false
}
```

Every turn: the classifier answered over the network (real
`POST https://router.allaigate.com/v1/route`), the middleware replaced the
model, Hermes completed the request through OpenRouter with the selected
model, and the session continued normally. Decisions are recorded exactly as
the classifier returned them. Note the second turn: this run's verdict for the
TCP/UDP prompt was `tier=low` (score 0.3146) — the same prompt classified
`medium` (0.3393/0.3377) in other runs; both values are shown unedited.

## 5. Mechanics battery (aggressive cases)

All runs against the same local proxy; process never crashed.

**Concurrent streams (2 parallel requests):**

```text
wall time: 12655 ms
parallel-1 (medium prompt): HTTP 200 in 2477 ms
  X-Cortiq-Router : task=qa;score=0.3393;model=anthropic/claude-haiku-5.5
  served model    : anthropic/claude-haiku-5.5   terminator=[DONE] ok bytes=22351
parallel-2 (high prompt): HTTP 200 in 12651 ms
  X-Cortiq-Router : task=code;score=0.6061;model=moonshotai/kimi-k3.1-pro
  served model    : moonshotai/kimi-k3.1-pro     terminator=[DONE] ok bytes=129373
```

Both streams were classified independently and delivered complete SSE frames.

**Empty user message** (`content: ""`): nothing to classify → no routing:

```text
HTTP 400 in 111 ms
  X-Cortiq-Router : task=norouting;score=na;model=deepseek/deepseek-v3.2
  content-type    : application/json
```

The proxy did not call the classifier, kept the request's model, and the
upstream's own `400` for an empty message passed through untouched.

**Very long prompt (~65 000 chars > `maxChars` 12 000):**

```text
HTTP 200 in 2612 ms
  X-Cortiq-Router : task=chitchat;score=0.1884;model=xiaomi/mimo-v2.6-flash
  served model    : xiaomi/mimo-v2.6-flash   terminator=[DONE] ok bytes=16763
```

Classification ran on the first 12 000 UTF-16 units (unit-tested boundary);
generation completed normally.

**Request WITHOUT `messages` field:**

```text
HTTP 400 in 98 ms
  X-Cortiq-Router : task=norouting;score=na;model=deepseek/deepseek-v3.2
```

Predictable pass-through, no crash.

**Transparent pass-through** `GET /v1/models` → `HTTP 200, 467 models listed`
(no routing header — only `/v1/chat/completions` is routed).

**Upstream disabled (dead port 9999) → 502 JSON, process stays alive:**

```text
HTTP/1.1 502 Bad Gateway
content-type: application/json; charset=utf-8
x-cortiq-router: task=chitchat;score=0.0779;model=xiaomi/mimo-v2.6-flash
{"error":{"message":"upstream unavailable: fetch failed","type":"cortiq_router_error","code":"upstream_unavailable"}}

process still alive after 502: healthz={'ok': true, ...}
```

**Classifier unreachable → silent defaultModel (log to stderr only):**

```text
HTTP 200 in 880 ms
  X-Cortiq-Router : task=unclassified;score=na;model=xiaomi/mimo-v2.6-flash
  served model    : xiaomi/mimo-v2.6-flash   terminator=[DONE] ok bytes=7817
stderr:
cortiq-hermes-router: classifier unreachable (fetch failed) — using fallback chain
cortiq-hermes-router: task=unclassified score=na tier=na model=xiaomi/mimo-v2.6-flash
```

**Invalid config → clear validation error, exit 1:**

```text
$ node dist/server.js serve --config cortiq-bad.json   # {"typoField": true}
exit code: 1
stderr: error: Invalid cortiq-hermes-router configuration field: unknown setting
```

**Graceful shutdown:**

```text
$ kill -TERM <proxy>
exit code: 0
stderr: cortiq-hermes-router: SIGTERM received — shutting down
```

## 6. Reproduce

```sh
git clone https://github.com/infosave2007/cortiq-hermes-router.git
cd cortiq-hermes-router

# unit tests (no network, no keys)
python3 -m unittest discover -s tests -t .
(cd standalone && npm ci && npm run check)

# plugin validation gate used by the catalog
hermes plugins validate .

# live routing (paid calls: AllaiGate + your model provider)
export CORTIQ_ROUTER_KEY=...      # AllaiGate key, never commit it
export OPENROUTER_API_KEY=...     # upstream key
cp cortiq-router.example.json ~/.config/cortiq-hermes-router/config.json
hermes plugins install https://github.com/infosave2007/cortiq-hermes-router
hermes plugins enable cortiq-hermes-router
hermes plugins capabilities cortiq-hermes-router   # grant llm.model_override

# standalone proxy (optional)
cd standalone && npm ci
node dist/server.js serve --port 8791
node dist/server.js decide "explain TCP vs UDP"
node dist/server.js status
```

Limits worth stating: one machine, one account, one run per prompt shown
(classifier verdicts near a tier boundary vary between runs — values above
are unedited); the high-tier completion's first SSE deltas are reasoning
content, so its "reply head" is empty while the stream continues; the capture
script's optional final JSON export crashed *after* all sections had already
been written (harness bug, not the proxy). Timings include classification
(~0.4–2.9 s) plus generation.

---

## Кратко по-русски

**Все тесты зелёные, 2026-10-25, реальные ключи только в env.**

- Юнит-тесты: Python — **58 OK**, Node (`npm run check`) — **61/61**, плюс
  `hermes plugins validate` — все проверки ✓, security scan **safe**.
- Три тира вживую через прокси: low → `xiaomi/mimo-v2.6-flash`
  (score 0.2513), medium → `anthropic/claude-haiku-5.5` (0.3393),
  high → `moonshotai/kimi-k3.1-pro` (0.6061); выбранная модель подтверждена
  полем `model` в SSE-ответе провайдера, заголовок `X-Cortiq-Router` на месте,
  стрим завершается `[DONE]`.
- Три реальных поворота через **плагин** в изолированном `HERMES_HOME`:
  middleware переписал `model` (original `deepseek/deepseek-v3.2` → mimo/kimi),
  факты записаны в `last_decision` плагина; решения классификатора записаны
  как есть.
- Агрессивные кейсы: 2 параллельных стрима — оба 200 и корректны; пустое
  сообщение и запрос без `messages` — предсказуемый pass-through
  (`task=norouting`), без падений; промпт ~65 000 символов — классификация по
  первым 12 000, ответ корректен; мёртвый upstream — **502 JSON**
  (`upstream_unavailable`) и процесс жив; мёртвый классификатор — молчаливый
  `defaultModel` + строка в stderr; битый конфиг — понятная ошибка и exit 1;
  SIGTERM — graceful shutdown, exit 0.