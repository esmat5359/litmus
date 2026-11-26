#!/usr/bin/env node
/**
 * LITMUS live end-to-end run for GenLayer Studionet (GenVM v0.2.16 contract).
 *
 *   npm ci
 *   node scripts/live_e2e.mjs                 # full run (about 45-70 minutes on the public RPC)
 *   node scripts/live_e2e.mjs --skip-slow     # skips the two-window timeout case
 *   node scripts/live_e2e.mjs --dry-run       # no network: runs against the in-process JS model
 *
 * Environment: LITMUS_KEYS=0xkey1,0xkey2,... (optional, 5 pre-funded keys; otherwise fresh accounts are
 * created and funded with the Studio RPC method sim_fundAccount).  GENLAYER_RPC overrides the RPC URL.
 *
 * Funding model: value only enters through deposit(); every other call is non-payable and spends ledger
 * balance, so a rejected call can never strand funds (the first live run proved a rejected call keeps its value).
 *
 * Truth model: every step is verified against CONTRACT STATE (read back through the views), never against
 * receipt field names alone.  Language-model steps are retried because validators may legitimately fail to
 * agree under the exact-equality law; a retry is reported as a warning, never hidden.
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { bytes, eventually, fmtGen, minBig, parseView, ruleDigest, sha256 } from './lib/common.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const argv = Object.fromEntries(process.argv.slice(2).map((a) => {
  const [k, v] = a.replace(/^--/, '').split('=');
  return [k, v ?? true];
}));
const DRY = Boolean(argv['dry-run']);
const SKIP_SLOW = Boolean(argv['skip-slow']);
const STRICT = Boolean(argv.strict);
const LLM_ATTEMPTS = Number(argv['llm-attempts'] ?? 3);
const FINALITY_MIN = Number(argv['finality-timeout-min'] ?? 35);
const OUT_DIR = resolve(ROOT, String(argv['out-dir'] ?? 'reports'));
const RPC = argv.rpc ?? process.env.GENLAYER_RPC ?? 'https://studio.genlayer.com/api';
const EXPLORER = 'https://explorer-studio.genlayer.com/tx/';

// ------------------------------------------------------------------------------------------ scenario data
const UNIT = 10n ** 15n;        // penalty unit
const DEPOSIT = 10n ** 14n;     // filing and appeal deposit
const STAKE = 9n * UNIT;        // three maximum penalties (2 aggravating factors -> 3 units each)
const BOND = 6n * UNIT;
const WINDOW_MIN = 3n;          // defense and appeal windows of the main rule
const RULE_TEXT = 'Members must not post a link to a product or service that costs money in the #general channel. '
  + 'Links to free resources are allowed. Posting any link in the #market channel is always allowed.';
const AGGS = ['The statement says the member had already been warned about this rule before.',
  'The statement says the member tagged or mentioned more than ten people in the post.'];
const PROBES = [
  { scenario: 'A member posted a link to buy a paid online course in the #general channel.', expected: 'BREACH', factors: [false, false] },
  { scenario: 'A member who had been warned about this rule yesterday posted a link to a paid e-book in the #general channel.', expected: 'BREACH', factors: [true, false] },
  { scenario: 'A member tagged twenty people and posted a link to a paid mobile app in the #general channel.', expected: 'BREACH', factors: [false, true] },
  { scenario: 'A member posted a link to a free public library website in the #general channel.', expected: 'COMPLIANT', factors: [false, false] },
  { scenario: 'A member posted a link to a paid online course in the #market channel.', expected: 'COMPLIANT', factors: [false, false] },
];
const CASES = {
  A: { statement: 'On Monday the member, who had been warned about this rule the week before, tagged thirty people and posted a link to a paid language course in the #general channel.',
       defense: 'I only wanted to help my friends learn.', designed: { verdict: 'BREACH', factors: [true, true] } },
  B: { statement: 'The member posted a link to a free public weather service in the #general channel.',
       defense: 'It is a free service and costs nothing.', designed: { verdict: 'COMPLIANT', factors: [false, false] } },
  C: { statement: 'The member posted a link to a paid photo-editing subscription in the #general channel.',
       defense: 'I did not know about the rule.', designed: { verdict: 'BREACH', factors: [false, false] },
       grounds: 'The statement does not show that the subscription was paid for or promoted by the member.' },
  T: { statement: 'The member posted a link to a paid fitness application in the #general channel.', defense: '',
       designed: { verdict: 'BREACH', factors: [false, false] } },
};

// ------------------------------------------------------------------------------------------ reporting
const report = { tool: 'litmus-live-e2e', mode: DRY ? 'dry-run' : 'studionet', rpc: DRY ? null : RPC, startedAt: new Date().toISOString(),
  contract: null, accounts: {}, checks: [], txs: [], notes: [], summary: null };
let seq = 0;
const COUNT = { PASS: 0, FAIL: 0, WARN: 0, INFO: 0, SKIP: 0 };
function check(status, name, detail = '') {
  seq += 1;
  const id = `C${String(seq).padStart(3, '0')}`;
  COUNT[status] += 1;
  report.checks.push({ id, status, name, detail: String(detail) });
  const tag = { PASS: 'PASS', FAIL: 'FAIL', WARN: 'WARN', INFO: 'INFO', SKIP: 'SKIP' }[status];
  console.log(`[${id}] ${tag.padEnd(4)} ${name}${detail ? ` -- ${detail}` : ''}`);
  return status === 'PASS' || status === 'INFO';
}
const assertEq = (name, actual, expected, hint = '') => {
  const ok = String(actual) === String(expected);
  check(ok ? 'PASS' : 'FAIL', name, ok ? String(actual) : `expected ${expected}, got ${actual}${hint ? ` (${hint})` : ''}`);
  return ok;
};
class Abort extends Error {}

// ------------------------------------------------------------------------------------------ chain + context
let chain; let address; let ACCTS = {};
const SENT = new Map();        // native value each wallet attached to calls (only deposit() takes any)
const START = new Map();       // wallet balances right after funding
const WITHDRAWN = new Map();   // ledger balances each wallet withdrew
async function fundLedger(who, amount, label) {
  const before = await claimable(who);
  const res = await tx(`deposit ${label}`, who, 'deposit', [], { value: amount, verify: async () => (await claimable(who)) >= before + amount });
  if (!res.ok) throw new Abort(`could not fund the ledger for ${label}`);
}
const nowSec = () => Math.floor(chain.now() / 1000);
const wallet = (who) => who.address.toLowerCase();
const view = async (fn, args = []) => parseView(await eventually(async () => chain.read(address, fn, args), { timeoutMs: 120_000, sleep: (ms) => chain.sleep(ms), now: () => chain.now() }) || (() => { throw new Error(`view ${fn} failed`); })());
const rule = async (id) => view('get_rule', [BigInt(id)]);
const kase = async (id) => view('get_case', [BigInt(id)]);
const member = async (rid, who) => view('get_member', [BigInt(rid), wallet(who)]);
const claimable = async (who) => BigInt(await view('get_claimable', [wallet(who)]));
const stats = async () => view('get_stats');
const until = (fn, timeoutMs = 90_000) => eventually(fn, { timeoutMs, intervalMs: 2_500, sleep: (ms) => chain.sleep(ms), now: () => chain.now() });

async function tx(label, who, fn, args, opts = {}) {
  const { value = 0n, kind = 'det', expectFail = false, verify = null, unchanged = null } = opts;
  const sentKey = who.address.toLowerCase();
  const attempts = opts.attempts ?? (kind === 'llm' ? LLM_ATTEMPTS : 2);
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    const started = Date.now();
    let receipt;
    try { receipt = await chain.send(who, address, fn, args, value); }
    catch (error) { receipt = { hash: null, status: 'SEND_ERROR', executionOk: false, error: String(error?.message ?? error).slice(0, 300), votes: [] }; }
    if (value > 0n && receipt.status !== 'SEND_ERROR') SENT.set(sentKey, (SENT.get(sentKey) ?? 0n) + value);
    report.txs.push({ label, fn, attempt, at: new Date().toISOString(), value: String(value), hash: receipt.hash, status: receipt.status, executionOk: receipt.executionOk, error: receipt.error, votes: receipt.votes,
      seconds: Math.round((Date.now() - started) / 1000), link: receipt.hash ? EXPLORER + receipt.hash : null });
    if (expectFail) {
      await chain.sleep(2_000);
      const stayed = unchanged ? await unchanged() : true;
      const detail = `exec=${receipt.executionOk} status=${receipt.status}${receipt.error ? ` error=${receipt.error}` : ''}`;
      if (stayed) { check('PASS', `${label} (rejected, state unchanged)`, detail); return { ok: true, receipt }; }
      check('FAIL', `${label} should have been rejected but state changed`, detail);
      return { ok: false, receipt };
    }
    const waitFailed = receipt.status === 'WAIT_ERROR';
    const ok = verify ? Boolean(await until(verify, waitFailed ? 300_000 : 75_000)) : receipt.executionOk !== false && !waitFailed;
    if (!ok && waitFailed) { check('FAIL', `${label}: sent (hash known) but the receipt could not be read`, `${receipt.hash} ${receipt.error ?? ''}`); return { ok: false, receipt }; }
    if (ok) {
      const note = attempt > 1 ? ` after ${attempt} attempts` : '';
      check(attempt > 1 ? 'WARN' : 'PASS', `${label}${note}`, `status=${receipt.status} exec=${receipt.executionOk} votes=${(receipt.votes ?? []).join('/') || 'n/a'}`);
      return { ok: true, receipt };
    }
    const transient = ['SEND_ERROR', 'UNDETERMINED', 'CANCELED', 'LEADER_TIMEOUT', 'VALIDATORS_TIMEOUT'].includes(receipt.status) || /LLM_ERROR/.test(receipt.error ?? '');
    if (attempt < attempts && (kind === 'llm' || transient)) {
      check('WARN', `${label}: attempt ${attempt}/${attempts} did not take effect`, `status=${receipt.status} exec=${receipt.executionOk} ${receipt.error ?? ''}`);
      await chain.sleep(5_000);
      continue;
    }
    check('FAIL', `${label} did not take effect`, `status=${receipt.status} exec=${receipt.executionOk} ${receipt.error ?? ''}`);
    return { ok: false, receipt };
  }
  return { ok: false, receipt: null };
}

async function audit(label, { exact = true } = {}) {
  const s = await stats();
  const n = Number(s.rules); const m = Number(s.cases);
  let held = 0n; const people = Object.values(ACCTS);
  const rules = []; for (let i = 1; i <= n; i += 10) rules.push(...await view('list_rules', [BigInt(i), 10n]));
  const cases = []; for (let i = 1; i <= m; i += 10) cases.push(...await view('list_cases', [BigInt(i), 10n]));
  for (const r of rules) {
    held += BigInt(r.bond_remaining);
    for (const p of people) { const mem = await member(r.id, p); if (mem) held += BigInt(mem.stake); }
  }
  for (const c of cases) if (c.status !== 'SETTLED') held += BigInt(c.deposit);
  let claim = 0n; for (const p of people) claim += await claimable(p);
  held += claim;
  const okClaim = BigInt(s.claimable_total) === claim;
  const okEscrow = BigInt(s.escrow_total) === held;
  let bal = null;
  try { bal = await chain.balance(address); } catch (error) { bal = null; }
  check(okClaim && okEscrow ? 'PASS' : 'FAIL', `audit ${label}: books balance`,
    `escrow=${s.escrow_total} recomputed=${held} claimable=${s.claimable_total}/${claim} contract_balance=${bal ?? 'unavailable'}`);
  if (bal === null) check('WARN', `audit ${label}: contract balance could not be read from the RPC`, 'native balance comparison skipped');
  else if (bal < BigInt(s.escrow_total)) check('FAIL', `audit ${label}: contract balance covers escrow`, `balance ${bal} < escrow ${s.escrow_total}`);
  else if (bal !== BigInt(s.escrow_total)) check(exact ? 'FAIL' : 'WARN', `audit ${label}: contract balance differs from escrow`, `balance ${bal}, escrow ${s.escrow_total}${exact ? ' (funds sit in the contract that no ledger entry owns)' : ' (transfers are emitted on finalization)'}`);
  else check('PASS', `audit ${label}: contract balance equals escrow exactly`, String(bal));
  return { stats: s, rules, cases };
}

/** Independent accounting oracle: what a settlement must pay, computed from the on-chain verdict only. */
function expectedSettlement(r, c, bondBefore) {
  const credits = new Map(); const add = (w, v) => credits.set(w, (credits.get(w) ?? 0n) + v);
  const unit = BigInt(r.penalty_unit); const dep = BigInt(c.deposit);
  const v = c.final_verdict; const sev = v === 'BREACH' ? 1n + BigInt(c.final_factors.filter(Boolean).length) : 0n;
  const penalty = unit * sev;
  if (v === 'BREACH') add(c.plaintiff, penalty + dep); else if (v === 'COMPLIANT') add(c.defendant, dep); else add(c.plaintiff, dep);
  let favorable = false; let slash = 0n;
  if (c.appeal_by) {
    const ad = BigInt(c.appeal_deposit);
    favorable = c.appeal_by === c.defendant ? v !== 'BREACH' : v === 'BREACH';
    if (favorable) { add(c.appeal_by, ad); slash = minBig(unit, bondBefore); add(c.appeal_by, slash); }
    else add(c.appeal_by === c.defendant ? c.plaintiff : c.defendant, ad);
  }
  return { credits, penalty, sev, favorable, slash };
}

