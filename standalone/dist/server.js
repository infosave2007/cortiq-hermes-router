#!/usr/bin/env node
/**
 * cortiq-hermes-router standalone proxy — GENERATED FILE, do not edit.
 * Rebuild with: npm run build   (scripts/bundless.mjs)
 * Source: lib/*.js · zero runtime dependencies (node:http + global fetch).
 */
// ── lib/select.js ───────────────────────────────────────────────────
/**
 * Pure model-selection logic for cortiq-hermes-router.
 *
 * Port of the family routing table (cortiq-gateway/src/routing.rs →
 * cortiq-pi-router/src/routing-table.ts → this repository). No I/O here:
 * every step is a pure function of the config and the classifier decision,
 * so the whole priority chain is unit-testable.
 *
 * Priority for a classified request:
 *   1. task rule models FOR the resolved complexity tier;
 *   2. tier=high → the global `high` chain first, then the task's `any`
 *      models (escalation must not be undercut); tier=low/medium → the
 *      task's `any` models first, then the global tier chain;
 *   3. `defaultModel` as the last candidate.
 * Classifier failure → `defaultModel` directly (`fallback: true`).
 */

/** Complexity tiers in ascending order. */
const TIERS = ['low', 'medium', 'high'];

/** Normalize a task rule value to an array of model ids. */
function asList(value) {
  if (value === undefined || value === null) return [];
  return Array.isArray(value) ? value.map(String) : [String(value)];
}

/**
 * Parse a user-supplied task value into `{ any, per }`.
 * Accepts a plain string, an array, or a tier object whose `any` key may be
 * spelled `models`.
 */
function parseTaskRule(value) {
  if (value === null || value === undefined) return { any: [], per: {} };
  if (typeof value === 'string') return { any: [value], per: {} };
  if (Array.isArray(value)) return { any: value.map(String), per: {} };
  if (typeof value === 'object') {
    const any = asList(value.any ?? value.models);
    const per = {};
    for (const tier of TIERS) {
      if (tier in value) per[tier] = asList(value[tier]);
    }
    return { any, per };
  }
  return { any: [String(value)], per: {} };
}

/**
 * Resolve the effective tier from a complexity score.
 * Without custom bands the router's tier stands as-is; bands are inclusive
 * on the upper bound (`score <= low → low`, `score <= medium → medium`).
 */
function resolveTier(score, routerTier, bands) {
  if (!bands) return routerTier;
  if (score <= bands.low) return 'low';
  if (score <= bands.medium) return 'medium';
  return 'high';
}

/**
 * Build the routing table for a config shape.
 * Returns `{ candidatesFor(task, tier), tierOf(score, routerTier) }`.
 */
function createRoutingTable(config) {
  const globalTiers = config.globalTiers ?? {};
  const defaultModel = config.defaultModel ?? '';
  const bands = config.complexityBands ?? null;
  const rules = Object.create(null);
  for (const [task, raw] of Object.entries(config.taskRules ?? {})) {
    const rule = parseTaskRule(raw);
    if (rule.any.length > 0 || Object.keys(rule.per).length > 0) rules[task] = rule;
  }

  const tierOf = (score, routerTier) => resolveTier(score, routerTier, bands);

  const candidatesFor = (task, tier) => {
    const out = [];
    const push = (id) => {
      if (id.length > 0 && !out.includes(id)) out.push(id);
    };
    const rule = Object.hasOwn(rules, task) ? rules[task] : undefined;
    const tierChain = globalTiers[tier] ?? (defaultModel ? [defaultModel] : []);

    // 1. Task rule models for this exact tier.
    if (rule?.per[tier]) for (const id of rule.per[tier]) push(id);

    const escalate = tier === 'high' && globalTiers.high !== undefined;
    if (escalate) {
      // 2a. high: global high chain first, then the task's any-tier models.
      for (const id of tierChain) push(id);
      if (rule) for (const id of rule.any) push(id);
    } else {
      // 2b. low/medium: task's any-tier models first, then the global chain.
      if (rule) for (const id of rule.any) push(id);
      for (const id of tierChain) push(id);
    }

    // 3. defaultModel as the last resort.
    push(defaultModel);

    const head = out[0];
    let taskRuleMatched = false;
    if (rule && head) {
      taskRuleMatched = rule.per[tier]?.[0] === head || rule.any[0] === head;
    }
    return { models: out, taskRuleMatched };
  };

  return { candidatesFor, tierOf };
}

/**
 * Choose a model for a classified (or failed) request.
 *
 * @param config  `{ globalTiers, taskRules, defaultModel, complexityBands }`
 * @param decision `null` when the classifier was unavailable/disabled,
 *                 otherwise `{ taskLabel, complexityScore, complexityTier }`
 * @returns `{ task, score, tier, model, candidates, fallback, taskRuleMatched }`
 *          — `model` is the first candidate; an empty string means nothing
 *          is configured and the caller should keep the request's model.
 */
function selectModel(config, decision) {
  if (!decision) {
    const fb = config.defaultModel ? [config.defaultModel] : [];
    return {
      task: 'unclassified',
      score: null,
      tier: null,
      model: fb[0] ?? '',
      candidates: fb,
      fallback: true,
      taskRuleMatched: false,
    };
  }
  const table = createRoutingTable(config);
  const tier = table.tierOf(decision.complexityScore, decision.complexityTier);
  const { models, taskRuleMatched } = table.candidatesFor(decision.taskLabel, tier);
  const model = models[0] ?? '';
  return {
    task: decision.taskLabel,
    score: decision.complexityScore,
    tier,
    model,
    candidates: models,
    fallback: model === '',
    taskRuleMatched,
  };
}

// ── lib/config.js ───────────────────────────────────────────────────
/**
 * Configuration for cortiq-hermes-router.
 *
 * The schema follows the cortiq-*-router family: `globalTiers` / `taskRules`
 * / `defaultModel` plus the shared AllaiGate classifier settings, extended
 * with proxy-specific `host`, `port` and `upstream` sections. Validation is
 * strict: an unknown setting is an error, not a warning.
 */

import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

/** Every accepted top-level setting. */
const CONFIG_FIELDS = new Set([
  'enabled', 'apiKeyEnv', 'routerUrl', 'taxonomyId', 'timeoutMs', 'maxChars',
  'routerProfile', 'complexityBands', 'globalTiers', 'taskRules',
  'defaultModel', 'echoRouting', 'host', 'port', 'upstream',
]);
const UPSTREAM_FIELDS = new Set(['url', 'apiKeyEnv', 'timeoutMs']);
const TIER_NAMES = ['low', 'medium', 'high'];
const PROFILES = ['balanced', 'cost-saver', 'quality-first'];
const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const MODEL_ID = /^[^/\s]+\/\S+$/;

