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
