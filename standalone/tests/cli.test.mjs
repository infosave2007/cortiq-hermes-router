// Unit tests: CLI argv parsing and the Hermes setup plan (no I/O, no secrets echoed).
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { parseArgs, VERSION } from '../lib/cli.js';
import {
  buildSetupPlan,
  maskValue,
  parseModelBlock,
  setupBackupPath,
} from '../lib/hermes.js';

test('parseArgs: default command is serve with flags', () => {
  const args = parseArgs(['--port', '9000', '--host', '0.0.0.0', '--config', '/tmp/x.json']);
  assert.equal(args.command, 'serve');
  assert.equal(args.port, 9000);
  assert.equal(args.host, '0.0.0.0');
  assert.equal(args.config, '/tmp/x.json');
});

test('parseArgs: inline --flag=value form and validation', () => {
  const args = parseArgs(['--port=8791']);
  assert.equal(args.port, 8791);
  assert.throws(() => parseArgs(['--port', '0']), /invalid --port/);
  assert.throws(() => parseArgs(['--port', 'abc']), /invalid --port/);
  assert.throws(() => parseArgs(['--port']), /missing value/);
  assert.throws(() => parseArgs(['--nope']), /unknown option/);
  assert.throws(() => parseArgs(['frobnicate']), /unknown command/);
});

test('parseArgs: subcommands and decide prompt', () => {
  const decide = parseArgs(['decide', 'refactor', 'this', 'function']);
  assert.equal(decide.command, 'decide');
  assert.deepEqual(decide.positional, ['refactor', 'this', 'function']);
  const setup = parseArgs(['setup', '--apply', '--port', '8888']);
  assert.equal(setup.command, 'setup');
  assert.equal(setup.apply, true);
  assert.equal(setup.port, 8888);
  assert.equal(parseArgs(['status']).command, 'status');
  assert.equal(parseArgs([]).command, 'serve');
  assert.equal(parseArgs(['--help']).help, true);
  assert.equal(parseArgs(['--version']).version, true);
});

test('VERSION matches the package version', async () => {
  const { readFile } = await import('node:fs/promises');
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  assert.equal(VERSION, pkg.version);
});

test('maskValue never leaks the secret', () => {
  assert.equal(maskValue(''), '<unset>');
  assert.equal(maskValue(null), '<unset>');
  const masked = maskValue('top-secret-value-123');
  assert.ok(!masked.includes('top-secret-value-123'));
  assert.match(masked, /^<set, \d+ chars>$/);
});

test('parseModelBlock: extracts explicit model settings from config.yaml', () => {
  const yaml = [
    'display:',
    '  language: ru',
    'model:',
    '  # comment line',
    '  default: "xiaomi/mimo-v2.6-flash"',
    '  provider: "openrouter"   # pinned',
    '  base_url: "https://openrouter.ai/api/v1"',
    '  # api_key: <redacted>',
    'agents:',
    '  main: {}',
    '',
  ].join('\n');
  const { found, values } = parseModelBlock(yaml);
  assert.equal(found, true);
  assert.equal(values.default, 'xiaomi/mimo-v2.6-flash');
  assert.equal(values.provider, 'openrouter');
  assert.equal(values.base_url, 'https://openrouter.ai/api/v1');
  assert.equal(values.api_key, undefined);
  assert.equal(parseModelBlock('no model section\n').found, false);
});

test('buildSetupPlan: prints current values masked, exact set commands, revert, warning', () => {
  const values = {
    configPath: '/home/u/.hermes/config.yaml',
    raw: { default: 'xiaomi/mimo-v2.6-flash', provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1' },
    resolved: { 'model.default': 'xiaomi/mimo-v2.6-flash',
      'model.provider': 'openrouter',
      'model.base_url': 'https://openrouter.ai/api/v1',
      'model.api_key': 'really-secret-key-0001' },
  };
  const plan = buildSetupPlan({ values, port: 8791, backupPath: '/b/hermes-backup.json' });
  const text = plan.lines.join('\n');

  // current values are shown…
  assert.match(text, /model\.base_url\s+https:\/\/openrouter\.ai\/api\/v1/);
  assert.match(text, /model\.provider\s+openrouter/);
  // …keys masked, never echoed
  assert.ok(!text.includes('really-secret-key-0001'));
  assert.match(text, /model\.api_key\s+<set, \d+ chars>/);

  // exact commands
  assert.deepEqual(plan.commands, [
    { key: 'model.base_url', value: 'http://127.0.0.1:8791/v1' },
    { key: 'model.api_key', value: 'cortiq-local' },
  ]);
  assert.ok(text.includes('hermes config set model.base_url http://127.0.0.1:8791/v1'));
  assert.ok(text.includes('hermes config set model.api_key cortiq-local'));

  // revert restores originals; api_key was NOT explicit -> unset
  const revert = Object.fromEntries(plan.revert.map((c) => [c.key, c.value]));
  assert.equal(revert['model.base_url'], 'https://openrouter.ai/api/v1');
  assert.equal(revert['model.provider'], 'openrouter');
  assert.equal(revert['model.default'], 'xiaomi/mimo-v2.6-flash');
  assert.equal(revert['model.api_key'], null);
  assert.ok(text.includes('hermes config unset model.api_key'));

  // gateway warning: external restart, never from chat
  assert.match(text, /EXTERNAL shell/);
  assert.match(text, /hermes gateway restart/);
  assert.match(text, /blocked inside a chat session/);
  assert.match(text, /Dry run/);

  // backup payload: no key unless it was explicitly written
  assert.equal(plan.backup.values['model.api_key'].explicit, false);
  assert.equal(plan.backup.values['model.api_key'].value, null);
  assert.equal(plan.backup.hermesConfigPath, '/home/u/.hermes/config.yaml');
});

test('buildSetupPlan: explicit api_key is preserved in the backup for revert', () => {
  const values = {
    configPath: '/c.yaml',
    raw: { api_key: 'original-user-key' },
    resolved: { 'model.api_key': 'original-user-key' },
  };
  const plan = buildSetupPlan({ values, backupPath: '/b.json' });
  assert.equal(plan.backup.values['model.api_key'].explicit, true);
  assert.equal(plan.backup.values['model.api_key'].value, 'original-user-key');
  const revert = plan.revert.find((c) => c.key === 'model.api_key');
  assert.equal(revert.value, 'original-user-key');
});

test('setupBackupPath lives under the router state dir', () => {
  const path = setupBackupPath({ CORTIQ_ROUTER_CONFIG: '' , HOME: '/home/u' });
  assert.match(path, /\/\.config\/cortiq-hermes-router\/hermes-backup\.json$/);
});