const isObject = (v) => !!v && typeof v === 'object' && !Array.isArray(v);

function invalid(field) {
  throw new Error(`Invalid cortiq-hermes-router configuration field: ${field}`);
}

function ensureModel(value, path) {
  if (typeof value !== 'string' || !MODEL_ID.test(value)) invalid(path);
  return value;
}

function ensureModels(value, path, single = false) {
  if (single && typeof value === 'string') return ensureModel(value, path);
  if (!Array.isArray(value)) invalid(path);
  for (const item of value) ensureModel(item, path);
}

/** Validate a loopback-or-HTTPS base URL without credentials/query/hash. */
function ensureBaseUrl(value, field) {
  if (typeof value !== 'string') invalid(field);
  let url;
  try {
    url = new URL(value);
  } catch {
    invalid(field);
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    invalid(field);
  }
  if (url.protocol === 'http:' && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) {
    invalid(`${field} (HTTP requires loopback)`);
  }
  return value.replace(/\/+$/, '');
}

/** Validate a raw config object and return the normalized config. */
function parseConfig(raw) {
  if (!isObject(raw)) invalid('root');
  for (const field of Object.keys(raw)) if (!CONFIG_FIELDS.has(field)) invalid('unknown setting');

  for (const field of ['enabled', 'echoRouting']) {
    if (raw[field] !== undefined && typeof raw[field] !== 'boolean') invalid(field);
  }
  for (const field of ['apiKeyEnv', 'taxonomyId']) {
    if (raw[field] !== undefined && (typeof raw[field] !== 'string' || !raw[field].trim())) invalid(field);
  }
  if (raw.apiKeyEnv !== undefined && !ENV_NAME.test(String(raw.apiKeyEnv))) invalid('apiKeyEnv');
  for (const [field, max] of [['timeoutMs', 120000], ['maxChars', 1000000]]) {
    if (raw[field] !== undefined
      && (typeof raw[field] !== 'number' || !Number.isInteger(raw[field]) || raw[field] < 1 || raw[field] > max)) {
      invalid(field);
    }
  }
  if (raw.routerProfile !== undefined
    && (typeof raw.routerProfile !== 'string' || !PROFILES.includes(raw.routerProfile))) {
    invalid('routerProfile');
  }

  const routerUrl = raw.routerUrl === undefined ? 'https://router.allaigate.com'
    : ensureBaseUrl(raw.routerUrl, 'routerUrl');

  if (raw.complexityBands !== undefined) {
    const b = raw.complexityBands;
    if (!isObject(b) || Object.keys(b).some((k) => !['low', 'medium'].includes(k))
      || typeof b.low !== 'number' || typeof b.medium !== 'number'
      || !Number.isFinite(b.low) || !Number.isFinite(b.medium)
      || b.low < 0 || b.low >= b.medium || b.medium > 1) {
      invalid('complexityBands');
    }
  }

  if (raw.globalTiers !== undefined) {
    if (!isObject(raw.globalTiers)) invalid('globalTiers');
    for (const [tier, value] of Object.entries(raw.globalTiers)) {
      if (!TIER_NAMES.includes(tier)) invalid('globalTiers');
      ensureModels(value, 'globalTiers');
    }
  }

  if (raw.taskRules !== undefined) {
    if (!isObject(raw.taskRules)) invalid('taskRules');
    for (const value of Object.values(raw.taskRules)) {
      if (isObject(value)) {
        for (const [key, list] of Object.entries(value)) {
          if (![...TIER_NAMES, 'any', 'models'].includes(key)) invalid('taskRules');
          ensureModels(list, 'taskRules', true);
        }
      } else {
        ensureModels(value, 'taskRules', true);
      }
    }
  }

  if (raw.defaultModel !== undefined && raw.defaultModel !== '') ensureModel(raw.defaultModel, 'defaultModel');

  if (raw.host !== undefined && (typeof raw.host !== 'string' || !raw.host.trim() || /\s|[/\\]/.test(raw.host))) {
    invalid('host');
  }
  if (raw.port !== undefined
    && (typeof raw.port !== 'number' || !Number.isInteger(raw.port) || raw.port < 1 || raw.port > 65535)) {
    invalid('port');
  }

  const upstream = { ...(raw.upstream ?? {}) };
  if (raw.upstream !== undefined && !isObject(raw.upstream)) invalid('upstream');
  for (const field of Object.keys(upstream)) if (!UPSTREAM_FIELDS.has(field)) invalid('upstream');
  upstream.url = upstream.url === undefined ? 'https://openrouter.ai/api/v1'
    : ensureBaseUrl(upstream.url, 'upstream.url');
  if (upstream.apiKeyEnv === undefined) upstream.apiKeyEnv = 'OPENROUTER_API_KEY';
  if (!ENV_NAME.test(String(upstream.apiKeyEnv))) invalid('upstream.apiKeyEnv');
  if (upstream.timeoutMs === undefined) upstream.timeoutMs = 0;
  if (typeof upstream.timeoutMs !== 'number' || !Number.isInteger(upstream.timeoutMs)
    || upstream.timeoutMs < 0 || upstream.timeoutMs > 3600000) {
    invalid('upstream.timeoutMs');
  }

  return {
    enabled: true,
    apiKeyEnv: 'CORTIQ_ROUTER_KEY',
    routerUrl: 'https://router.allaigate.com',
    taxonomyId: 'data-assistant',
    timeoutMs: 15000,
    maxChars: 12000,
    routerProfile: 'balanced',
    globalTiers: {},
    taskRules: {},
    defaultModel: '',
    echoRouting: false,
    host: '127.0.0.1',
    port: 8791,
    ...raw,
    routerUrl,
    upstream,
  };
}

/**
 * Resolve the config path: `--config` wins, then `CORTIQ_ROUTER_CONFIG`
 * (relative paths resolve against the current directory), then
 * `~/.config/cortiq-hermes-router/config.json`.
 */
function resolveConfigPath(explicitPath, env = process.env) {
  if (explicitPath) return resolve(explicitPath);
  if (env.CORTIQ_ROUTER_CONFIG) return resolve(env.CORTIQ_ROUTER_CONFIG);
  return join(homedir(), '.config', 'cortiq-hermes-router', 'config.json');
}

/** State directory for backups and local files (~/.config/cortiq-hermes-router). */
function routerStateDir(env = process.env) {
  const path = env.CORTIQ_ROUTER_CONFIG ? resolve(env.CORTIQ_ROUTER_CONFIG) : null;
  const dir = path ? path.replace(/[/\\][^/\\]+$/, '') : join(homedir(), '.config', 'cortiq-hermes-router');
  return dir || join(homedir(), '.config', 'cortiq-hermes-router');
}

