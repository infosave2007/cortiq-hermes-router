/**
 * The proxy itself: node:http + global fetch, zero runtime dependencies.
 *
 * - `POST /v1/chat/completions` — classify the last user message with
 *   AllaiGate, select a model per the family schema, rewrite `model`,
 *   forward to the upstream and stream the response back UNCHANGED, adding
 *   only the `X-Cortiq-Router: task=…;score=…;model=…` response header.
 * - any other `/v1/*` — transparent pass-through (GET /v1/models etc.).
 * - classifier failures degrade silently to the fallback chain (stderr log);
 *   upstream connection failures/timeouts answer `502` with a JSON error.
 */

import http from 'node:http';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { classify } from './classify.js';
import { selectModel } from './select.js';
import {
  buildRoutingHeader,
  extractLastUserText,
  filterResponseHeaders,
  truncateText,
} from './routing.js';

/** Maximum accepted request body (tool schemas + history live in here). */
const BODY_LIMIT = 32 * 1024 * 1024;

const logToStderr = (message) => { process.stderr.write(`${message}\n`); };

/** Read the whole request body, rejecting anything over `BODY_LIMIT`. */
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > BODY_LIMIT) {
        reject(Object.assign(new Error('request body too large'), { status: 413, code: 'payload_too_large' }));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', (error) => reject(error));
  });
}

function sendJson(res, status, payload, headers = {}) {
  const body = Buffer.from(`${JSON.stringify(payload)}\n`);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': String(body.length),
    ...headers,
  });
  res.end(body);
}

function errorPayload(message, code, type = 'cortiq_router_error') {
  return { error: { message, type, code } };
}

