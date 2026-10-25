/**
 * Minimal SSE (text/event-stream) frame parser.
 *
 * The proxy streams upstream bytes through unchanged — this helper exists
 * for tests, `live-verification` tooling and any harness that needs to
 * inspect frames without buffering the whole response. It follows the
 * HTML Living Standard event-stream parsing rules: frames are terminated
 * by a blank line, `data:` lines accumulate into one payload, comment
 * lines (`:`) are collected but never dispatched, and a trailing
 * unterminated block is pending, not dispatched.
 */

/**
 * Parse a complete (or chunked) event-stream string into frames.
 * @returns `[{ event, data, id, retry, comments }]`
 */
export function parseSseFrames(chunk) {
  const text = String(chunk ?? '').replace(/\r\n/g, '\n');
  const blocks = text.split('\n\n');
  const trailing = text.endsWith('\n\n') ? [] : [blocks.pop() ?? ''];
  const frames = [];

  for (const block of blocks) {
    if (!block) continue;
    const comments = [];
    const dataLines = [];
    let event = 'message';
    let id = null;
    let retry = null;
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) {
        comments.push(line.slice(1).trimStart());
        continue;
      }
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'data') dataLines.push(value);
      else if (field === 'event') event = value;
      else if (field === 'id') id = value;
      else if (field === 'retry' && /^\d+$/.test(value)) retry = Number(value);
      // Unknown fields are ignored, per spec.
    }
    if (dataLines.length > 0) {
      frames.push({ event, data: dataLines.join('\n'), id, retry, comments });
    }
  }

  void trailing; // unterminated trailing block is pending — not dispatched
  return frames;
}

/** Parse a frame's data as JSON; returns `null` when it is not JSON. */
export function sseJson(frame) {
  if (!frame || typeof frame.data !== 'string') return null;
  try {
    return JSON.parse(frame.data);
  } catch {
    return null;
  }
}