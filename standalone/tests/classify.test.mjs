// Unit tests: classifier payload (input is a STRUCT), response validation, degradation.
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { buildRouteBody, classify, parseRouteDecision } from '../lib/classify.js';

// Shape fixture matching a real AllaiGate /v1/route response (schema 1.1 subset).
const REAL_DECISION = {
  schema_version: '1.1',
  request_id: 'req_livefixture0001',
  decision: {
    task_id: 7,
    task_label: 'coding',
    taxonomy_id: 'data-assistant',
    confidence: 0.8741,
    confident: true,
    raw_confidence: 0.8741,
    margin: 0.41,
    is_novel: false,
    novelty_score: 0.02,
    complexity: { score: 0.8213, tier: 'high' },
    source: 'router',
    flags: [],
  },
  scores: [],
  usage: { billable_decisions: 1, oracle_calls: 0 },
  meta: { model_version: 'm1', taxonomy_version: 't1', latency_ms: 42,
    embedding_latency_ms: 6, served_by: 'test' },
};

test('buildRouteBody: input is a struct, never a bare string', () => {
  const body = buildRouteBody({ text: 'hello' });
  assert.equal(typeof body.input, 'object');
  assert.deepEqual(body.input, { text: 'hello' });
});

test('buildRouteBody: profile, taxonomy and maxChars truncation', () => {
  const body = buildRouteBody({
    text: 'x'.repeat(50), profile: 'cost-saver',
    taxonomyId: 'data-assistant', maxChars: 10,
  });
  assert.equal(body.options.policy_profile, 'cost-saver');
  assert.equal(body.taxonomy_id, 'data-assistant');
  assert.equal(body.input.text, 'x'.repeat(10));
  assert.equal(buildRouteBody({ text: 'p' }).options.policy_profile, 'balanced');
  assert.equal(buildRouteBody({ text: 'p' }).taxonomy_id, undefined);
  // maxChars counts UTF-16 code units: one astral emoji == 2 units
  assert.equal(buildRouteBody({ text: '🙂🙂🙂', maxChars: 2 }).input.text, '🙂');
  assert.equal(buildRouteBody({ text: '🙂🙂🙂', maxChars: 4 }).input.text, '🙂🙂');
});

test('parseRouteDecision: valid fixture passes with exact fields', () => {
  const decision = parseRouteDecision(REAL_DECISION);
  assert.equal(decision.taskLabel, 'coding');
  assert.ok(Math.abs(decision.complexityScore - 0.8213) < 1e-9);
  assert.equal(decision.complexityTier, 'high');
  assert.equal(decision.requestId, 'req_livefixture0001');
  assert.equal(decision.source, 'router');
  assert.equal(decision.isNovel, false);
});

test('parseRouteDecision: malformed payloads are rejected, never coerced', () => {
  const cases = [
    null, 'string', {}, { decision: null }, { decision: {} },
    { decision: { complexity: { score: 0.5, tier: 'ultra' }, task_label: 'x',
      confidence: 0.5, is_novel: false, source: 'router' } },
    { decision: { complexity: { score: 1.5, tier: 'high' }, task_label: 'x',
      confidence: 0.5, is_novel: false, source: 'router' } },
    { decision: { complexity: { score: 0.5, tier: 'high' }, task_label: 'x',
      confidence: 0.5, is_novel: 'no', source: 'router' } },
    { decision: { complexity: { score: 0.5, tier: 'low' }, task_label: '  ',
      confidence: 0.5, is_novel: false, source: 'router' } },
    { decision: { complexity: { score: 0.5, tier: 'low' }, task_label: 'x',
      confidence: 0.5, is_novel: false, source: 'router' } },
    { request_id: 'r1', decision: { complexity: { score: 0.5, tier: 'medium' },
      task_label: 'x', confidence: 0.5, is_novel: true, source: '' } },
  ];
  for (const value of cases) assert.equal(parseRouteDecision(value), null, JSON.stringify(value)?.slice(0, 80));
});

test('classify: missing key degrades with one stderr log line', async () => {
  const logs = [];
  const config = { apiKeyEnv: 'CORTIQ_ROUTER_KEY', routerUrl: 'https://router.allaigate.com',
    taxonomyId: 'data-assistant', timeoutMs: 15000, maxChars: 12000, routerProfile: 'balanced' };
  const decision = await classify(config, 'hi', { env: {}, log: (line) => logs.push(line) });
  assert.equal(decision, null);
  assert.match(logs[0], /CORTIQ_ROUTER_KEY is not set/);
});

test('classify: unreachable router degrades instead of throwing', async () => {
  const config = { apiKeyEnv: 'CORTIQ_ROUTER_KEY', routerUrl: 'http://127.0.0.1:9',
    taxonomyId: 'data-assistant', timeoutMs: 500, maxChars: 12000, routerProfile: 'balanced' };
  const logs = [];
  const decision = await classify(config, 'hi', {
    env: { CORTIQ_ROUTER_KEY: 'test' },
    log: (line) => logs.push(line),
  });
  assert.equal(decision, null);
  assert.match(logs[0], /unreachable|timeout/);
});