/**
 * Load and validate the config.
 * An explicitly selected missing file (`--config` or `CORTIQ_ROUTER_CONFIG`)
 * is an error; a missing default file means empty routing defaults.
 */
async function loadConfig(explicitPath, env = process.env) {
  const path = resolveConfigPath(explicitPath, env);
  const explicit = Boolean(explicitPath || env.CORTIQ_ROUTER_CONFIG);
  let contents;
  try {
    contents = await readFile(path, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT' && !explicit) return parseConfig({});
    throw new Error(`Cannot read cortiq-hermes-router configuration file: ${path}`);
  }
  let raw;
  try {
    raw = JSON.parse(contents);
  } catch {
    throw new Error(`Cortiq configuration must be valid JSON: ${path}`);
  }
  return parseConfig(raw);
}

// ── lib/classify.js ─────────────────────────────────────────────────
/**
 * AllaiGate classifier client (POST {routerUrl}/v1/route).
 *
 * The request payload matches the cortiq-*-router family exactly —
 * `input` is a STRUCT (`{ text }`), never a bare string — and the response
 * is validated before use. Every failure mode (missing key, timeout, HTTP
 * error, invalid payload, unreachable host) degrades to `null` plus one
 * stderr line; the proxy itself never fails because of the classifier.
 */

/** Build the `/v1/route` payload (pure — truncated to `maxChars`). */
function buildRouteBody({ text, profile, taxonomyId, maxChars = 12000 }) {
  const body = {
    input: { text: String(text ?? '').slice(0, maxChars) },
    options: {
      policy_profile: profile ?? 'balanced',
      allow_oracle: true,
      return_explanation: false,
      top_k: 3,
    },
  };
  if (taxonomyId) body.taxonomy_id = taxonomyId;
  return body;
}

/**
 * Validate the subset of the classifier response that routing needs.
 * Returns `null` (never coerces) for anything malformed.
 */
function parseRouteDecision(value) {
  if (!value || typeof value !== 'object') return null;
  const data = value;
  const d = data.decision;
  if (!d || typeof d !== 'object') return null;
  const c = d.complexity;
  if (!c || typeof c !== 'object') return null;
  const unit = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1;
  if (typeof d.task_label !== 'string' || !d.task_label.trim()
    || !unit(c.score)
    || typeof c.tier !== 'string' || !['low', 'medium', 'high'].includes(c.tier)
    || !unit(d.confidence)
    || typeof data.request_id !== 'string' || !data.request_id
    || typeof d.is_novel !== 'boolean'
    || typeof d.source !== 'string' || !d.source) {
    return null;
  }
  return {
    taskLabel: d.task_label,
    complexityScore: c.score,
    complexityTier: c.tier,
    confidence: d.confidence,
    requestId: data.request_id,
    isNovel: d.is_novel,
    source: d.source,
  };
}

/**
 * Classify text. Returns a validated decision or `null` — never throws.
 * `log` receives one-line stderr diagnostics (silent for the request itself).
 */
async function classify(config, text, { env = process.env, log = () => {} } = {}) {
  const key = env[config.apiKeyEnv];
  if (!key) {
    log(`cortiq-hermes-router: ${config.apiKeyEnv} is not set — classification skipped, using fallback chain`);
    return null;
  }
  const base = String(config.routerUrl).replace(/\/+$/, '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  try {
    const response = await fetch(`${base}/v1/route`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${key}`,
      },
      body: JSON.stringify(buildRouteBody({
        text,
        profile: config.routerProfile,
        taxonomyId: config.taxonomyId,
        maxChars: config.maxChars,
      })),
      signal: controller.signal,
      redirect: 'error',
    });
    if (!response.ok) {
      log(`cortiq-hermes-router: classifier HTTP ${response.status} — using fallback chain`);
      return null;
    }
    const decision = parseRouteDecision(await response.json());
    if (!decision) {
      log('cortiq-hermes-router: invalid classifier response — using fallback chain');
      return null;
    }
    return decision;
  } catch (error) {
    const reason = error?.name === 'AbortError' ? `timeout after ${config.timeoutMs}ms` : (error?.message ?? error);
    log(`cortiq-hermes-router: classifier unreachable (${reason}) — using fallback chain`);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// ── lib/sse.js ──────────────────────────────────────────────────────
/**
 * Minimal SSE (text/event-stream) frame parser.
 *
 * The proxy streams upstream bytes through unchanged — this helper exists
 * for tests, `live-verification` tooling and any harness that needs to
 * inspect frames without buffering the whole response. It follows the
 * HTML Living Standard event-stream parsing rules: frames are terminated
 * by a blank line, `data:` lines accumulate into one payload, comment
 * lines (`:`) are collected but never dispatched, and a trailing
 * unterminated block is pending, not dispatched.
 */

/**
 * Parse a complete (or chunked) event-stream string into frames.
 * @returns `[{ event, data, id, retry, comments }]`
 */
function parseSseFrames(chunk) {
  const text = String(chunk ?? '').replace(/\r\n/g, '\n');
  const blocks = text.split('\n\n');
  const trailing = text.endsWith('\n\n') ? [] : [blocks.pop() ?? ''];
  const frames = [];

  for (const block of blocks) {
    if (!block) continue;
    const comments = [];
    const dataLines = [];
    let event = 'message';
    let id = null;
    let retry = null;
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) {
        comments.push(line.slice(1).trimStart());
        continue;
      }
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'data') dataLines.push(value);
      else if (field === 'event') event = value;
      else if (field === 'id') id = value;
      else if (field === 'retry' && /^\d+$/.test(value)) retry = Number(value);
      // Unknown fields are ignored, per spec.
    }
    if (dataLines.length > 0) {
      frames.push({ event, data: dataLines.join('\n'), id, retry, comments });
    }
  }

  void trailing; // unterminated trailing block is pending — not dispatched
  return frames;
}

/** Parse a frame's data as JSON; returns `null` when it is not JSON. */
function sseJson(frame) {
  if (!frame || typeof frame.data !== 'string') return null;
  try {
    return JSON.parse(frame.data);
  } catch {
    return null;
  }
}

// ── lib/routing.js ──────────────────────────────────────────────────
/**
 * Pure helpers for request payloads and response headers.
 *
 * - `extractLastUserText` — pull the last user message for classification
 *   (string content or array-of-parts content; images/tool output ignored).
 * - `buildRoutingHeader` / `parseRoutingHeader` — the `X-Cortiq-Router`
 *   response header (`task=…;score=…;model=…`) and its parser for tests
 *   and verification tooling.
 * - `filterResponseHeaders` — copy upstream response headers minus the
 *   hop-by-hop set, so streaming (SSE) passes through byte-for-byte.
 */

/** Hop-by-hop headers that must not be forwarded (plus our transport overrides). */
const HOP_BY_HOP = new Set([
  'connection', 'keep-alive', 'transfer-encoding', 'te', 'trailer', 'upgrade',
  'proxy-authenticate', 'proxy-authorization', 'host',
]);

/** Truncate to UTF-16 code units, the family's `maxChars` semantics. */
function truncateText(text, maxChars) {
  return String(text ?? '').slice(0, maxChars);
}

function contentToText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .filter((part) => part && (part.type === 'text' || part.type === 'input_text')
        && typeof part.text === 'string')
      .map((part) => part.text)
      .join('\n');
  }
  return '';
}

/** Last `role: "user"` message text from an OpenAI `messages` array. */
function extractLastUserText(messages) {
  if (!Array.isArray(messages)) return '';
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message && message.role === 'user') return contentToText(message.content);
  }
  return '';
}

/** Header-token sanitizer: printable ASCII minus `;`, spaces and friends. */
function sanitizeToken(value) {
  return String(value ?? '').replace(/[^A-Za-z0-9_.:/@+,-]/g, '_');
}

/** Build the `X-Cortiq-Router` value: `task=…;score=…;model=…`. */
function buildRoutingHeader({ task, score, model }) {
  const scorePart = Number.isFinite(score) ? Number(score).toFixed(4) : 'na';
  return `task=${sanitizeToken(task)};score=${scorePart};model=${sanitizeToken(model)}`;
}

/** Parse an `X-Cortiq-Router` value back into `{ task, score, model }`. */
function parseRoutingHeader(value) {
  const out = { task: '', score: null, model: '' };
  if (typeof value !== 'string') return out;
  for (const part of value.split(';')) {
    const eq = part.indexOf('=');
    if (eq === -1) continue;
    const key = part.slice(0, eq).trim();
    const val = part.slice(eq + 1).trim();
    if (key === 'task') out.task = val;
    else if (key === 'model') out.model = val;
    else if (key === 'score') out.score = val === 'na' || val === '' ? null : Number(val);
  }
  if (out.score !== null && !Number.isFinite(out.score)) out.score = null;
  return out;
}

/** Copy response headers (Headers / array / plain object) minus hop-by-hop. */
function filterResponseHeaders(headers) {
  const out = {};
  const add = (name, value) => {
    const key = String(name).toLowerCase();
    if (HOP_BY_HOP.has(key)) return;
    if (value === undefined || value === null) return;
    out[key] = Array.isArray(value) ? value.join(', ') : String(value);
  };
  if (!headers) return out;
  if (typeof headers.forEach === 'function' && !Array.isArray(headers)) {
    headers.forEach((value, name) => add(name, value));
  } else if (Array.isArray(headers)) {
    for (const [name, value] of headers) add(name, value);
  } else {
    for (const [name, value] of Object.entries(headers)) add(name, value);
  }
  return out;
}

/** `true` for `text/event-stream` (any parameters, any case). */
function isEventStream(contentType) {
  return /^text\/event-stream\b/i.test(String(contentType ?? ''));
}

// ── lib/hermes.js ───────────────────────────────────────────────────
/**
 * Hermes integration: `setup` reads the current `model.*` values, snapshots
 * them to a backup file and prints (or applies) the exact `hermes config`
 * commands that point Hermes at the local proxy.
 *
 * Nothing here touches the config file directly — every change goes through
 * the official `hermes config set` / `unset` CLI, and a backup is always
 * written before `--apply`.
 */

import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';


/** Hermes settings this tool reads and (on `--apply`) rewrites. */
const HERMES_KEYS = ['model.default', 'model.provider', 'model.base_url', 'model.api_key'];

/** Placeholder written to `model.api_key` — the proxy does not check it. */
const LOCAL_KEY_PLACEHOLDER = 'cortiq-local';

/** Never echo a real key: show only its length. */
function maskValue(value) {
  if (value === null || value === undefined || value === '') return '<unset>';
  return `<set, ${String(value).length} chars>`;
}

/** Run the `hermes` CLI. Returns `{ ok, out, status }` — never throws. */
function runHermes(args) {
  const result = spawnSync('hermes', args, { encoding: 'utf8', timeout: 30000 });
  if (result.error) return { ok: false, out: '', status: -1, error: result.error.code === 'ENOENT' ? 'hermes CLI not found in PATH' : result.error.message };
  const out = `${result.stdout ?? ''}`.trim();
  return { ok: result.status === 0, out, status: result.status, error: result.status === 0 ? '' : `${result.stderr ?? ''}`.trim() };
}

/**
 * Extract the `model:` block from config.yaml without a YAML dependency.
 * Returns `{ found, values }` with RAW (explicitly written) values only —
 * `found: false` for a key means it was not set explicitly.
 */
function parseModelBlock(yamlText) {
  const values = {};
  let inModel = false;
  for (const line of String(yamlText ?? '').split('\n')) {
    if (/^#/.test(line)) continue;
    if (/^model:\s*(#.*)?$/.test(line)) { inModel = true; continue; }
    if (/^\S/.test(line)) { inModel = false; continue; }
    if (!inModel) continue;
    const match = /^  ([A-Za-z0-9_]+):\s*(.*)$/.exec(line);
    if (!match) continue;
    let value = match[2].replace(/\s+#.*$/, '').trim();
    if ((value.startsWith('"') && value.endsWith('"') && value.length > 1)
      || (value.startsWith("'") && value.endsWith("'") && value.length > 1)) {
      value = value.slice(1, -1);
    }
    values[match[1]] = value;
  }
  return { found: Object.keys(values).length > 0, values };
}

/**
 * Read current settings: raw config.yaml model block (what revert restores)
 * plus `hermes config get` resolved values (shown to the user, keys masked).
 */
function readHermesValues(run = runHermes) {
  const pathResult = run(['config', 'path']);
  if (!pathResult.ok) return { ok: false, error: pathResult.error || `hermes config path failed: ${pathResult.out || pathResult.error}` };
  const configPath = pathResult.out;

  let raw = { found: false, values: {} };
  try {
    raw = parseModelBlock(readFileSync(configPath, 'utf8'));
  } catch {
    raw = { found: false, values: {} };
  }

  const resolved = {};
  for (const key of HERMES_KEYS) {
    const result = run(['config', 'get', key]);
    resolved[key] = result.ok && result.out ? result.out : '';
  }

  return { ok: true, configPath, raw: raw.values, resolved };
}

/**
 * Build the full `setup` plan (pure). Returns:
 * `{ lines, commands, revert, backupPath, backup, apply }` where `commands`
 * and `revert` are `{ key, value }[]` (`value === null` → `unset`).
 */
function buildSetupPlan({ values, host = '127.0.0.1', port = 8791, apply = false, backupPath, now = new Date() }) {
  const base = `http://${host}:${port}/v1`;
  const raw = values.raw ?? {};
  const resolved = values.resolved ?? {};

  const commands = [
    { key: 'model.base_url', value: base },
    { key: 'model.api_key', value: LOCAL_KEY_PLACEHOLDER },
  ];
  // provider is deliberately NOT changed — the router keeps the fact provider.
  const revert = [];
  for (const key of ['model.default', 'model.provider', 'model.base_url']) {
    const short = key.slice('model.'.length);
    const original = raw[short] !== undefined ? raw[short] : '';
    if (original) revert.push({ key, value: original });
    else revert.push({ key, value: null });
  }
  revert.push(raw.api_key !== undefined
    ? { key: 'model.api_key', value: raw.api_key }
    : { key: 'model.api_key', value: null });

  const lines = [];
  lines.push('Current Hermes model settings (read from Hermes, keys masked):');
  lines.push(`  config file      ${values.configPath}`);
  lines.push(`  model.default    ${resolved['model.default'] || '<unset>'}`);
  lines.push(`  model.provider   ${resolved['model.provider'] || '<unset>'}   (kept as-is — the router does not change your provider)`);
  lines.push(`  model.base_url   ${resolved['model.base_url'] || '<unset>'}`);
  lines.push(`  model.api_key    ${maskValue(resolved['model.api_key'])}${raw.api_key !== undefined ? ' (explicit in config.yaml)' : ' (resolved, not written in config.yaml)'}`);
  lines.push('');
  lines.push(`Backup: ${backupPath}`);
  lines.push('');
  lines.push('Commands that point Hermes through cortiq-hermes-router:');
  for (const cmd of commands) lines.push(`  hermes config set ${cmd.key} ${cmd.value}`);
  lines.push(`  hermes config set model.provider ${resolved['model.provider'] || '<your-provider>'}   # only if you want to pin it explicitly`);
  lines.push('');
  lines.push('To revert (original values are in the backup file):');
  for (const cmd of revert) {
    lines.push(cmd.value === null
      ? `  hermes config unset ${cmd.key}`
      : `  hermes config set ${cmd.key} ${cmd.value}`);
  }
  lines.push(`  # or restore from ${backupPath}`);
  lines.push('');
  lines.push('WARNING: restart the Hermes gateway from an EXTERNAL shell:');
  lines.push('  hermes gateway restart');
  lines.push('  Gateway lifecycle commands are blocked inside a chat session —');
  lines.push('  do not run them from chat, and expect to re-run setup after provider changes.');
  if (apply) {
    lines.push('');
    lines.push('APPLY MODE: writing the backup, then running the set commands above.');
  } else {
    lines.push('');
    lines.push('Dry run — nothing was changed. Re-run with --apply to write the backup and apply.');
  }

  const backup = {
    savedAt: now.toISOString(),
    hermesConfigPath: values.configPath,
    values: {
      'model.default': { explicit: raw.default !== undefined, value: raw.default ?? null },
      'model.provider': { explicit: raw.provider !== undefined, value: raw.provider ?? null },
      'model.base_url': { explicit: raw.base_url !== undefined, value: raw.base_url ?? null },
      'model.api_key': { explicit: raw.api_key !== undefined, value: raw.api_key ?? null },
    },
    revertCommands: revert,
    note: 'Keys are stored only when explicitly written in config.yaml; resolved values are masked.',
  };

  return { lines, commands, revert, backupPath, backup, apply };
}

/** Path of the setup backup file inside the router state directory. */
function setupBackupPath(env = process.env) {
  return `${routerStateDir(env)}/hermes-backup.json`;
}

/**
 * Write the backup (rotating an existing one to `hermes-backup.previous.json`),
 * then apply the commands through `hermes config set`.
 */
function writeBackup(backupPath, backup) {
  const dir = backupPath.replace(/[/\\][^/\\]+$/, '');
  mkdirSync(dir, { recursive: true });
  if (existsSync(backupPath)) {
    writeFileSync(`${backupPath}.previous.json`.replace('.json.previous.json', '.previous.json'),
      readFileSync(backupPath, 'utf8'));
  }
  writeFileSync(backupPath, `${JSON.stringify(backup, null, 2)}\n`, { mode: 0o600 });
  return backupPath;
}

/** Apply `{key, value}[]` through the hermes CLI. Returns per-command results. */
function applySetup(commands, run = runHermes) {
  return commands.map((cmd) => {
    const args = cmd.value === null ? ['config', 'unset', cmd.key] : ['config', 'set', cmd.key, cmd.value];
    const result = run(args);
    return { key: cmd.key, ok: result.ok, detail: result.ok ? 'ok' : (result.error || result.out || 'failed') };
  });
}

// ── lib/server.js ───────────────────────────────────────────────────
/**
 * The proxy itself: node:http + global fetch, zero runtime dependencies.
 *
 * - `POST /v1/chat/completions` — classify the last user message with
 *   AllaiGate, select a model per the family schema, rewrite `model`,
 *   forward to the upstream and stream the response back UNCHANGED, adding
 *   only the `X-Cortiq-Router: task=…;score=…;model=…` response header.
 * - any other `/v1/*` — transparent pass-through (GET /v1/models etc.).
 * - classifier failures degrade silently to the fallback chain (stderr log);
 *   upstream connection failures/timeouts answer `502` with a JSON error.
 */

import http from 'node:http';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';




/** Maximum accepted request body (tool schemas + history live in here). */
const BODY_LIMIT = 32 * 1024 * 1024;

const logToStderr = (message) => { process.stderr.write(`${message}\n`); };

/** Read the whole request body, rejecting anything over `BODY_LIMIT`. */
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > BODY_LIMIT) {
        reject(Object.assign(new Error('request body too large'), { status: 413, code: 'payload_too_large' }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', (error) => reject(error));
  });
}

function sendJson(res, status, payload, headers = {}) {
  const body = Buffer.from(`${JSON.stringify(payload)}\n`);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': String(body.length),
    ...headers,
  });
  res.end(body);
}