// ------------------------------------------------------------------------------------------ scenario steps
async function setup() {
  chain = DRY ? new (await import('./lib/mock_chain.mjs')).MockChain({ flipAppeals: Boolean(argv['flip-appeals']) })
    : new (await import('./lib/real_chain.mjs')).RealChain({ rpc: RPC });
  if (!DRY) {
    try { await chain.balance('0x' + '0'.repeat(40)); } catch (error) {
      throw new Abort(`cannot reach the GenLayer RPC at ${RPC}: ${String(error?.shortMessage ?? error?.message ?? error).split('\n')[0]}. Check network access, or pass --rpc=<url>.`);
    }
    check('PASS', 'RPC reachable', RPC);
  }
  const names = ['author', 'plaintiff', 'defendant', 'observer', 'outsider'];
  const keys = (process.env.LITMUS_KEYS ?? '').split(',').map((k) => k.trim()).filter(Boolean);
  if (keys.length && keys.length < names.length) throw new Abort(`LITMUS_KEYS needs ${names.length} keys, got ${keys.length}`);
  names.forEach((name, i) => { ACCTS[name] = chain.newAccount(keys[i]); report.accounts[name] = ACCTS[name].address; });
  if (!keys.length) {
    for (const name of names) {
      try { await chain.fundAccount(ACCTS[name].address, 20n * 10n ** 18n); } catch (error) {
        report.notes.push(`sim_fundAccount failed for ${name}: ${error?.message ?? error}`);
      }
    }
  }
  for (const name of names) {
    const bal = await chain.balance(ACCTS[name].address);
    if (bal < 10n ** 17n) {
      throw new Abort(`${name} (${ACCTS[name].address}) holds only ${fmtGen(bal)}. Fund the five generated accounts, or export LITMUS_KEYS with five funded private keys, then rerun.`);
    }
  }
  for (const name of names) START.set(ACCTS[name].address.toLowerCase(), await chain.balance(ACCTS[name].address));
  check('PASS', 'accounts created and funded', names.map((n) => `${n}=${ACCTS[n].address}`).join(' '));
}

