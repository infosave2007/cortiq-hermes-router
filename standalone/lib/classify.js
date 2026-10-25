/**
 * AllaiGate classifier client (POST {routerUrl}/v1/route).
 *
 * The request payload matches the cortiq-*-router family exactly —
 * `input` is a STRUCT (`{ text }`), never a bare string — and the response
 * is validated before use. Every failure mode (missing key, timeout, HTTP
 * error, invalid payload, unreachable host) degrades to `null` plus one
 * stderr line; the proxy itself never fails because of the classifier.
 */

/** Build the `/v1/route` payload (pure — truncated to `maxChars`). */
export function buildRouteBody({ text, profile, taxonomyId, maxChars = 12000 }) {
  const body = {
    input: { text: String(text ?? '').slice(0, maxChars) },
    options: {
      policy_profile: profile ?? 'balanced',
      allow_oracle: true,
      return_explanation: false,
      top_k: 3,
    },
  };
  if (taxonomyId) body.taxonomy_id = taxonomyId;
  return body;
}

/**
 * Validate the subset of the classifier response that routing needs.
 * Returns `null` (never coerces) for anything malformed.
 */
export function parseRouteDecision(value) {
  if (!value || typeof value !== 'object') return null;
  const data = value;
  const d = data.decision;
  if (!d || typeof d !== 'object') return null;
  const c = d.complexity;
  if (!c || typeof c !== 'object') return null;
  const unit = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1;
  if (typeof d.task_label !== 'string' || !d.task_label.trim()
    || !unit(c.score)
    || typeof c.tier !== 'string' || !['low', 'medium', 'high'].includes(c.tier)
    || !unit(d.confidence)
    || typeof data.request_id !== 'string' || !data.request_id
    || typeof d.is_novel !== 'boolean'
    || typeof d.source !== 'string' || !d.source) {
    return null;
  }
  return {
    taskLabel: d.task_label,
    complexityScore: c.score,
    complexityTier: c.tier,
    confidence: d.confidence,
    requestId: data.request_id,
    isNovel: d.is_novel,
    source: d.source,
  };
}

/**
 * Classify text. Returns a validated decision or `null` — never throws.
 * `log` receives one-line stderr diagnostics (silent for the request itself).
 */
export async function classify(config, text, { env = process.env, log = () => {} } = {}) {
  const key = env[config.apiKeyEnv];
  if (!key) {
    log(`cortiq-hermes-router: ${config.apiKeyEnv} is not set — classification skipped, using fallback chain`);
    return null;
  }
  const base = String(config.routerUrl).replace(/\/+$/, '');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  try {
    const response = await fetch(`${base}/v1/route`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${key}`,
      },
      body: JSON.stringify(buildRouteBody({
        text,
        profile: config.routerProfile,
        taxonomyId: config.taxonomyId,
        maxChars: config.maxChars,
      })),
      signal: controller.signal,
      redirect: 'error',
    });
    if (!response.ok) {
      log(`cortiq-hermes-router: classifier HTTP ${response.status} — using fallback chain`);
      return null;
    }
    const decision = parseRouteDecision(await response.json());
    if (!decision) {
      log('cortiq-hermes-router: invalid classifier response — using fallback chain');
      return null;
    }
    return decision;
  } catch (error) {
    const reason = error?.name === 'AbortError' ? `timeout after ${config.timeoutMs}ms` : (error?.message ?? error);
    log(`cortiq-hermes-router: classifier unreachable (${reason}) — using fallback chain`);
    return null;
  } finally {
    clearTimeout(timer);
  }
}