function errorPayload(message, code, type = 'cortiq_router_error') {
  return { error: { message, type, code } };
}

/** Create the HTTP server. `classifyFn`/`selectFn` are injectable for tests. */
function createProxyServer({
  config,
  env = process.env,
  log = logToStderr,
  classifyFn = classify,
  selectFn = selectModel,
} = {}) {
  const upstreamBase = String(config.upstream.url).replace(/\/+$/, '');
  const upstreamKey = env[config.upstream.apiKeyEnv];
  let warnedNoKey = false;

  /** Forward a buffered request to the upstream and stream the response. */
  async function forward(req, res, { path, method, body, routingHeader }) {
    const headers = {
      'content-type': req.headers['content-type'] ?? 'application/json',
      accept: 'application/json, text/event-stream',
      'accept-encoding': 'identity',
    };
    if (upstreamKey) headers.authorization = `Bearer ${upstreamKey}`;
    else if (!warnedNoKey) {
      warnedNoKey = true;
      log(`cortiq-hermes-router: ${config.upstream.apiKeyEnv} is not set — upstream called without a key`);
    }

    // Upstream bases commonly carry the /v1 prefix themselves (…/api/v1);
    // drop the client's duplicated leading segment so …/api/v1/v1 never happens.
    let target = `${upstreamBase}${path}`;
    try {
      const basePath = new URL(upstreamBase).pathname.replace(/\/+$/, '');
      const baseSegment = basePath.split('/').filter(Boolean).pop();
      const requestUrl = new URL(path, 'http://cortiq.local');
      const segments = requestUrl.pathname.split('/').filter(Boolean);
      if (baseSegment && segments[0] === baseSegment) segments.shift();
      target = `${upstreamBase}/${segments.join('/')}${requestUrl.search}`;
    } catch {
      // keep the naive concatenation
    }

    const fromClient = new AbortController();
    res.on('close', () => { if (!res.writableEnded) fromClient.abort(); });
    const timeoutSignal = config.upstream.timeoutMs > 0
      ? AbortSignal.timeout(config.upstream.timeoutMs)
      : null;
    const signals = [fromClient.signal, ...(timeoutSignal ? [timeoutSignal] : [])];
    const signal = signals.length === 1 ? signals[0] : AbortSignal.any(signals);

    let upstream;
    try {
      upstream = await fetch(target, {
        method,
        headers,
        body: body && body.length ? body : undefined,
        signal,
        redirect: 'error',
      });
    } catch (error) {
      if (fromClient.signal.aborted) return; // client went away — nothing to answer
      const reason = error?.name === 'TimeoutError' || error?.name === 'AbortError'
        ? `timeout after ${config.upstream.timeoutMs}ms`
        : (error?.message ?? String(error));
      log(`cortiq-hermes-router: upstream unavailable (${reason})`);
      if (!res.headersSent) {
        sendJson(res, 502, errorPayload(`upstream unavailable: ${reason}`, 'upstream_unavailable'), {
          ...(routingHeader ? { 'x-cortiq-router': routingHeader } : {}),
        });
      }
      return;
    }

    const outHeaders = filterResponseHeaders(upstream.headers);
    if (routingHeader) outHeaders['x-cortiq-router'] = routingHeader;
    if (!outHeaders['x-cortiq-router']) outHeaders['x-cortiq-router'] = 'task=transparent;score=na;model=na';
    res.writeHead(upstream.status, outHeaders);

    if (!upstream.body || method === 'HEAD') {
      res.end();
      return;
    }
    if (upstream.status >= 500) log(`cortiq-hermes-router: upstream HTTP ${upstream.status} for ${path}`);
    try {
      await pipeline(Readable.fromWeb(upstream.body), res);
    } catch (error) {
      if (!fromClient.signal.aborted) log(`cortiq-hermes-router: stream aborted: ${error?.message ?? error}`);
      res.destroy();
    }
  }

  /** Smart path: classify → select → rewrite `model` → forward. */
  async function handleChat(req, res, path) {
    let payload;
    try {
      const raw = await readBody(req);
      payload = JSON.parse(raw.toString('utf8'));
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('body must be a JSON object');
      }
    } catch (error) {
      const status = error.status ?? 400;
      sendJson(res, status, errorPayload(
        status === 413 ? 'request body too large' : `invalid JSON body: ${error.message}`,
        status === 413 ? 'payload_too_large' : 'invalid_request',
      ));
      return;
    }

    let selection = null;
    let decision = null;
    const originalModel = typeof payload.model === 'string' ? payload.model : '';
    if (config.enabled) {
      const text = truncateText(extractLastUserText(payload.messages), config.maxChars);
      if (text.trim()) {
        // Nothing to classify → nothing to route on: keep the request's model.
        decision = await classifyFn(config, text, { env, log });
        selection = selectFn(config, decision);
      } else {
        selection = {
          task: 'norouting', score: null, tier: null,
          model: originalModel, candidates: [], fallback: false, taskRuleMatched: false,
        };
      }
    } else {
      selection = {
        task: 'disabled', score: null, tier: null,
        model: originalModel,
        candidates: [], fallback: false, taskRuleMatched: false,
      };
    }

    const chosen = selection.model || originalModel;
    if (!chosen) {
      sendJson(res, 500, errorPayload(
        'no model available: configure defaultModel or globalTiers (and defaultModel as fallback)',
        'no_model_configured',
      ), { 'x-cortiq-router': buildRoutingHeader({ task: selection.task, score: selection.score, model: 'none' }) });
      return;
    }

    payload.model = chosen;
    const routingHeader = buildRoutingHeader({ task: selection.task, score: selection.score, model: chosen });
    if (config.echoRouting) {
      log(`cortiq-hermes-router: task=${selection.task} score=${selection.score ?? 'na'} tier=${selection.tier ?? 'na'} model=${chosen}`);
    }

    await forward(req, res, {
      path,
      method: 'POST',
      body: Buffer.from(JSON.stringify(payload)),
      routingHeader,
    });
  }

  const server = http.createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://localhost');
    const path = `${url.pathname}${url.search}`;

    if (req.method === 'GET' && url.pathname === '/healthz') {
      sendJson(res, 200, {
        ok: true,
        name: 'cortiq-hermes-router',
        enabled: config.enabled,
        upstream: config.upstream.url,
        classifier: config.routerUrl,
      });
      return;
    }

    if (!url.pathname.startsWith('/v1/')) {
      sendJson(res, 404, errorPayload(`not found: ${url.pathname} (use /v1/...)`, 'not_found'));
      return;
    }

    const handle = async () => {
      if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
        await handleChat(req, res, path);
        return;
      }
      if (req.method === 'GET' || req.method === 'HEAD') {
        await forward(req, res, { path, method: req.method, body: undefined, routingHeader: null });
        return;
      }
      const body = await readBody(req);
      await forward(req, res, { path, method: req.method, body, routingHeader: null });
    };

    handle().catch((error) => {
      log(`cortiq-hermes-router: internal error: ${error?.stack ?? error}`);
      if (!res.headersSent) {
        sendJson(res, 500, errorPayload(`internal error: ${error?.message ?? error}`, 'internal_error'));
      } else {
        res.destroy();
      }
    });
  });

  return server;
}

