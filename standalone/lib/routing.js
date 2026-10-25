/**
 * Pure helpers for request payloads and response headers.
 *
 * - `extractLastUserText` — pull the last user message for classification
 *   (string content or array-of-parts content; images/tool output ignored).
 * - `buildRoutingHeader` / `parseRoutingHeader` — the `X-Cortiq-Router`
 *   response header (`task=…;score=…;model=…`) and its parser for tests
 *   and verification tooling.
 * - `filterResponseHeaders` — copy upstream response headers minus the
 *   hop-by-hop set, so streaming (SSE) passes through byte-for-byte.
 */

/** Hop-by-hop headers that must not be forwarded (plus our transport overrides). */
export const HOP_BY_HOP = new Set([
  'connection', 'keep-alive', 'transfer-encoding', 'te', 'trailer', 'upgrade',
  'proxy-authenticate', 'proxy-authorization', 'host',
]);

/** Truncate to UTF-16 code units, the family's `maxChars` semantics. */
export function truncateText(text, maxChars) {
  return String(text ?? '').slice(0, maxChars);
}

function contentToText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .filter((part) => part && (part.type === 'text' || part.type === 'input_text')
        && typeof part.text === 'string')
      .map((part) => part.text)
      .join('\n');
  }
  return '';
}

/** Last `role: "user"` message text from an OpenAI `messages` array. */
export function extractLastUserText(messages) {
  if (!Array.isArray(messages)) return '';
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message && message.role === 'user') return contentToText(message.content);
  }
  return '';
}

/** Header-token sanitizer: printable ASCII minus `;`, spaces and friends. */
export function sanitizeToken(value) {
  return String(value ?? '').replace(/[^A-Za-z0-9_.:/@+,-]/g, '_');
}

/** Build the `X-Cortiq-Router` value: `task=…;score=…;model=…`. */
export function buildRoutingHeader({ task, score, model }) {
  const scorePart = Number.isFinite(score) ? Number(score).toFixed(4) : 'na';
  return `task=${sanitizeToken(task)};score=${scorePart};model=${sanitizeToken(model)}`;
}

/** Parse an `X-Cortiq-Router` value back into `{ task, score, model }`. */
export function parseRoutingHeader(value) {
  const out = { task: '', score: null, model: '' };
  if (typeof value !== 'string') return out;
  for (const part of value.split(';')) {
    const eq = part.indexOf('=');
    if (eq === -1) continue;
    const key = part.slice(0, eq).trim();
    const val = part.slice(eq + 1).trim();
    if (key === 'task') out.task = val;
    else if (key === 'model') out.model = val;
    else if (key === 'score') out.score = val === 'na' || val === '' ? null : Number(val);
  }
  if (out.score !== null && !Number.isFinite(out.score)) out.score = null;
  return out;
}

/** Copy response headers (Headers / array / plain object) minus hop-by-hop. */
export function filterResponseHeaders(headers) {
  const out = {};
  const add = (name, value) => {
    const key = String(name).toLowerCase();
    if (HOP_BY_HOP.has(key)) return;
    if (value === undefined || value === null) return;
    out[key] = Array.isArray(value) ? value.join(', ') : String(value);
  };
  if (!headers) return out;
  if (typeof headers.forEach === 'function' && !Array.isArray(headers)) {
    headers.forEach((value, name) => add(name, value));
  } else if (Array.isArray(headers)) {
    for (const [name, value] of headers) add(name, value);
  } else {
    for (const [name, value] of Object.entries(headers)) add(name, value);
  }
  return out;
}

/** `true` for `text/event-stream` (any parameters, any case). */
export function isEventStream(contentType) {
  return /^text\/event-stream\b/i.test(String(contentType ?? ''));
}