async function deploy() {
  const sourceBytes = readFileSync(resolve(ROOT, 'contracts/Litmus.py'));
  if (sourceBytes.includes(0x0d) || sourceBytes.subarray(0, 3).equals(Buffer.from([0xef, 0xbb, 0xbf]))) throw new Abort('contract source must use LF endings and no BOM');
  check('PASS', 'contract source is LF-only and BOM-free', `sha256=${sha256(sourceBytes)} bytes=${sourceBytes.length}`);
  if (argv.address && argv.address !== true) {
    address = String(argv.address);
    report.contract = address;
    const s = await stats();
    if (Number(s.rules) !== 0 || Number(s.cases) !== 0) throw new Abort('--address must point at a freshly deployed Litmus contract (rules and cases must be 0)');
    check('PASS', 'using existing fresh contract', address);
  } else {
    const result = await chain.deploy(sourceBytes.toString('utf8'), ACCTS.author);
    report.txs.push({ label: 'deploy', fn: 'deploy', attempt: 1, hash: result.receipt.hash, status: result.receipt.status, executionOk: result.receipt.executionOk, error: result.receipt.error, votes: result.receipt.votes, link: result.receipt.hash ? EXPLORER + result.receipt.hash : null });
    if (!result.address || !/^0x[0-9a-fA-F]{40}$/.test(result.address)) throw new Abort(`deployment did not return a contract address (status=${result.receipt.status} exec=${result.receipt.executionOk} ${result.receipt.error ?? ''})`);
    address = result.address;
    report.contract = address;
    check('PASS', 'deployed Litmus', `${address} status=${result.receipt.status}`);
  }
  const ready = await until(async () => (await stats()).rules === 0, 120_000);
  if (!ready) throw new Abort('deployed contract is not answering get_stats');
  check('PASS', 'fresh state reads back', JSON.stringify(await stats()));
  try {
    const code = await chain.contractCode(address);
    check(Buffer.compare(code, sourceBytes) === 0 ? 'PASS' : 'FAIL', 'deployed bytes equal the committed source', `${code.length} bytes`);
  } catch (error) { check('WARN', 'source parity could not be checked', String(error?.message ?? error).slice(0, 160)); }
  const limits = await view('get_limits');
  assertEq('limits expose min_probes', limits.min_probes, 4);
  assertEq('limits expose the seven-day void window', limits.void_after_seconds, 604800);
}

