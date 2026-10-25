// Unit tests: SSE frame parsing (streaming passes through unchanged; this
// helper is for tests/verification tooling that must inspect frames).
import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { parseSseFrames, sseJson } from '../lib/sse.js';

test('parses multiple frames separated by blank lines', () => {
  const frames = parseSseFrames('data: one\n\ndata: two\n\n');
  assert.equal(frames.length, 2);
  assert.equal(frames[0].data, 'one');
  assert.equal(frames[1].data, 'two');
  assert.equal(frames[0].event, 'message');
});

test('handles CRLF line endings', () => {
  const frames = parseSseFrames('event: delta\r\ndata: hello\r\n\r\n');
  assert.equal(frames.length, 1);
  assert.equal(frames[0].event, 'delta');
  assert.equal(frames[0].data, 'hello');
});

test('comment lines are collected with their data frame, never dispatched alone', () => {
  const attached = parseSseFrames(': keep-alive\ndata: x\n\n');
  assert.equal(attached.length, 1);
  assert.deepEqual(attached[0].comments, ['keep-alive']);
  // a comment-only block carries no data -> nothing to dispatch
  assert.equal(parseSseFrames(': ping\n\n').length, 0);
  assert.equal(parseSseFrames(': keep-alive\n\ndata: x\n\n').length, 1);
  assert.deepEqual(parseSseFrames(': keep-alive\n\ndata: x\n\n')[0].comments, []);
});

test('multi-line data accumulates with newlines', () => {
  const frames = parseSseFrames('data: line1\ndata: line2\n\n');
  assert.equal(frames[0].data, 'line1\nline2');
});

test('a trailing unterminated block is pending, not dispatched', () => {
  assert.equal(parseSseFrames('data: partial').length, 0);
  assert.equal(parseSseFrames('data: partial\n\n').length, 1);
  assert.equal(parseSseFrames('').length, 0);
});

test('id and retry fields parse; unknown fields ignored', () => {
  const frames = parseSseFrames('id: 7\nretry: 1000\nfoo: bar\ndata: x\n\n');
  assert.equal(frames[0].id, '7');
  assert.equal(frames[0].retry, 1000);
  assert.equal(frames[0].data, 'x');
});

test('value without a colon after the field name is an empty value', () => {
  const frames = parseSseFrames('data\ndata: y\n\n');
  assert.equal(frames[0].data, '\ny');
});

test('sseJson parses JSON frames and returns null otherwise', () => {
  const [frame] = parseSseFrames('data: {"ok":true}\n\n');
  assert.deepEqual(sseJson(frame), { ok: true });
  const [text] = parseSseFrames('data: not-json\n\n');
  assert.equal(sseJson(text), null);
  assert.equal(sseJson(null), null);
});

test('OpenAI-style chunk stream parses end to end', () => {
  const stream = [
    'data: {"id":"1","choices":[{"delta":{"content":"Hel"}}]}',
    '',
    'data: {"id":"1","choices":[{"delta":{"content":"lo"}}]}',
    '',
    'data: [DONE]',
    '',
  ].join('\n') + '\n';
  const frames = parseSseFrames(stream);
  assert.equal(frames.length, 3);
  const text = frames.slice(0, 2).map((f) => sseJson(f).choices[0].delta.content).join('');
  assert.equal(text, 'Hello');
  assert.equal(frames[2].data, '[DONE]');
});