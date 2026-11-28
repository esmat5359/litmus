// Small shared helpers for the Litmus live end-to-end script.
import { createHash } from 'node:crypto';

export const sleepReal = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export const sha256 = (data) => createHash('sha256').update(data).digest('hex');

/** Canonical JSON with recursively sorted keys; byte-identical to Python json.dumps(sort_keys, compact). */
export function canon(value) {
  if (Array.isArray(value)) return '[' + value.map(canon).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map((k) => JSON.stringify(k) + ':' + canon(value[k])).join(',') + '}';
  }
  return JSON.stringify(value);
}

/** The digest the contract stores when a rule is certified. */
export function ruleDigest(rule) {
  return sha256(Buffer.from(canon({
    text: rule.text,
    aggravators: rule.aggravators,
    probes: rule.probes.map((p) => ({ scenario: p.scenario, expected: p.expected, factors: p.factors })),
  }), 'utf8'));
}

export const bytes = (text) => Buffer.byteLength(text, 'utf8');
export const wei = (n) => BigInt(n);
export const fmtGen = (v) => {
  const big = BigInt(v);
  const whole = big / 10n ** 18n;
  const frac = (big % 10n ** 18n).toString().padStart(18, '0').replace(/0+$/, '');
  return frac ? `${whole}.${frac} GEN` : `${whole} GEN`;
};
export const minBig = (a, b) => (a < b ? a : b);

/**
 * A wallet that withdrew `amount` ends up with `amount - fee` more than before the withdraw transaction was sent,
 * because the transaction fee leaves the wallet at the same time (about 1.3e14 wei on Studionet). Comparing with
 * `before + amount` therefore calls a correct payout "missing". The slack is far below every amount the live script
 * withdraws (smallest: 5.1e15) and far above a fee, so a payout that never arrived (net change <= 0) is never accepted.
 */
export const FEE_SLACK_WEI = 5n * 10n ** 14n;
export const payoutArrived = (before, now, amount) => BigInt(now) - BigInt(before) >= (BigInt(amount) > FEE_SLACK_WEI ? BigInt(amount) - FEE_SLACK_WEI : 1n);

/** Can `target` be written as a sum of some of `values` (each used at most once)? Used to name stray contract balance. */
export function isSubsetSum(values, target) {
  let sums = new Set([0n]);
  for (const v of values) {
    const next = new Set(sums);
    for (const s of sums) { const t = s + v; if (t <= target) next.add(t); }
    sums = next;
    if (sums.size > 200_000) return false;
  }
  return sums.has(target);
}

export function parseView(raw) {
  if (typeof raw !== 'string') return raw;
  const first = raw.trim()[0];
  return first === '{' || first === '[' || raw.trim() === 'null' ? JSON.parse(raw) : raw;
}

export async function eventually(check, { timeoutMs = 90_000, intervalMs = 2_500, sleep = sleepReal, now = () => Date.now() } = {}) {
  const deadline = now() + timeoutMs;
  let last;
  for (;;) {
    try {
      last = await check();
      if (last) return last;
    } catch (error) {
      last = undefined;
    }
    if (now() >= deadline) return false;
    await sleep(intervalMs);
  }
}

const errorText = (error) => [error?.details, error?.shortMessage, error?.message, String(error)].filter(Boolean).join(' ');

/** The RPC explicitly refused the request (rate limit): nothing was processed, so even a write may be repeated. */
export const isRateLimitError = (error) => /rate limit|too many requests|\b429\b/i.test(errorText(error));

/**
 * Errors that say nothing about the contract: rate limits, gateway HTML pages instead of JSON, dropped
 * connections. A long receipt-wait timeout is deliberately NOT transient (retrying would only multiply it).
 */
export const isTransientRpcError = (error) => isRateLimitError(error)
  || /<!DOCTYPE|is not valid JSON|HTTP request failed|Status: 50[234]|fetch failed|ECONNRESET|ETIMEDOUT|EAI_AGAIN|socket hang up/i.test(errorText(error));

/** Retry `fn` with exponential backoff while it fails with an error that `shouldRetry` calls transient. */
export async function retryTransient(fn, { attempts = 6, baseMs = 5_000, maxMs = 30_000, sleep = sleepReal, shouldRetry = isTransientRpcError, onRetry = null } = {}) {
  for (let attempt = 1; ; attempt += 1) {
    try { return await fn(); } catch (error) {
      if (attempt >= attempts || !shouldRetry(error)) throw error;
      const waitMs = Math.min(maxMs, baseMs * 2 ** (attempt - 1));
      if (onRetry) onRetry({ attempt, attempts, waitMs, error });
      await sleep(waitMs);
    }
  }
}
