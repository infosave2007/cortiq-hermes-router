// Unit tests: X-Cortiq-Router header build/parse, response-header filtering, message extraction.
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  buildRoutingHeader,
  extractLastUserText,
  filterResponseHeaders,
  HOP_BY_HOP,
  isEventStream,
  parseRoutingHeader,
  truncateText,
} from '../lib/routing.js';

test('buildRoutingHeader: task=...;score=...;model=...', () => {
  const header = buildRoutingHeader({ task: 'coding', score: 0.8213, model: 'deepseek/deepseek-v4-pro' });
  assert.equal(header, 'task=coding;score=0.8213;model=deepseek/deepseek-v4-pro');
});

test('buildRoutingHeader: sanitizes hostile values, score "na" when unknown', () => {
  const header = buildRoutingHeader({ task: 'weird task;injected', score: null, model: 'a b/c d' });
  assert.equal(header, 'task=weird_task_injected;score=na;model=a_b/c_d');
  assert.ok(!/[^\x20-\x7E]/.test(header));
});

test('parseRoutingHeader round-trips', () => {
  const parsed = parseRoutingHeader('task=chitchat;score=0.4533;model=x/y');
  assert.equal(parsed.task, 'chitchat');
  assert.ok(Math.abs(parsed.score - 0.4533) < 1e-9);
  assert.equal(parsed.model, 'x/y');
  const na = parseRoutingHeader('task=disabled;score=na;model=orig/model');
  assert.equal(na.score, null);
  assert.equal(na.model, 'orig/model');
  assert.deepEqual(parseRoutingHeader(undefined), { task: '', score: null, model: '' });
});

test('filterResponseHeaders strips hop-by-hop headers', () => {
  const filtered = filterResponseHeaders({
    'Content-Type': 'text/event-stream; charset=utf-8',
    Connection: 'keep-alive',
    'Transfer-Encoding': 'chunked',
    'Keep-Alive': 'timeout=5',
    'X-Request-Id': 'abc123',
    TE: 'trailers',
  });
  assert.deepEqual(filtered, {
    'content-type': 'text/event-stream; charset=utf-8',
    'x-request-id': 'abc123',
  });
  for (const name of ['connection', 'transfer-encoding']) assert.ok(HOP_BY_HOP.has(name));
});

test('filterResponseHeaders accepts a Headers instance and arrays', () => {
  const headers = new Headers({ 'Content-Type': 'application/json', Upgrade: 'h2c' });
  assert.deepEqual(filterResponseHeaders(headers), { 'content-type': 'application/json' });
  assert.deepEqual(
    filterResponseHeaders([['Content-Type', 'text/plain'], ['Trailer', 'x']]),
    { 'content-type': 'text/plain' },
  );
  assert.deepEqual(filterResponseHeaders(null), {});
});

test('isEventStream matches with parameters and any case', () => {
  assert.equal(isEventStream('text/event-stream'), true);
  assert.equal(isEventStream('text/event-stream; charset=utf-8'), true);
  assert.equal(isEventStream('TEXT/Event-Stream'), true);
  assert.equal(isEventStream('application/json'), false);
  assert.equal(isEventStream(undefined), false);
});

test('extractLastUserText: last user message wins', () => {
  const messages = [
    { role: 'system', content: 'sys' },
    { role: 'user', content: 'first question' },
    { role: 'assistant', content: 'answer' },
    { role: 'user', content: 'second question' },
  ];
  assert.equal(extractLastUserText(messages), 'second question');
});

test('extractLastUserText: array content joins text parts, ignores images', () => {
  const messages = [{
    role: 'user',
    content: [
      { type: 'text', text: 'part one' },
      { type: 'image_url', image_url: { url: 'http://x/y.png' } },
      { type: 'input_text', text: 'part two' },
    ],
  }];
  assert.equal(extractLastUserText(messages), 'part one\npart two');
});

test('extractLastUserText: no user / odd shapes return empty', () => {
  assert.equal(extractLastUserText([{ role: 'assistant', content: 'a' }]), '');
  assert.equal(extractLastUserText([]), '');
  assert.equal(extractLastUserText(undefined), '');
  assert.equal(extractLastUserText('not a list'), '');
  assert.equal(extractLastUserText([{ role: 'user', content: { x: 1 } }]), '');
});

test('truncateText: UTF-16 code units like the family maxChars', () => {
  assert.equal(truncateText('abcdef', 3), 'abc');
  assert.equal(truncateText('🙂🙂🙂', 2), '🙂'); // 2 UTF-16 units = one astral emoji
  assert.equal(truncateText(null, 5), '');
});