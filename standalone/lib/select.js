/**
 * Pure model-selection logic for cortiq-hermes-router.
 *
 * Port of the family routing table (cortiq-gateway/src/routing.rs →
 * cortiq-pi-router/src/routing-table.ts → this repository). No I/O here:
 * every step is a pure function of the config and the classifier decision,
 * so the whole priority chain is unit-testable.
 *
 * Priority for a classified request:
 *   1. task rule models FOR the resolved complexity tier;
 *   2. tier=high → the global `high` chain first, then the task's `any`
 *      models (escalation must not be undercut); tier=low/medium → the
 *      task's `any` models first, then the global tier chain;
 *   3. `defaultModel` as the last candidate.
 * Classifier failure → `defaultModel` directly (`fallback: true`).
 */

/** Complexity tiers in ascending order. */
const TIERS = ['low', 'medium', 'high'];

/** Normalize a task rule value to an array of model ids. */
function asList(value) {
  if (value === undefined || value === null) return [];
  return Array.isArray(value) ? value.map(String) : [String(value)];
}

/**
 * Parse a user-supplied task value into `{ any, per }`.
 * Accepts a plain string, an array, or a tier object whose `any` key may be
 * spelled `models`.
 */
export function parseTaskRule(value) {
  if (value === null || value === undefined) return { any: [], per: {} };
  if (typeof value === 'string') return { any: [value], per: {} };
  if (Array.isArray(value)) return { any: value.map(String), per: {} };
  if (typeof value === 'object') {
    const any = asList(value.any ?? value.models);
    const per = {};
    for (const tier of TIERS) {
      if (tier in value) per[tier] = asList(value[tier]);
    }
    return { any, per };
  }
  return { any: [String(value)], per: {} };
}

/**
 * Resolve the effective tier from a complexity score.
 * Without custom bands the router's tier stands as-is; bands are inclusive
 * on the upper bound (`score <= low → low`, `score <= medium → medium`).
 */
export function resolveTier(score, routerTier, bands) {
  if (!bands) return routerTier;
  if (score <= bands.low) return 'low';
  if (score <= bands.medium) return 'medium';
  return 'high';
}

/**
 * Build the routing table for a config shape.
 * Returns `{ candidatesFor(task, tier), tierOf(score, routerTier) }`.
 */
export function createRoutingTable(config) {
  const globalTiers = config.globalTiers ?? {};
  const defaultModel = config.defaultModel ?? '';
  const bands = config.complexityBands ?? null;
  const rules = Object.create(null);
  for (const [task, raw] of Object.entries(config.taskRules ?? {})) {
    const rule = parseTaskRule(raw);
    if (rule.any.length > 0 || Object.keys(rule.per).length > 0) rules[task] = rule;
  }

  const tierOf = (score, routerTier) => resolveTier(score, routerTier, bands);

  const candidatesFor = (task, tier) => {
    const out = [];
    const push = (id) => {
      if (id.length > 0 && !out.includes(id)) out.push(id);
    };
    const rule = Object.hasOwn(rules, task) ? rules[task] : undefined;
    const tierChain = globalTiers[tier] ?? (defaultModel ? [defaultModel] : []);

    // 1. Task rule models for this exact tier.
    if (rule?.per[tier]) for (const id of rule.per[tier]) push(id);

    const escalate = tier === 'high' && globalTiers.high !== undefined;
    if (escalate) {
      // 2a. high: global high chain first, then the task's any-tier models.
      for (const id of tierChain) push(id);
      if (rule) for (const id of rule.any) push(id);
    } else {
      // 2b. low/medium: task's any-tier models first, then the global chain.
      if (rule) for (const id of rule.any) push(id);
      for (const id of tierChain) push(id);
    }

    // 3. defaultModel as the last resort.
    push(defaultModel);

    const head = out[0];
    let taskRuleMatched = false;
    if (rule && head) {
      taskRuleMatched = rule.per[tier]?.[0] === head || rule.any[0] === head;
    }
    return { models: out, taskRuleMatched };
  };

  return { candidatesFor, tierOf };
}

/**
 * Choose a model for a classified (or failed) request.
 *
 * @param config  `{ globalTiers, taskRules, defaultModel, complexityBands }`
 * @param decision `null` when the classifier was unavailable/disabled,
 *                 otherwise `{ taskLabel, complexityScore, complexityTier }`
 * @returns `{ task, score, tier, model, candidates, fallback, taskRuleMatched }`
 *          — `model` is the first candidate; an empty string means nothing
 *          is configured and the caller should keep the request's model.
 */
export function selectModel(config, decision) {
  if (!decision) {
    const fb = config.defaultModel ? [config.defaultModel] : [];
    return {
      task: 'unclassified',
      score: null,
      tier: null,
      model: fb[0] ?? '',
      candidates: fb,
      fallback: true,
      taskRuleMatched: false,
    };
  }
  const table = createRoutingTable(config);
  const tier = table.tierOf(decision.complexityScore, decision.complexityTier);
  const { models, taskRuleMatched } = table.candidatesFor(decision.taskLabel, tier);
  const model = models[0] ?? '';
  return {
    task: decision.taskLabel,
    score: decision.complexityScore,
    tier,
    model,
    candidates: models,
    fallback: model === '',
    taskRuleMatched,
  };
}