const forgeArgs = (o = {}) => {
  const p = { title: 'Link etiquette', text: RULE_TEXT, aggs: AGGS, probes: PROBES, unit: UNIT, deposit: DEPOSIT, stake: STAKE, bond: BOND, strikes: 3n, pass: 80n, defMin: WINDOW_MIN, appMin: WINDOW_MIN, ...o };
  return [p.title, p.text, JSON.stringify(p.aggs), JSON.stringify(p.probes), p.unit, p.deposit, p.stake, p.bond, p.strikes, p.pass, p.defMin, p.appMin];
};

async function forgeAndCertify() {
  const noRules = async () => (await stats()).rules === 0;
  await tx('forge_rule rejected while the author ledger is empty', ACCTS.author, 'forge_rule', forgeArgs(), { expectFail: true, unchanged: noRules });
  await fundLedger(ACCTS.author, BOND, 'author bond');
  const kept = async () => (await stats()).rules === 0 && (await claimable(ACCTS.author)) === BOND;
  await tx('forge_rule rejects three probes', ACCTS.author, 'forge_rule', forgeArgs({ probes: PROBES.slice(0, 3) }), { expectFail: true, unchanged: kept });
  await tx('forge_rule rejects a bond below three penalty units', ACCTS.author, 'forge_rule', forgeArgs({ bond: 3n * UNIT - 1n }), { expectFail: true, unchanged: kept });
  await tx('forge_rule rejects a stake that cannot cover the maximum penalty', ACCTS.author, 'forge_rule', forgeArgs({ stake: 3n * UNIT - 1n }), { expectFail: true, unchanged: kept });
  await audit('after rejected forges (nothing may be stranded)');

  const forged = await tx('forge_rule (main rule)', ACCTS.author, 'forge_rule', forgeArgs(), { verify: async () => (await stats()).rules === 1 });
  if (!forged.ok) throw new Abort('rule could not be forged');
  const r = await rule(1);
  assertEq('rule status is DRAFT', r.status, 'DRAFT');
  assertEq('rule author', r.author, wallet(ACCTS.author));
  assertEq('bond escrowed exactly', r.bond_remaining, BOND);
  assertEq('author ledger spent exactly the bond', await claimable(ACCTS.author), 0n);
  assertEq('probes start pending', r.probes.map((p) => p.result).join(','), 'PENDING,PENDING,PENDING,PENDING,PENDING');
  await tx('run_probe rejected for a non-author', ACCTS.outsider, 'run_probe', [1n, 0n], { expectFail: true, unchanged: async () => (await rule(1)).probes_run === 0 });
  await tx('certify_rule rejected before every probe ran', ACCTS.author, 'certify_rule', [1n], { expectFail: true, unchanged: async () => (await rule(1)).status === 'DRAFT' });
  await audit('after forge');

  for (let i = 0; i < PROBES.length; i += 1) {
    const res = await tx(`run_probe ${i} (model, exact consensus)`, ACCTS.author, 'run_probe', [1n, BigInt(i)], { kind: 'llm', verify: async () => (await rule(1)).probes[i].result !== 'PENDING' });
    if (!res.ok) throw new Abort(`probe ${i} never reached consensus`);
    const p = (await rule(1)).probes[i];
    const agree = p.result === 'PASS';
    check(agree ? 'PASS' : 'WARN', `probe ${i} result ${p.result}`, `expected ${p.expected} ${JSON.stringify(p.factors)}; validators agreed on ${p.got} ${JSON.stringify(p.got_factors)}`);
  }
  await tx('run_probe cannot run twice', ACCTS.author, 'run_probe', [1n, 0n], { expectFail: true, unchanged: async () => (await rule(1)).probes_run === PROBES.length });
  const pr = await rule(1);
  const needed = Number(pr.pass_percent) * PROBES.length;
  if (pr.probes_passed * 100 < needed) {
    check('FAIL', 'rule cannot be certified: clarity below threshold', `${pr.probes_passed}/${PROBES.length} probes passed, ${pr.pass_percent}% required. The validators' answers differ from the rule author's expectations; this is exactly what the gate is for.`);
    throw new Abort('rule failed its own litmus test');
  }
  await tx('certify_rule', ACCTS.author, 'certify_rule', [1n], { verify: async () => (await rule(1)).status === 'CERTIFIED' });
  const cr = await rule(1);
  assertEq('clarity is floor(passed*100/total)', cr.clarity, Math.floor((cr.probes_passed * 100) / PROBES.length));
  assertEq('stored digest equals the locally computed digest', cr.digest, ruleDigest(cr));
  await tx('revise_rule rejected on a certified rule', ACCTS.author, 'revise_rule', [1n, RULE_TEXT + ' Revised.', JSON.stringify(AGGS), JSON.stringify(PROBES)], { expectFail: true, unchanged: async () => (await rule(1)).version === 1 });
}