/** Graceful shutdown: stop accepting, drop idle keep-alives, then force-close. */
function shutdown(server, { graceMs = 5000 } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    server.close(finish);
    server.closeIdleConnections?.();
    const timer = setTimeout(() => {
      server.closeAllConnections?.();
      finish();
    }, graceMs);
    timer.unref?.();
    server.once('close', () => { clearTimeout(timer); finish(); });
  });
}

// ── lib/cli.js ──────────────────────────────────────────────────────
/**
 * CLI entry: `serve` (default) | `setup` | `decide` | `status`.
 *
 * Flags: `--port N`, `--host H`, `--config PATH`, `--apply` (setup only),
 * `--help`, `--version`. No dependencies: argv parsing, JSON output and
 * human-readable status lines are all plain stdio.
 */







const VERSION = '0.1.0';

const COMMANDS = ['serve', 'setup', 'decide', 'status', 'help', 'version'];

const HELP = `cortiq-hermes-router ${VERSION} — OpenAI-compatible model router for Hermes

Usage:
  cortiq-hermes-router [serve] [--port N] [--host H] [--config PATH]
  cortiq-hermes-router setup [--apply] [--port N] [--host H]
  cortiq-hermes-router decide "<prompt>" [--config PATH]
  cortiq-hermes-router status [--config PATH]
  cortiq-hermes-router --help | --version

Commands:
  serve     Run the local proxy (default when no command is given).
            POST /v1/chat/completions is classified and re-routed; other
            /v1/* requests pass through to the upstream unchanged.
  setup     Show the current hermes model.* values (keys masked), save a
            backup to ~/.config/cortiq-hermes-router/hermes-backup.json and
            print the exact "hermes config set" commands. With --apply the
            backup is written and the commands are executed.
  decide    Classify the prompt and print the routing decision as JSON
            ({task, score, tier, model, candidates}) without proxying.
  status    Check config validity, env keys, upstream /v1/models and the
            classifier. Exit code 1 when a check FAILs.

Flags:
  --port N     Listen port (default 8791, or config "port")
  --host H     Listen host (default 127.0.0.1, or config "host")
  --config P   Config file (default ~/.config/cortiq-hermes-router/config.json,
               or $CORTIQ_ROUTER_CONFIG)
  --apply      setup: write the backup and run the printed commands

Environment:
  CORTIQ_ROUTER_CONFIG   Alternate config file
  CORTIQ_ROUTER_KEY      AllaiGate classifier key (name configurable)
  OPENROUTER_API_KEY     Upstream key (name configurable via upstream.apiKeyEnv)

Docs: https://github.com/infosave2007/cortiq-hermes-router
`;

