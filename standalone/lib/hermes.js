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
import { routerStateDir } from './config.js';

/** Hermes settings this tool reads and (on `--apply`) rewrites. */
export const HERMES_KEYS = ['model.default', 'model.provider', 'model.base_url', 'model.api_key'];

/** Placeholder written to `model.api_key` — the proxy does not check it. */
export const LOCAL_KEY_PLACEHOLDER = 'cortiq-local';

/** Never echo a real key: show only its length. */
export function maskValue(value) {
  if (value === null || value === undefined || value === '') return '<unset>';
  return `<set, ${String(value).length} chars>`;
}

/** Run the `hermes` CLI. Returns `{ ok, out, status }` — never throws. */
export function runHermes(args) {
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
export function parseModelBlock(yamlText) {
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
export function readHermesValues(run = runHermes) {
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
export function buildSetupPlan({ values, host = '127.0.0.1', port = 8791, apply = false, backupPath, now = new Date() }) {
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
export function setupBackupPath(env = process.env) {
  return `${routerStateDir(env)}/hermes-backup.json`;
}

/**
 * Write the backup (rotating an existing one to `hermes-backup.previous.json`),
 * then apply the commands through `hermes config set`.
 */
export function writeBackup(backupPath, backup) {
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
export function applySetup(commands, run = runHermes) {
  return commands.map((cmd) => {
    const args = cmd.value === null ? ['config', 'unset', cmd.key] : ['config', 'set', cmd.key, cmd.value];
    const result = run(args);
    return { key: cmd.key, ok: result.ok, detail: result.ok ? 'ok' : (result.error || result.out || 'failed') };
  });
}