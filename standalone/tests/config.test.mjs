// Unit tests: config validation — the family defaults table is the source of truth.
import { strict as assert } from 'node:assert';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

import { loadConfig, parseConfig, resolveConfigPath } from '../lib/config.js';

test('defaults match the family table', () => {
  const config = parseConfig({});
  assert.equal(config.enabled, true);
  assert.equal(config.apiKeyEnv, 'CORTIQ_ROUTER_KEY');
  assert.equal(config.routerUrl, 'https://router.allaigate.com');
  assert.equal(config.taxonomyId, 'data-assistant');
  assert.equal(config.timeoutMs, 15000);
  assert.equal(config.maxChars, 12000);
  assert.equal(config.routerProfile, 'balanced');
  assert.equal(config.complexityBands, undefined);
  assert.deepEqual(config.globalTiers, {});
  assert.deepEqual(config.taskRules, {});
  assert.equal(config.defaultModel, '');
  assert.equal(config.echoRouting, false);
  // proxy-specific extensions
  assert.equal(config.host, '127.0.0.1');
  assert.equal(config.port, 8791);
  assert.deepEqual(config.upstream, {
    url: 'https://openrouter.ai/api/v1',
    apiKeyEnv: 'OPENROUTER_API_KEY',
    timeoutMs: 0,
  });
});

test('unknown setting is an error, not a warning', () => {
  assert.throws(() => parseConfig({ typoField: 1 }), /unknown setting/);
  assert.throws(() => parseConfig(['nope']), /root/);
});

test('numeric bounds: timeoutMs 1..120000, maxChars 1..1000000', () => {
  for (const bad of [0, 120001, -5, 1.5, '15000']) {
    assert.throws(() => parseConfig({ timeoutMs: bad }), /timeoutMs/);
  }
  for (const bad of [0, 1000001, true]) {
    assert.throws(() => parseConfig({ maxChars: bad }), /maxChars/);
  }
  assert.equal(parseConfig({ timeoutMs: 15000 }).timeoutMs, 15000);
});

test('routerProfile enum and boolean fields', () => {
  assert.equal(parseConfig({ routerProfile: 'cost-saver' }).routerProfile, 'cost-saver');
  assert.throws(() => parseConfig({ routerProfile: 'fastest' }), /routerProfile/);
  assert.throws(() => parseConfig({ enabled: 'yes' }), /enabled/);
  assert.throws(() => parseConfig({ echoRouting: 1 }), /echoRouting/);
});

test('complexityBands range: 0 <= low < medium <= 1', () => {
  assert.deepEqual(
    parseConfig({ complexityBands: { low: 0.35, medium: 0.65 } }).complexityBands,
    { low: 0.35, medium: 0.65 },
  );
  for (const bad of [
    { low: 0.5, medium: 0.5 }, { low: -0.1, medium: 0.5 },
    { low: 0.2, medium: 1.5 }, { low: 0.3 }, { extra: 1, low: 0.1, medium: 0.2 },
  ]) {
    assert.throws(() => parseConfig({ complexityBands: bad }), /complexityBands/);
  }
});

test('model ids need provider/model form', () => {
  assert.equal(
    parseConfig({ defaultModel: 'deepseek/deepseek-chat' }).defaultModel,
    'deepseek/deepseek-chat',
  );
  for (const bad of ['no-slash', 'a/b c', '/leading', 42]) {
    assert.throws(() => parseConfig({ defaultModel: bad }), /defaultModel/);
  }
});

test('routerUrl: https anywhere, http loopback only, no credentials', () => {
  assert.equal(
    parseConfig({ routerUrl: 'https://router.allaigate.com/' }).routerUrl,
    'https://router.allaigate.com',
  );
  assert.equal(
    parseConfig({ routerUrl: 'http://127.0.0.1:9099' }).routerUrl,
    'http://127.0.0.1:9099',
  );
  assert.throws(() => parseConfig({ routerUrl: 'http://example.com' }), /loopback/);
  assert.throws(() => parseConfig({ routerUrl: 'ftp://x' }), /routerUrl/);
  assert.throws(() => parseConfig({ routerUrl: 'https://u:p@host' }), /routerUrl/);
});

test('globalTiers arrays and taskRules shapes', () => {
  const config = parseConfig({
    globalTiers: { low: ['a/b'], high: ['x/y'] },
    taskRules: { coding: { low: 'l/m', high: ['h/i'], any: ['any/x'] }, chitchat: 'g/h' },
  });
  assert.deepEqual(config.taskRules.chitchat, 'g/h');
  assert.throws(() => parseConfig({ globalTiers: { ultra: ['a/b'] } }), /globalTiers/);
  assert.throws(() => parseConfig({ globalTiers: { low: 'not-array' } }), /globalTiers/);
  assert.throws(() => parseConfig({ taskRules: { coding: { wrong: ['a/b'] } } }), /taskRules/);
  assert.throws(() => parseConfig({ taskRules: { coding: { low: ['nope'] } } }), /taskRules/);
});