async function enlistMembers() {
  await fundLedger(ACCTS.outsider, STAKE - 1n, 'outsider (one wei short of a stake)');
  await tx('enlist rejected with a ledger one wei short of the stake', ACCTS.outsider, 'enlist', [1n], { expectFail: true, unchanged: async () => (await member(1, ACCTS.outsider)) === null && (await claimable(ACCTS.outsider)) === STAKE - 1n });
  for (const name of ['plaintiff', 'defendant', 'observer']) {
    await fundLedger(ACCTS[name], STAKE, `${name} stake`);
    await tx(`enlist ${name}`, ACCTS[name], 'enlist', [1n], { verify: async () => (await member(1, ACCTS[name]))?.status === 'ACTIVE' });
    assertEq(`${name} stake escrowed`, (await member(1, ACCTS[name])).stake, STAKE);
    assertEq(`${name} ledger spent exactly the stake`, await claimable(ACCTS[name]), 0n);
  }
  await fundLedger(ACCTS.plaintiff, STAKE, 'plaintiff (to prove a rejected enlist keeps the ledger)');
  await tx('enlist twice rejected, ledger untouched', ACCTS.plaintiff, 'enlist', [1n], { expectFail: true, unchanged: async () => (await rule(1)).members === 3 && (await claimable(ACCTS.plaintiff)) === STAKE });
  assertEq('rule counts three active members', (await rule(1)).members, 3);
  const casesNow = (await stats()).cases;
  await tx('file_case rejected while the plaintiff ledger is empty', ACCTS.observer, 'file_case', [1n, wallet(ACCTS.defendant), CASES.A.statement], { expectFail: true, unchanged: async () => (await stats()).cases === casesNow });
  await audit('after enlist');
}

async function fileCase(spec, plaintiff, defendant) {
  const before = (await stats()).cases;
  await fundLedger(plaintiff, DEPOSIT, `case ${spec.name} filing deposit`);
  const res = await tx(`file_case ${spec.name}`, plaintiff, 'file_case', [1n, wallet(defendant), spec.statement], { verify: async () => (await stats()).cases === before + 1 });
  if (!res.ok) throw new Abort(`case ${spec.name} could not be filed`);
  const id = (await stats()).cases;
  const c = await kase(id);
  assertEq(`case ${spec.name} is OPEN with the deposit escrowed`, `${c.status}/${c.deposit}`, `OPEN/${DEPOSIT}`);
  return id;
}

async function runCase(name, plaintiff, defendant, { appeal = false, negativeChecks = false } = {}) {
  const spec = { ...CASES[name], name };
  if (negativeChecks) {
    const casesBefore = (await stats()).cases;
    const same = async () => (await stats()).cases === casesBefore;
    await tx('file_case rejected against oneself', plaintiff, 'file_case', [1n, wallet(plaintiff), spec.statement], { expectFail: true, unchanged: same });
    await tx('file_case rejected for a non-member defendant', plaintiff, 'file_case', [1n, wallet(ACCTS.outsider), spec.statement], { expectFail: true, unchanged: same });
  }
  const cid = await fileCase(spec, plaintiff, defendant);
  const dBefore = await member(1, defendant);
  assertEq(`case ${name}: defendant stake reserved`, dBefore.locked, 3n * UNIT * BigInt(dBefore.open_cases));
  if (negativeChecks) {
    await tx('rule_case rejected while the defense window is open', ACCTS.observer, 'rule_case', [BigInt(cid)], { expectFail: true, unchanged: async () => (await kase(cid)).status === 'OPEN' });
    await tx('submit_defense rejected for the plaintiff', plaintiff, 'submit_defense', [BigInt(cid), 'The plaintiff may not defend.'], { expectFail: true, unchanged: async () => (await kase(cid)).defense === '' });
  }
  await tx(`submit_defense ${name}`, defendant, 'submit_defense', [BigInt(cid), spec.defense], { verify: async () => (await kase(cid)).defense === spec.defense });
  await tx(`rule_case ${name} (model, exact consensus)`, ACCTS.observer, 'rule_case', [BigInt(cid)], { kind: 'llm', verify: async () => (await kase(cid)).status === 'RULED' });
  const ruled = await kase(cid);
  const n = AGGS.length;
  const sevOk = ruled.severity === (ruled.verdict === 'BREACH' ? 1 + ruled.factors.filter(Boolean).length : 0);
  check(sevOk && ruled.factors.length === n ? 'PASS' : 'FAIL', `case ${name}: severity derives from the agreed flags`, `${ruled.verdict} ${JSON.stringify(ruled.factors)} severity ${ruled.severity}`);
  const same = ruled.verdict === spec.designed.verdict && JSON.stringify(ruled.factors) === JSON.stringify(spec.designed.factors);
  check(same ? 'PASS' : 'WARN', `case ${name}: validators' ruling vs the designed expectation`, `designed ${spec.designed.verdict} ${JSON.stringify(spec.designed.factors)}, agreed ${ruled.verdict} ${JSON.stringify(ruled.factors)}`);
  const appellantAcct = ruled.verdict === 'BREACH' ? defendant : plaintiff;
  const otherAcct = appellantAcct === defendant ? plaintiff : defendant;
  report.notes.push(`case ${name}: id ${cid}, ruled ${ruled.verdict} ${JSON.stringify(ruled.factors)}`);

  // Snapshot right after the ruling and BEFORE the appeal deposit is funded: funding (+D) and the debit (-D) cancel,
  // so every later ledger delta equals exactly the credits the settlement produced.
  const r0 = await rule(1);
  const pre = { rule: r0, pm: await member(1, plaintiff), dm: await member(1, defendant), cl: new Map() };
  for (const w of [plaintiff, defendant]) pre.cl.set(wallet(w), await claimable(w));

  if (appeal) {
    await tx(`appeal_case ${name} rejected for the party that won`, otherAcct, 'appeal_case', [BigInt(cid), 'The winning party must not appeal this ruling.'], { expectFail: true, unchanged: async () => (await kase(cid)).status === 'RULED' });
    await fundLedger(appellantAcct, DEPOSIT, `case ${name} appeal deposit`);
    await tx(`appeal_case ${name} (model, exact consensus)`, appellantAcct, 'appeal_case', [BigInt(cid), CASES[name].grounds], { kind: 'llm', verify: async () => (await kase(cid)).status === 'SETTLED' });
  } else {
    await tx(`settle_case ${name} rejected while the appeal window is open`, ACCTS.observer, 'settle_case', [BigInt(cid)], { expectFail: true, unchanged: async () => (await kase(cid)).status === 'RULED' });
    await tx(`waive_appeal ${name} rejected for the winning party`, otherAcct, 'waive_appeal', [BigInt(cid)], { expectFail: true, unchanged: async () => (await kase(cid)).waived === false });
    await tx(`waive_appeal ${name}`, appellantAcct, 'waive_appeal', [BigInt(cid)], { verify: async () => (await kase(cid)).waived === true });
    await tx(`settle_case ${name}`, ACCTS.observer, 'settle_case', [BigInt(cid)], { verify: async () => (await kase(cid)).status === 'SETTLED' });
  }
  await verifySettlement(name, cid, pre, plaintiff, defendant, appeal);
  await audit(`after case ${name}`);
  return cid;
}