/** Parse argv (pure). Throws on unknown flags/commands. */
function parseArgs(argv) {
  const out = {
    command: null, positional: [], port: null, host: null, config: null,
    apply: false, help: false, version: false,
  };
  const valueFlags = { '--port': 'port', '--host': 'host', '--config': 'config' };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    let name = arg;
    let inline = null;
    const eq = arg.indexOf('=');
    if (arg.startsWith('--') && eq !== -1) {
      name = arg.slice(0, eq);
      inline = arg.slice(eq + 1);
    }
    if (name in valueFlags) {
      const value = inline ?? argv[++i];
      if (value === undefined) throw new Error(`missing value for ${name}`);
      if (name === '--port') {
        const port = Number(value);
        if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`invalid --port: ${value}`);
        out.port = port;
      } else if (name === '--host') {
        out.host = value;
      } else {
        out.config = value;
      }
      continue;
    }
    if (arg === '--apply') { out.apply = true; continue; }
    if (arg === '--help' || arg === '-h') { out.help = true; continue; }
    if (arg === '--version') { out.version = true; continue; }
    if (arg.startsWith('-') && arg !== '-') throw new Error(`unknown option: ${arg}`);
    if (out.command === null && COMMANDS.includes(arg)) { out.command = arg; continue; }
    if (out.command === null && !COMMANDS.includes(arg)) throw new Error(`unknown command: ${arg}`);
    out.positional.push(arg);
  }
  out.command ??= 'serve';
  return out;
}

