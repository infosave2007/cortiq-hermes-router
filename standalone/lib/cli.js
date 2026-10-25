/**
 * CLI entry: `serve` (default) | `setup` | `decide` | `status`.
 *
 * Flags: `--port N`, `--host H`, `--config PATH`, `--apply` (setup only),
 * `--help`, `--version`. No dependencies: argv parsing, JSON output and
 * human-readable status lines are all plain stdio.
 */

import { loadConfig, resolveConfigPath } from './config.js';
import { classify } from './classify.js';
import { selectModel } from './select.js';
import { createProxyServer, shutdown } from './server.js';
import {
  applySetup,
  buildSetupPlan,
  HERMES_KEYS,
  maskValue,
  readHermesValues,
  setupBackupPath,
  writeBackup,
} from './hermes.js';

export const VERSION = '0.1.0';

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
export function parseArgs(argv) {
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
export async function runCli(argv, io = {}) {
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