async function verifySettlement(name, cid, pre, plaintiff, defendant, appealed) {
  const c = await kase(cid);
  const r1 = await rule(1);
  assertEq(`case ${name} settled`, c.status, 'SETTLED');
  const exp = expectedSettlement(pre.rule, c, BigInt(pre.rule.bond_remaining));
  assertEq(`case ${name}: final severity`, c.final_severity, Number(exp.sev));
  assertEq(`case ${name}: penalty equals unit x severity`, c.penalty, exp.penalty);
  if (appealed) assertEq(`case ${name}: reversed flag matches the appeal outcome`, c.reversed, exp.favorable);
  for (const w of [plaintiff, defendant]) {
    const delta = (await claimable(w)) - pre.cl.get(wallet(w));
    assertEq(`case ${name}: credit to ${w === plaintiff ? 'plaintiff' : 'defendant'}`, delta, exp.credits.get(wallet(w)) ?? 0n);
  }
  const dm = await member(1, defendant); const pm = await member(1, plaintiff);
  assertEq(`case ${name}: defendant stake after penalty`, dm.stake, BigInt(pre.dm.stake) - exp.penalty);
  assertEq(`case ${name}: defendant strikes`, dm.strikes, pre.dm.strikes + (c.final_verdict === 'BREACH' ? 1 : 0));
  assertEq(`case ${name}: reservation released`, dm.locked, BigInt(pre.dm.locked) - BigInt(c.reserve));
  assertEq(`case ${name}: open-case counters released`, `${pm.open_cases}/${dm.open_cases}`, `${pre.pm.open_cases - 1}/${pre.dm.open_cases - 1}`);
  const key = { BREACH: 'breaches', COMPLIANT: 'dismissed', INCONCLUSIVE: 'inconclusive' }[c.final_verdict];
  assertEq(`case ${name}: rule counter ${key}`, r1[key], pre.rule[key] + 1);
  assertEq(`case ${name}: bond after slash`, r1.bond_remaining, BigInt(pre.rule.bond_remaining) - exp.slash);
  assertEq(`case ${name}: reversals`, r1.reversals, pre.rule.reversals + (exp.favorable ? 1 : 0));
}

async function timeoutCaseStart() {
  const spec = { ...CASES.T, name: 'T' };
  const cid = await fileCase(spec, ACCTS.observer, ACCTS.defendant);
  const c = await kase(cid);
  report.notes.push(`timeout case T filed as case ${cid}; defense window closes at ${c.defense_due}`);
  return { cid, due: c.defense_due };
}

async function waitUntilSec(target, label) {
  while (nowSec() < target) {
    const left = target - nowSec();
    console.log(`      ... waiting ${left}s for ${label}`);
    await chain.sleep(Math.min(left, 30) * 1000);
  }
}

async function timeoutCaseFinish(t) {
  await waitUntilSec(t.due + 20, 'the defense window of case T to close');
  await tx('rule_case T without any defense (model, exact consensus)', ACCTS.plaintiff, 'rule_case', [BigInt(t.cid)], { kind: 'llm', verify: async () => (await kase(t.cid)).status === 'RULED' });
  const ruled = await kase(t.cid);
  check(ruled.defense === '' ? 'PASS' : 'FAIL', 'case T was ruled with no defense on file', `${ruled.verdict} ${JSON.stringify(ruled.factors)}`);
  await waitUntilSec(ruled.appeal_due + 20, 'the appeal window of case T to close');
  const r0 = await rule(1);
  const pre = { rule: r0, pm: await member(1, ACCTS.observer), dm: await member(1, ACCTS.defendant), cl: new Map() };
  for (const w of [ACCTS.observer, ACCTS.defendant]) pre.cl.set(wallet(w), await claimable(w));
  await tx('settle_case T after the appeal window (no waiver)', ACCTS.outsider, 'settle_case', [BigInt(t.cid)], { verify: async () => (await kase(t.cid)).status === 'SETTLED' });
  await verifySettlement('T', t.cid, pre, ACCTS.observer, ACCTS.defendant, false);
  const dm = await member(1, ACCTS.defendant);
  if (dm.status === 'EXPELLED') check('PASS', 'defendant reached the strike limit and was expelled', `strikes ${dm.strikes}`);
  else check('INFO', 'defendant membership status after case T', `${dm.status}, strikes ${dm.strikes}`);
  await audit('after case T');
}

