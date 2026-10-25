// Unit tests: pure selection logic (tiers / bands / taskRules / fallback).
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { createRoutingTable, parseTaskRule, resolveTier, selectModel } from '../lib/select.js';

const config = (overrides = {}) => ({
  globalTiers: {
    low: ['cheap/small'],
    medium: ['mid/one', 'mid/two'],
    high: ['strong/one'],
  },
  taskRules: {},
  defaultModel: 'fallback/model',
  complexityBands: null,
  ...overrides,
});

test('resolveTier: no bands passes the router tier through', () => {
  assert.equal(resolveTier(0.9, 'high', null), 'high');
  assert.equal(resolveTier(0.1, 'medium', undefined), 'medium');
});

test('resolveTier: bands are inclusive on the upper bound', () => {
  const bands = { low: 0.35, medium: 0.65 };
  assert.equal(resolveTier(0, 'high', bands), 'low');
  assert.equal(resolveTier(0.35, 'high', bands), 'low');
  assert.equal(resolveTier(0.350001, 'high', bands), 'medium');
  assert.equal(resolveTier(0.65, 'high', bands), 'medium');
  assert.equal(resolveTier(0.650001, 'high', bands), 'high');
  assert.equal(resolveTier(1, 'low', bands), 'high');
});

test('parseTaskRule: string, array, tier object, models alias', () => {
  assert.deepEqual(parseTaskRule('a/b'), { any: ['a/b'], per: {} });
  assert.deepEqual(parseTaskRule(['a/b', 'c/d']), { any: ['a/b', 'c/d'], per: {} });
  assert.deepEqual(
    parseTaskRule({ low: 'l/m', high: ['h/i'], any: ['any/one'] }),
    { any: ['any/one'], per: { low: ['l/m'], high: ['h/i'] } },
  );
  assert.deepEqual(parseTaskRule({ models: ['only/x'] }), { any: ['only/x'], per: {} });
  assert.deepEqual(parseTaskRule(null), { any: [], per: {} });
});

test('candidatesFor: task tier models lead, then global chain, then defaultModel', () => {
  const table = createRoutingTable(config({ taskRules: { coding: { medium: ['task/medium'] } } }));
  const { models, taskRuleMatched } = table.candidatesFor('coding', 'medium');
  assert.deepEqual(models, ['task/medium', 'mid/one', 'mid/two', 'fallback/model']);
  assert.equal(taskRuleMatched, true);
});

test('candidatesFor: high escalation puts global high before task any', () => {
  const table = createRoutingTable(config({
    taskRules: { coding: { high: ['task/high'], any: ['task/any'] } },
  }));
  const { models } = table.candidatesFor('coding', 'high');
  assert.deepEqual(models, ['task/high', 'strong/one', 'task/any', 'fallback/model']);
});

test('candidatesFor: low/medium puts task any before the global chain', () => {
  const table = createRoutingTable(config({ taskRules: { chitchat: { any: ['task/any'] } } }));
  const { models, taskRuleMatched } = table.candidatesFor('chitchat', 'low');
  assert.deepEqual(models, ['task/any', 'cheap/small', 'fallback/model']);
  assert.equal(taskRuleMatched, true);
});

test('candidatesFor: dedup, empty strings dropped, missing tier chain', () => {
  const table = createRoutingTable(config({
    globalTiers: { low: ['a/b', 'a/b', '', 'fallback/model'] },
  }));
  assert.deepEqual(table.candidatesFor('x', 'low').models, ['a/b', 'fallback/model']);
  assert.deepEqual(table.candidatesFor('x', 'high').models, ['fallback/model']);
  const empty = createRoutingTable({ globalTiers: {}, taskRules: {}, defaultModel: '' });
  assert.deepEqual(empty.candidatesFor('x', 'low').models, []);
  assert.equal(empty.candidatesFor('x', 'low').taskRuleMatched, false);
});

test('selectModel: classifier error falls back to defaultModel', () => {
  const selection = selectModel(config(), null);
  assert.equal(selection.model, 'fallback/model');
  assert.equal(selection.task, 'unclassified');
  assert.equal(selection.score, null);
  assert.equal(selection.tier, null);
  assert.equal(selection.fallback, true);
  assert.deepEqual(selection.candidates, ['fallback/model']);
});

test('selectModel: classifier error without defaultModel keeps the caller model', () => {
  const selection = selectModel(config({ defaultModel: '' }), null);
  assert.equal(selection.model, '');
  assert.equal(selection.fallback, true);
});

test('selectModel: bands override the router tier and task high rule wins', () => {
  const cfg = config({
    complexityBands: { low: 0.35, medium: 0.65 },
    taskRules: { coding: { high: ['coder/x'] } },
  });
  const selection = selectModel(cfg, {
    taskLabel: 'coding', complexityScore: 0.82, complexityTier: 'medium',
    confidence: 0.9, requestId: 'req_1', isNovel: false, source: 'router',
  });
  assert.equal(selection.tier, 'high');
  assert.equal(selection.model, 'coder/x');
  assert.equal(selection.task, 'coding');
  assert.ok(Math.abs(selection.score - 0.82) < 1e-9);
  assert.equal(selection.fallback, false);
  assert.equal(selection.taskRuleMatched, true);
});

test('selectModel: without bands the router tier stands', () => {
  const selection = selectModel(config(), {
    taskLabel: 'chitchat', complexityScore: 0.1, complexityTier: 'high',
    confidence: 0.9, requestId: 'req_2', isNovel: false, source: 'router',
  });
  assert.equal(selection.tier, 'high');
  assert.equal(selection.model, 'strong/one');
});

test('selectModel: unconfigured everything yields empty model (caller keeps request model)', () => {
  const selection = selectModel({ globalTiers: {}, taskRules: {}, defaultModel: '' }, null);
  assert.equal(selection.model, '');
  assert.deepEqual(selection.candidates, []);
});