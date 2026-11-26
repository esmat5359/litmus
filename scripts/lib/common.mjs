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