async function exitFlow() {
  const people = [['plaintiff', ACCTS.plaintiff], ['defendant', ACCTS.defendant], ['observer', ACCTS.observer]];
  for (const [name, acct] of people) {
    const m = await member(1, acct); const before = await claimable(acct);
    await tx(`leave ${name}`, acct, 'leave', [1n], { verify: async () => (await member(1, acct)).stake === '0' });
    assertEq(`${name} stake released to the ledger`, (await claimable(acct)) - before, BigInt(m.stake));
    check('INFO', `${name} final membership status`, (await member(1, acct)).status);
  }
  await tx('reclaim_bond rejected before retirement', ACCTS.author, 'reclaim_bond', [1n], { expectFail: true, unchanged: async () => (await rule(1)).bond_remaining !== '0' });
  await tx('retire_rule', ACCTS.author, 'retire_rule', [1n], { verify: async () => (await rule(1)).status === 'RETIRED' });
  await fundLedger(ACCTS.outsider, 1n, 'outsider (completes a full stake)');
  await tx('enlist rejected on a retired rule, ledger untouched', ACCTS.outsider, 'enlist', [1n], { expectFail: true, unchanged: async () => (await member(1, ACCTS.outsider)) === null && (await claimable(ACCTS.outsider)) === STAKE });
  const remaining = BigInt((await rule(1)).bond_remaining); const beforeBond = await claimable(ACCTS.author);
  await tx('reclaim_bond', ACCTS.author, 'reclaim_bond', [1n], { verify: async () => (await rule(1)).bond_remaining === '0' });
  assertEq('bond remainder credited to the author', (await claimable(ACCTS.author)) - beforeBond, remaining);
  await audit('before withdrawals');
}

const WITHDRAWALS = [];

/** Withdraws the whole ledger balance and inspects the transfer message the contract emitted for it. */
async function withdrawNow(name, acct, label = 'withdraw') {
  const amount = await claimable(acct);
  if (amount === 0n) return null;
  const balBefore = await chain.balance(acct.address);
  const res = await tx(`${label} ${name} (${fmtGen(amount)})`, acct, 'withdraw', [], { verify: async () => (await claimable(acct)) === 0n });
  assertEq(`${name} ledger cleared`, await claimable(acct), 0n);
  if (!res.ok) return null;
  const w = { name, acct, amount, balBefore, hash: res.receipt.hash, at: chain.now(), child: null };
  WITHDRAWALS.push(w);
  WITHDRAWN.set(acct.address.toLowerCase(), (WITHDRAWN.get(acct.address.toLowerCase()) ?? 0n) + amount);
  await inspectTransfer(w);
  return w;
}

/** The contract's balance drops when it emits the transfer; the child transaction shows where it went. */
async function inspectTransfer(w) {
  if (!w.hash || typeof chain.triggered !== 'function') return;
  try {
    const kids = await eventually(async () => { const k = await chain.triggered(w.hash); return k.length ? k : false; }, { timeoutMs: 90_000, intervalMs: 5_000, sleep: (ms) => chain.sleep(ms), now: () => chain.now() });
    if (!kids) { check('WARN', `${w.name}: withdraw produced no child transaction`, 'no emitted transfer message is visible for this transaction yet'); return; }
    const same = (k) => String(k.to).toLowerCase() === wallet(w.acct) && (() => { try { return BigInt(k.value) === w.amount; } catch { return String(k.value) === String(w.amount); } })();
    const match = kids.find(same);
    w.child = match?.id ?? null;
    check(match ? 'PASS' : 'WARN', `${w.name}: withdraw emitted a transfer message to the wallet for the exact amount`,
      match ? `child ${match.id} status ${match.status} value ${match.value}` : `children seen: ${JSON.stringify(kids).slice(0, 300)}`);
  } catch (error) { check('WARN', `${w.name}: transfer message could not be inspected`, String(error?.shortMessage ?? error?.message ?? error).slice(0, 160)); }
}

async function withdrawAll() {
  for (const [name, acct] of Object.entries(ACCTS)) await withdrawNow(name, acct);
  await tx('withdraw rejected when nothing is owed', ACCTS.outsider, 'withdraw', [], { expectFail: true, unchanged: async () => true });
  const s = await stats();
  assertEq('escrow is zero after full wind-down', s.escrow_total, 0);
  assertEq('claimable is zero after full wind-down', s.claimable_total, 0);
  try {
    const bal = await chain.balance(address);
    assertEq('contract balance is zero right after the last withdrawal (transfers debit the contract at execution)', bal, 0n);
  } catch (error) { check('WARN', 'contract balance could not be read after wind-down', String(error?.message ?? error).slice(0, 120)); }
  await finalizeWithdrawals();
  await reconcileWallets();
}

/** Waits for the withdraw transactions to finalize (messages marked 'finalized' execute then) and reports it. */
async function finalizeWithdrawals() {
  if (!WITHDRAWALS.length) return;
  const deadline = Math.max(...WITHDRAWALS.map((w) => w.at)) + FINALITY_MIN * 60_000;
  for (const w of WITHDRAWALS) {
    const remaining = Math.max(30_000, deadline - chain.now());
    console.log(`      ... waiting up to ${Math.round(remaining / 60_000)} min for the withdraw of ${w.name} to finalize`);
    const done = await chain.waitFinalized(w.hash, remaining);
    const childStatus = w.child ? await chain.txStatus(w.child) : 'n/a';
    check(done.status === 'FINALIZED' ? 'PASS' : 'WARN', `${w.name}: withdraw transaction finalization`, `status ${done.status} after ${done.waitedSec}s of waiting; child transfer status ${childStatus}`);
  }
}