function readStdin() {
  return new Promise((resolve) => {
    if (process.stdin.isTTY) { resolve(''); return; }
    const chunks = [];
    process.stdin.on('data', (chunk) => chunks.push(chunk));
    process.stdin.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    process.stdin.on('error', () => resolve(''));
  });
}

const print = (io, stream, text) => io[stream].write(text);

async function cmdServe(args, io) {
  const config = await loadConfig(args.config, io.env);
  const port = args.port ?? config.port;
  const host = args.host ?? config.host;
  const server = createProxyServer({ config, env: io.env, log: (m) => io.stderr.write(`${m}\n`) });

  await new Promise((resolveListen, rejectListen) => {
    server.once('error', rejectListen);
    server.listen(port, host, resolveListen);
  });
  const address = server.address();
  const hostPart = host.includes(':') ? `[${host}]` : host;
  const keyState = (name) => (io.env[name] ? 'set' : 'MISSING');
  print(io, 'stdout', [
    `cortiq-hermes-router listening on http://${hostPart}:${address.port}`,
    `  upstream   ${config.upstream.url}   (key $${config.upstream.apiKeyEnv}: ${keyState(config.upstream.apiKeyEnv)})`,
    `  classifier ${config.routerUrl}   (key $${config.apiKeyEnv}: ${keyState(config.apiKeyEnv)})`,
    `  config     ${resolveConfigPath(args.config, io.env)}`,
    `  routing    ${config.enabled ? `enabled (profile ${config.routerProfile})` : 'DISABLED — pass-through'}`,
    '  stop with Ctrl-C (graceful shutdown)',
    '',
  ].join('\n'));

  await new Promise((resolveExit) => {
    const stop = async (signal) => {
      io.stderr.write(`\ncortiq-hermes-router: ${signal} received — shutting down\n`);
      await shutdown(server);
      resolveExit();
    };
    process.once('SIGINT', () => { stop('SIGINT'); });
    process.once('SIGTERM', () => { stop('SIGTERM'); });
  });
  return 0;
}

async function cmdDecide(args, io) {
  const config = await loadConfig(args.config, io.env);
  let prompt = args.positional.join(' ').trim();
  if (!prompt) prompt = (await readStdin()).trim();
  if (!prompt) {
    io.stderr.write('usage: decide "<prompt>"  (or pipe the prompt on stdin)\n');
    return 2;
  }
  const decision = await classify(config, prompt, { env: io.env, log: (m) => io.stderr.write(`${m}\n`) });
  const selection = selectModel(config, decision);
  const out = {
    task: selection.task,
    score: selection.score,
    tier: selection.tier,
    model: selection.model,
    candidates: selection.candidates,
    fallback: selection.fallback,
    classifier: decision ? 'ok' : 'unavailable',
    source: decision?.source ?? null,
    requestId: decision?.requestId ?? null,
    profile: config.routerProfile,
  };
  print(io, 'stdout', `${JSON.stringify(out, null, 2)}\n`);
  return 0;
}