/** Create the HTTP server. `classifyFn`/`selectFn` are injectable for tests. */
export function createProxyServer({
  config,
  env = process.env,
  log = logToStderr,
  classifyFn = classify,
  selectFn = selectModel,
} = {}) {
  const upstreamBase = String(config.upstream.url).replace(/\/+$/, '');
  const upstreamKey = env[config.upstream.apiKeyEnv];
  let warnedNoKey = false;

  /** Forward a buffered request to the upstream and stream the response. */
  async function forward(req, res, { path, method, body, routingHeader }) {
    const headers = {
      'content-type': req.headers['content-type'] ?? 'application/json',
      accept: 'application/json, text/event-stream',
      'accept-encoding': 'identity',
    };
    if (upstreamKey) headers.authorization = `Bearer ${upstreamKey}`;
    else if (!warnedNoKey) {
      warnedNoKey = true;
      log(`cortiq-hermes-router: ${config.upstream.apiKeyEnv} is not set — upstream called without a key`);
    }

    // Upstream bases commonly carry the /v1 prefix themselves (…/api/v1);
    // drop the client's duplicated leading segment so …/api/v1/v1 never happens.
    let target = `${upstreamBase}${path}`;
    try {
      const basePath = new URL(upstreamBase).pathname.replace(/\/+$/, '');
      const baseSegment = basePath.split('/').filter(Boolean).pop();
      const requestUrl = new URL(path, 'http://cortiq.local');
      const segments = requestUrl.pathname.split('/').filter(Boolean);
      if (baseSegment && segments[0] === baseSegment) segments.shift();
      target = `${upstreamBase}/${segments.join('/')}${requestUrl.search}`;
    } catch {
      // keep the naive concatenation
    }

    const fromClient = new AbortController();
    res.on('close', () => { if (!res.writableEnded) fromClient.abort(); });
    const timeoutSignal = config.upstream.timeoutMs > 0
      ? AbortSignal.timeout(config.upstream.timeoutMs)
      : null;
    const signals = [fromClient.signal, ...(timeoutSignal ? [timeoutSignal] : [])];
    const signal = signals.length === 1 ? signals[0] : AbortSignal.any(signals);

    let upstream;
    try {
      upstream = await fetch(target, {
        method,
        headers,
        body: body && body.length ? body : undefined,
        signal,
        redirect: 'error',
      });
    } catch (error) {
      if (fromClient.signal.aborted) return; // client went away — nothing to answer
      const reason = error?.name === 'TimeoutError' || error?.name === 'AbortError'
        ? `timeout after ${config.upstream.timeoutMs}ms`
        : (error?.message ?? String(error));
      log(`cortiq-hermes-router: upstream unavailable (${reason})`);
      if (!res.headersSent) {
        sendJson(res, 502, errorPayload(`upstream unavailable: ${reason}`, 'upstream_unavailable'), {
          ...(routingHeader ? { 'x-cortiq-router': routingHeader } : {}),
        });
      }
      return;
    }

    const outHeaders = filterResponseHeaders(upstream.headers);
    if (routingHeader) outHeaders['x-cortiq-router'] = routingHeader;
    if (!outHeaders['x-cortiq-router']) outHeaders['x-cortiq-router'] = 'task=transparent;score=na;model=na';
    res.writeHead(upstream.status, outHeaders);

    if (!upstream.body || method === 'HEAD') {
      res.end();
      return;
    }
    if (upstream.status >= 500) log(`cortiq-hermes-router: upstream HTTP ${upstream.status} for ${path}`);
    try {
      await pipeline(Readable.fromWeb(upstream.body), res);
    } catch (error) {
      if (!fromClient.signal.aborted) log(`cortiq-hermes-router: stream aborted: ${error?.message ?? error}`);
      res.destroy();
    }
  }

  /** Smart path: classify → select → rewrite `model` → forward. */
  async function handleChat(req, res, path) {
    let payload;
    try {
      const raw = await readBody(req);
      payload = JSON.parse(raw.toString('utf8'));
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('body must be a JSON object');
      }
    } catch (error) {
      const status = error.status ?? 400;
      sendJson(res, status, errorPayload(
        status === 413 ? 'request body too large' : `invalid JSON body: ${error.message}`,
        status === 413 ? 'payload_too_large' : 'invalid_request',
      ));
      return;
    }

    let selection = null;
    let decision = null;
    const originalModel = typeof payload.model === 'string' ? payload.model : '';
    if (config.enabled) {
      const text = truncateText(extractLastUserText(payload.messages), config.maxChars);
      if (text.trim()) {
        // Nothing to classify → nothing to route on: keep the request's model.
        decision = await classifyFn(config, text, { env, log });
        selection = selectFn(config, decision);
      } else {
        selection = {
          task: 'norouting', score: null, tier: null,
          model: originalModel, candidates: [], fallback: false, taskRuleMatched: false,
        };
      }
    } else {
      selection = {
        task: 'disabled', score: null, tier: null,
        model: originalModel,
        candidates: [], fallback: false, taskRuleMatched: false,
      };
    }

    const chosen = selection.model || originalModel;
    if (!chosen) {
      sendJson(res, 500, errorPayload(
        'no model available: configure defaultModel or globalTiers (and defaultModel as fallback)',
        'no_model_configured',
      ), { 'x-cortiq-router': buildRoutingHeader({ task: selection.task, score: selection.score, model: 'none' }) });
      return;
    }

    payload.model = chosen;
    const routingHeader = buildRoutingHeader({ task: selection.task, score: selection.score, model: chosen });
    if (config.echoRouting) {
      log(`cortiq-hermes-router: task=${selection.task} score=${selection.score ?? 'na'} tier=${selection.tier ?? 'na'} model=${chosen}`);
    }

    await forward(req, res, {
      path,
      method: 'POST',
      body: Buffer.from(JSON.stringify(payload)),
      routingHeader,
    });
  }

  const server = http.createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://localhost');
    const path = `${url.pathname}${url.search}`;

    if (req.method === 'GET' && url.pathname === '/healthz') {
      sendJson(res, 200, {
        ok: true,
        name: 'cortiq-hermes-router',
        enabled: config.enabled,
        upstream: config.upstream.url,
        classifier: config.routerUrl,
      });
      return;
    }

    if (!url.pathname.startsWith('/v1/')) {
      sendJson(res, 404, errorPayload(`not found: ${url.pathname} (use /v1/...)`, 'not_found'));
      return;
    }

    const handle = async () => {
      if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
        await handleChat(req, res, path);
        return;
      }
      if (req.method === 'GET' || req.method === 'HEAD') {
        await forward(req, res, { path, method: req.method, body: undefined, routingHeader: null });
        return;
      }
      const body = await readBody(req);
      await forward(req, res, { path, method: req.method, body, routingHeader: null });
    };

    handle().catch((error) => {
      log(`cortiq-hermes-router: internal error: ${error?.stack ?? error}`);
      if (!res.headersSent) {
        sendJson(res, 500, errorPayload(`internal error: ${error?.message ?? error}`, 'internal_error'));
      } else {
        res.destroy();
      }
    });
  });

  return server;
}

/** Graceful shutdown: stop accepting, drop idle keep-alives, then force-close. */
export function shutdown(server, { graceMs = 5000 } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    server.close(finish);
    server.closeIdleConnections?.();
    const timer = setTimeout(() => {
      server.closeAllConnections?.();
      finish();
    }, graceMs);
    timer.unref?.();
    server.once('close', () => { clearTimeout(timer); finish(); });
  });
}