/** start - value sent + value withdrawn must equal the final wallet balance (any gap is reported, never hidden). */
async function reconcileWallets() {
  const rows = async () => {
    const out = [];
    for (const [name, acct] of Object.entries(ACCTS)) {
      const key = acct.address.toLowerCase();
      const start = START.get(key); const sent = SENT.get(key) ?? 0n; const got = WITHDRAWN.get(key) ?? 0n;
      const actual = await chain.balance(acct.address);
      out.push({ name, start, sent, got, actual, expected: start - sent + got });
    }
    return out;
  };
  await until(async () => (await rows()).every((r) => r.actual === r.expected), 120_000);
  for (const r of await rows()) {
    const detail = `start ${r.start} - sent ${r.sent} + withdrawn ${r.got} = expected ${r.expected}; actual ${r.actual}; gap ${r.actual - r.expected}`;
    if (r.actual === r.expected) check('PASS', `wallet reconciliation ${r.name}`, detail);
    else if (r.got > 0n && r.actual === r.start - r.sent) check('WARN', `wallet reconciliation ${r.name}: the withdrawal has not arrived`, `${detail}. The contract emitted the transfer (its balance dropped) but the wallet balance did not rise.`);
    else check('WARN', `wallet reconciliation ${r.name}`, `${detail} (network fees or pending transfers)`);
  }
}

// ------------------------------------------------------------------------------------------ main
async function writeReport(error) {
  report.finishedAt = new Date().toISOString();
  report.summary = { ...COUNT, aborted: error ? String(error.message ?? error) : null, hardFailures: COUNT.FAIL + (STRICT ? COUNT.WARN : 0) };
  mkdirSync(OUT_DIR, { recursive: true });
  const stamp = report.startedAt.replace(/[:.]/g, '-');
  const base = resolve(OUT_DIR, `live_e2e_${DRY ? 'dryrun' : 'studionet'}_${stamp}`);
  writeFileSync(`${base}.json`, JSON.stringify(report, (k, v) => (typeof v === 'bigint' ? v.toString() : v), 2));
  const md = [`# Litmus live end-to-end report (${report.mode})`, '',
    `- started: ${report.startedAt}`, `- finished: ${report.finishedAt}`, `- contract: ${report.contract ?? 'n/a'}`,
    `- result: **${report.summary.hardFailures === 0 && !error ? 'PASSED' : 'FAILED'}** (pass ${COUNT.PASS}, fail ${COUNT.FAIL}, warn ${COUNT.WARN}, info ${COUNT.INFO})`,
    error ? `- aborted: ${error.message ?? error}` : '', '', '## Checks', '', '| id | status | check | detail |', '|---|---|---|---|',
    ...report.checks.map((c) => `| ${c.id} | ${c.status} | ${c.name.replace(/\|/g, '/')} | ${c.detail.replace(/\|/g, '/').slice(0, 220)} |`), '',
    '## Transactions', '', '| step | attempt | status | exec ok | seconds | explorer |', '|---|---|---|---|---|---|',
    ...report.txs.map((t) => `| ${t.label} | ${t.attempt} | ${t.status} | ${t.executionOk} | ${t.seconds ?? ''} | ${t.link ? `[${t.hash.slice(0, 12)}...](${t.link})` : 'n/a'} |`), '',
    '## Notes', '', ...report.notes.map((n) => `- ${n}`)].join('\n');
  writeFileSync(`${base}.md`, md);
  if (DRY && argv.transcript) writeFileSync(resolve(String(argv.transcript)), JSON.stringify({ accounts: chain.accounts.map((a) => a.address), contract: address, transcript: chain.transcript, finalState: finalModelState() }, null, 2));
  console.log(`\nreport: ${base}.md\n        ${base}.json`);
  return base;
}

function finalModelState() {
  const S = chain.state; const ser = (o) => JSON.parse(JSON.stringify(o, (k, v) => (typeof v === 'bigint' ? v.toString() : v)));
  return ser({ rules: S.rules, cases: S.cases, members: S.members, claimable: S.claimable, stats: { rules: S.ruleCount, cases: S.caseCount, escrow_total: S.escrow, claimable_total: S.claimableTotal } });
}

async function main() {
  console.log(`LITMUS live end-to-end (${report.mode}) -- ${new Date().toISOString()}`);
  let failure = null; let timeoutCase = null;
  try {
    await setup();
    await deploy();
    await forgeAndCertify();
    await enlistMembers();
    if (!SKIP_SLOW) timeoutCase = await timeoutCaseStart(); else check('SKIP', 'timeout case T (two waiting windows)', '--skip-slow');
    await runCase('A', ACCTS.plaintiff, ACCTS.defendant, { negativeChecks: true });
    await withdrawNow('plaintiff', ACCTS.plaintiff, 'early withdraw');   // finality overlaps the remaining cases
    await runCase('B', ACCTS.plaintiff, ACCTS.defendant);
    await withdrawNow('defendant', ACCTS.defendant, 'early withdraw');
    await runCase('C', ACCTS.plaintiff, ACCTS.defendant, { appeal: true });
    if (timeoutCase) await timeoutCaseFinish(timeoutCase);
    await exitFlow();
    await withdrawAll();
  } catch (error) {
    failure = error;
    if (error instanceof Abort) check('FAIL', 'run aborted', error.message);
    else { check('FAIL', 'unexpected error', error?.stack?.split('\n').slice(0, 3).join(' | ') ?? String(error)); }
  }
  await writeReport(failure);
  const hard = COUNT.FAIL + (STRICT ? COUNT.WARN : 0);
  console.log(`\nRESULT: ${hard === 0 && !failure ? 'PASSED' : 'FAILED'}  pass=${COUNT.PASS} fail=${COUNT.FAIL} warn=${COUNT.WARN} info=${COUNT.INFO} skip=${COUNT.SKIP}`);
  process.exit(hard === 0 && !failure ? 0 : 1);
}

main();