async function cmdSetup(args, io) {
  const values = readHermesValues();
  if (!values.ok) {
    io.stderr.write(`setup: cannot read Hermes configuration — ${values.error}\n`);
    return 1;
  }
  const backupPath = setupBackupPath(io.env);
  const plan = buildSetupPlan({
    values,
    host: args.host ?? '127.0.0.1',
    port: args.port ?? 8791,
    apply: args.apply,
    backupPath,
  });
  print(io, 'stdout', `${plan.lines.join('\n')}\n`);

  try {
    writeBackup(plan.backupPath, plan.backup);
    print(io, 'stdout', `\nBackup written: ${plan.backupPath} (mode 600)\n`);
  } catch (error) {
    io.stderr.write(`setup: cannot write backup: ${error.message}\n`);
    return 1;
  }

  if (!args.apply) return 0;

  const results = applySetup(plan.commands);
  let failed = 0;
  for (const result of results) {
    print(io, 'stdout', `  ${result.ok ? 'ok  ' : 'FAIL'} hermes config set ${result.key} ${result.detail === 'ok' ? '' : `— ${result.detail}`}\n`);
    if (!result.ok) failed += 1;
  }
  if (failed > 0) {
    io.stderr.write(`setup: ${failed} command(s) failed — restore from ${plan.backupPath}\n`);
    return 1;
  }
  print(io, 'stdout', [
    '',
    'Applied. Next steps:',
    '  1. start the router:   node dist/server.js serve',
    '  2. restart the gateway from an EXTERNAL shell:  hermes gateway restart',
    `  3. to revert, run the commands above (backup: ${plan.backupPath})`,
    '',
  ].join('\n'));
  return 0;
}

async function cmdStatus(args, io) {
  const lines = [];
  let failed = 0;
  const report = (level, text) => {
    lines.push(`${level.padEnd(5)} ${text}`);
    if (level === 'FAIL') failed += 1;
  };

  const path = resolveConfigPath(args.config, io.env);
  let config;
  try {
    config = await loadConfig(args.config, io.env);
    report('PASS', `config valid: ${path}`);
  } catch (error) {
    report('FAIL', `config: ${error.message}`);
    print(io, 'stdout', `${lines.join('\n')}\n`);
    return 1;
  }

  const classifierKey = io.env[config.apiKeyEnv];
  if (classifierKey) report('PASS', `classifier key present: $${config.apiKeyEnv}`);
  else report('FAIL', `classifier key missing: $${config.apiKeyEnv} (routing falls back to defaultModel)`);

  const upstreamKey = io.env[config.upstream.apiKeyEnv];
  const loopbackUpstream = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1])/.test(config.upstream.url);
  if (upstreamKey) report('PASS', `upstream key present: $${config.upstream.apiKeyEnv}`);
  else if (loopbackUpstream) report('PASS', `upstream key not required (loopback upstream ${config.upstream.url})`);
  else report('FAIL', `upstream key missing: $${config.upstream.apiKeyEnv}`);

  try {
    const headers = { accept: 'application/json' };
    if (upstreamKey) headers.authorization = `Bearer ${upstreamKey}`;
    const res = await fetch(`${config.upstream.url.replace(/\/+$/, '')}/models`, {
      headers, signal: AbortSignal.timeout(10000), redirect: 'error',
    });
    if (res.ok) {
      const body = await res.json().catch(() => null);
      const count = Array.isArray(body?.data) ? body.data.length : null;
      report('PASS', `upstream reachable: ${config.upstream.url}/models → HTTP ${res.status}${count !== null ? `, ${count} models` : ''}`);
    } else {
      report('FAIL', `upstream /models → HTTP ${res.status}`);
    }
  } catch (error) {
    report('FAIL', `upstream unreachable: ${error?.message ?? error}`);
  }

  if (classifierKey) {
    try {
      const res = await fetch(`${String(config.routerUrl).replace(/\/+$/, '')}/v1/taxonomies`, {
        headers: { authorization: `Bearer ${classifierKey}` },
        signal: AbortSignal.timeout(config.timeoutMs),
        redirect: 'error',
      });
      if (res.ok) report('PASS', `classifier reachable: ${config.routerUrl}/v1/taxonomies → HTTP 200`);
      else report('WARN', `classifier /v1/taxonomies → HTTP ${res.status} (routing still degrades gracefully)`);
    } catch (error) {
      report('WARN', `classifier unreachable: ${error?.message ?? error} (routing degrades gracefully)`);
    }
  } else {
    report('WARN', 'classifier probe skipped (no key)');
  }

  try {
    const res = await fetch(`http://${config.host}:${config.port}/healthz`, { signal: AbortSignal.timeout(2000) });
    report(res.ok ? 'PASS' : 'WARN', `local proxy running on ${config.host}:${config.port}/healthz → HTTP ${res.status}`);
  } catch {
    report('INFO', `local proxy not running on ${config.host}:${config.port} (start: node dist/server.js serve)`);
  }

  lines.push('');
  lines.push(`keys shown as names only; values never printed (api_key: ${maskValue(io.env.OPENROUTER_API_KEY ? 'present' : '')})`);
  print(io, 'stdout', `${lines.join('\n')}\n`);
  return failed > 0 ? 1 : 0;
}

/** Dispatch. Returns the process exit code; never throws. */
async function runCli(argv, io = {}) {
  const context = {
    env: io.env ?? process.env,
    stdout: io.stdout ?? process.stdout,
    stderr: io.stderr ?? process.stderr,
    stdin: io.stdin ?? process.stdin,
  };
  let args;
  try {
    args = parseArgs(argv);
  } catch (error) {
    context.stderr.write(`error: ${error.message}\n\n${HELP}`);
    return 2;
  }
  if (args.version || args.command === 'version') {
    context.stdout.write(`cortiq-hermes-router ${VERSION}\n`);
    return 0;
  }
  if (args.help || args.command === 'help') {
    context.stdout.write(HELP);
    return 0;
  }
  try {
    switch (args.command) {
      case 'serve': return await cmdServe(args, context);
      case 'setup': return await cmdSetup(args, context);
      case 'decide': return await cmdDecide(args, context);
      case 'status': return await cmdStatus(args, context);
      default:
        context.stderr.write(`error: unknown command: ${args.command}\n`);
        return 2;
    }
  } catch (error) {
    context.stderr.write(`error: ${error.message}\n`);
    return 1;
  }
}

// ── entry ──────────────────────────────────────────────────────────────────
runCli(process.argv.slice(2)).then(
  (code) => { process.exitCode = code ?? 0; },
  (error) => { process.stderr.write(`fatal: ${error?.stack || error}\n`); process.exitCode = 1; },
);