test('apiKeyEnv must be an env identifier', () => {
  assert.equal(parseConfig({ apiKeyEnv: 'MY_KEY' }).apiKeyEnv, 'MY_KEY');
  for (const bad of ['1BAD', 'with-dash', 'with space', '']) {
    assert.throws(() => parseConfig({ apiKeyEnv: bad }), /apiKeyEnv/);
  }
});

test('upstream section validation and defaults', () => {
  const config = parseConfig({ upstream: { url: 'http://127.0.0.1:9999/v1', timeoutMs: 5000 } });
  assert.deepEqual(config.upstream, {
    url: 'http://127.0.0.1:9999/v1',
    apiKeyEnv: 'OPENROUTER_API_KEY',
    timeoutMs: 5000,
  });
  assert.throws(() => parseConfig({ upstream: { url: 'http://example.com' } }), /loopback/);
  assert.throws(() => parseConfig({ upstream: { apiKey: 'hardcoded-secret' } }), /upstream/);
  assert.throws(() => parseConfig({ upstream: { timeoutMs: -1 } }), /upstream/);
  assert.throws(() => parseConfig({ port: 0 }), /port/);
});

test('loadConfig: missing default file means empty defaults', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'cortiq-test-'));
  try {
    const config = await loadConfig(undefined, { HOME: dir });
    assert.equal(config.defaultModel, '');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('loadConfig: explicitly selected missing file is an error', async () => {
  await assert.rejects(
    () => loadConfig('/nonexistent/cortiq.json', {}),
    /Cannot read/,
  );
  await assert.rejects(
    () => loadConfig(undefined, { CORTIQ_ROUTER_CONFIG: '/nonexistent/cortiq.json' }),
    /Cannot read/,
  );
});

test('loadConfig: invalid JSON is an error, valid file is validated', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'cortiq-test-'));
  const bad = join(dir, 'bad.json');
  const good = join(dir, 'good.json');
  writeFileSync(bad, '{not json');
  writeFileSync(good, JSON.stringify({ defaultModel: 'deepseek/deepseek-v4-flash' }));
  try {
    await assert.rejects(() => loadConfig(bad, {}), /valid JSON/);
    const config = await loadConfig(good, {});
    assert.equal(config.defaultModel, 'deepseek/deepseek-v4-flash');
    assert.equal(config.timeoutMs, 15000);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('resolveConfigPath precedence: --config > env > default', () => {
  assert.equal(resolveConfigPath('/x/y.json', {}), '/x/y.json');
  assert.equal(
    resolveConfigPath(undefined, { CORTIQ_ROUTER_CONFIG: '/e/z.json' }),
    '/e/z.json',
  );
  assert.ok(resolveConfigPath(undefined, { HOME: '/home/u' }).endsWith(
    '/.config/cortiq-hermes-router/config.json',
  ));
});


test('cortiq-router.example.json: owner tiers verbatim + tier selection', async () => {
  const { readFile } = await import('node:fs/promises');
  const raw = JSON.parse(await readFile(
    new URL('../../cortiq-router.example.json', import.meta.url), 'utf8'));
  const config = parseConfig(raw);
  assert.deepEqual(config.globalTiers, {
    low: ['xiaomi/mimo-v2.6-flash'],
    medium: ['anthropic/claude-haiku-5.5'],
    high: ['moonshotai/kimi-k3.1-pro'],
  });
  assert.equal(config.defaultModel, 'xiaomi/mimo-v2.6-flash');
  assert.equal(config.routerProfile, 'balanced');
  assert.equal(config.complexityBands, undefined); // default: follow the router tier
  assert.deepEqual(
    parseConfig({ complexityBands: { low: 0.35, medium: 0.65 } }).complexityBands,
    { low: 0.35, medium: 0.65 },
  );

  const { selectModel } = await import('../lib/select.js');
  const cases = [
    [0.12, 'low', 'xiaomi/mimo-v2.6-flash'],
    [0.5, 'medium', 'anthropic/claude-haiku-5.5'],
    [0.85, 'high', 'moonshotai/kimi-k3.1-pro'],
  ];
  for (const [score, tier, model] of cases) {
    const selection = selectModel(config, {
      taskLabel: 'chitchat', complexityScore: score, complexityTier: tier,
      confidence: 0.9, requestId: 'req_t', isNovel: false, source: 'router',
    });
    assert.equal(selection.tier, tier);
    assert.equal(selection.model, model);
  }
});
