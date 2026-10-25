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
export function parseConfig(raw) {
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
export function resolveConfigPath(explicitPath, env = process.env) {
  if (explicitPath) return resolve(explicitPath);
  if (env.CORTIQ_ROUTER_CONFIG) return resolve(env.CORTIQ_ROUTER_CONFIG);
  return join(homedir(), '.config', 'cortiq-hermes-router', 'config.json');
}

/** State directory for backups and local files (~/.config/cortiq-hermes-router). */
export function routerStateDir(env = process.env) {
  const path = env.CORTIQ_ROUTER_CONFIG ? resolve(env.CORTIQ_ROUTER_CONFIG) : null;
  const dir = path ? path.replace(/[/\\][^/\\]+$/, '') : join(homedir(), '.config', 'cortiq-hermes-router');
  return dir || join(homedir(), '.config', 'cortiq-hermes-router');
}

/**
 * Load and validate the config.
 * An explicitly selected missing file (`--config` or `CORTIQ_ROUTER_CONFIG`)
 * is an error; a missing default file means empty routing defaults.
 */
export async function loadConfig(explicitPath, env = process.env) {
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