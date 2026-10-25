#!/usr/bin/env node
/**
 * Bundle lib/*.js into a single self-contained dist/server.js (no bundler
 * dependency): strip relative imports, keep node: builtins, unwrap `export`,
 * and refuse to build if two modules declare the same top-level binding.
 */
import { spawnSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');

/** Deterministic concatenation order (dependencies first). */
const ORDER = [
  'select.js',
  'config.js',
  'classify.js',
  'sse.js',
  'routing.js',
  'hermes.js',
  'server.js',
  'cli.js',
];

const RELATIVE_IMPORT = /^import\s[^;'"]*?from\s+['"]\.\/[^'"]+['"];?\s*$/;
const SIDE_EFFECT_IMPORT = /^import\s+['"]\.\/[^'"]+['"];?\s*$/;
const DECLARATION = /^(?:export\s+)?(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)/;
const EXPORT_LIST = /^export\s*\{[\s\S]*?\};?\s*$/;

function stripModule(source) {
  let out = source;
  // Relative imports (single- and multi-line) disappear; node: builtins stay.
  // `[^;]` keeps each match inside ONE statement — no cross-statement stripping.
  out = out.replace(/^import\s*\{[^;]*?\}\s*from\s*['"]\.\/[^'"]+['"];?[ \t]*$/gm, '');
  out = out.replace(/^import\s+[^'"{};]+?from\s*['"]\.\/[^'"]+['"];?[ \t]*$/gm, '');
  out = out.replace(/^import\s+['"]\.\/[^'"]+['"];?[ \t]*$/gm, '');
  const kept = [];
  for (const line of out.split('\n')) {
    if (EXPORT_LIST.test(line)) continue;
    kept.push(line.replace(/^export\s+(?=(?:async\s+)?(?:function|const|let|var|class)\b)/, ''));
  }
  return kept.join('\n');
}

function declaredNames(source) {
  const names = [];
  for (const line of source.split('\n')) {
    const decl = DECLARATION.exec(line);
    if (decl) { names.push(decl[1]); continue; }
    const imports = /^import\s+(?:([\w$]+)\s*(?:,\s*)?)?(?:\{([^}]*)\})?/.exec(line);
    if (line.startsWith('import ')) {
      if (imports?.[1]) names.push(imports[1]);
      if (imports?.[2]) {
        for (const part of imports[2].split(',')) {
          const name = part.trim().split(/\s+as\s+/).pop();
          if (name) names.push(name);
        }
      }
    }
  }
  return names;
}

const parts = [];
const seen = new Map();
for (const file of ORDER) {
  const source = readFileSync(join(root, 'lib', file), 'utf8');
  const stripped = stripModule(source);
  for (const name of declaredNames(stripped)) {
    if (seen.has(name)) {
      console.error(`build: duplicate top-level binding "${name}" in lib/${file} and lib/${seen.get(name)}`);
      process.exit(1);
    }
    seen.set(name, file);
  }
  parts.push(`// ── lib/${file} ${'─'.repeat(Math.max(4, 60 - file.length))}\n${stripped.trim()}\n`);
}

const pkg = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
const cliSource = readFileSync(join(root, 'lib', 'cli.js'), 'utf8');
const version = /export const VERSION = '([^']+)'/.exec(cliSource)?.[1];
if (version !== pkg.version) {
  console.error(`build: lib/cli.js VERSION (${version}) != package.json version (${pkg.version})`);
  process.exit(1);
}

const bundle = `#!/usr/bin/env node
/**
 * cortiq-hermes-router standalone proxy — GENERATED FILE, do not edit.
 * Rebuild with: npm run build   (scripts/bundless.mjs)
 * Source: lib/*.js · zero runtime dependencies (node:http + global fetch).
 */
${parts.join('\n')}
// ── entry ──────────────────────────────────────────────────────────────────
runCli(process.argv.slice(2)).then(
  (code) => { process.exitCode = code ?? 0; },
  (error) => { process.stderr.write(\`fatal: \${error?.stack || error}\\n\`); process.exitCode = 1; },
);
`;

mkdirSync(join(root, 'dist'), { recursive: true });
const outFile = join(root, 'dist', 'server.js');
writeFileSync(outFile, bundle);
const check = spawnSync(process.execPath, ['--check', outFile], { encoding: 'utf8' });
if (check.status !== 0) {
  console.error(`built dist/server.js FAILED node --check:\n${check.stderr}`);
  process.exit(1);
}
console.log(`built dist/server.js (${bundle.length} bytes, ${ORDER.length} modules, node --check ok)`);