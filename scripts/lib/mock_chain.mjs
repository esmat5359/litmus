// In-memory JavaScript model of contracts/Litmus.py used ONLY by `--dry-run`.
// It lets the live script be exercised end to end without a network, with a virtual clock.
// tests/direct/test_dryrun_replay.py replays the recorded transcript against the real Python contract
// and fails if this model ever drifts from it.
import { createHash, randomBytes } from 'node:crypto';
import { bytes, canon, ruleDigest, sha256 } from './common.mjs';

const LABELS = ['BREACH', 'COMPLIANT'];
const BASE_TS = 1793534400;

class Revert extends Error {}
const need = (cond, msg) => { if (!cond) throw new Revert(msg); };

function bounded(v, lo, hi, label) {
  need(typeof v === 'string', `invalid ${label}`);
  const t = v.trim();
  need(bytes(t) >= lo && bytes(t) <= hi, `invalid ${label}`);
  return t;
}

function parseAggravators(raw) {
  let data = [];
  if (typeof raw === 'string' && raw.trim()) { try { data = JSON.parse(raw); } catch { throw new Revert('aggravators json'); } }
  need(Array.isArray(data) && data.length <= 3, 'at most 3');
  const out = data.map((x) => bounded(x, 8, 160, 'factor'));
  need(new Set(out.map((x) => x.toLowerCase())).size === out.length, 'distinct');
  return out;
}

function parseProbes(raw, n) {
  let data;
  try { data = JSON.parse(raw); } catch { throw new Revert('probes json'); }
  need(Array.isArray(data) && data.length >= 4 && data.length <= 6, 'golden probes');
  const counts = { BREACH: 0, COMPLIANT: 0 };
  const seen = new Set();
  const probes = data.map((item) => {
    need(item && typeof item === 'object' && !Array.isArray(item) && Object.keys(item).every((k) => ['scenario', 'expected', 'factors'].includes(k)), 'probe shape');
    const scenario = bounded(item.scenario, 12, 500, 'scenario');
    need(typeof item.expected === 'string' && LABELS.includes(item.expected), 'expected');
    const flags = item.factors ?? Array(n).fill(false);
    need(Array.isArray(flags) && flags.length === n && flags.every((f) => typeof f === 'boolean'), 'probe factors');
    need(item.expected === 'BREACH' || !flags.some(Boolean), 'only BREACH');
    need(!seen.has(scenario.toLowerCase()), 'distinct scenarios');
    seen.add(scenario.toLowerCase());
    counts[item.expected] += 1;
    return { scenario, expected: item.expected, factors: flags, result: 'PENDING', got: '', got_factors: [], reason: '' };
  });
  need(counts.BREACH >= 2 && counts.COMPLIANT >= 2, 'label balance');
  return probes;
}

/** Deterministic stand-in for the validators' language model, tuned to the live script's scenarios. */
export function mockRuling(mode, rule, statement, defense, grounds, flip) {
  const text = `${statement} ${defense ?? ''}`.toLowerCase();
  const n = rule.aggravators.length;
  let verdict;
  if (/does not name|not say what/.test(text)) verdict = 'INCONCLUSIVE';
  else if (/#market|free /.test(text)) verdict = 'COMPLIANT';
  else if (/paid/.test(text)) verdict = 'BREACH';
  else verdict = 'COMPLIANT';
  if (mode === 'PROBE' && verdict === 'INCONCLUSIVE') verdict = 'COMPLIANT';
  if (mode === 'APPEAL' && flip && verdict === 'BREACH') verdict = 'COMPLIANT';
  const flags = Array(n).fill(false);
  if (verdict === 'BREACH') {
    if (n > 0) flags[0] = /warned/.test(text);
    if (n > 1) flags[1] = /(tagged|mentioned) (twenty|thirty|forty|fifty)/.test(text);
  }
  return { verdict, factors: flags, reason: `mock ${mode} ruling` };
}

const clone = (x) => structuredClone(x);

export class MockChain {
  /**
   * flipAppeals        - appeals reverse the first ruling.
   * retainRejectedValue - behave like Studionet: value attached to a REJECTED payable call stays in the contract.
   * refundRejectedLate - behave like the Studionet run of 2026-11-26: value attached to a REJECTED payable call sits in
   *                      the contract until that transaction finalizes and is then returned to the sender.
   * lateTransfers       - withdraw() transfers leave the contract AND reach the wallet only on finalization (the contract
   *                      balance does not fall at acceptance).
   * lostTransfers       - withdraw() transactions finalize but the emitted transfer is never applied (a payout defect).
   * flakyRpc           - inject the two transport failures seen on Studionet: a rate-limit refusal before submission,
   *                      and a gateway error for a rejected appeal that nevertheless reached the chain.
   */
  constructor({ flipAppeals = false, retainRejectedValue = false, refundRejectedLate = false, lateTransfers = false, lostTransfers = false, flakyRpc = false } = {}) {
    this.isDryRun = true;
    this.flip = flipAppeals;
    this.retain = retainRejectedValue;
    this.refundLate = refundRejectedLate;
    this.lateTransfers = lateTransfers;
    this.lostTransfers = lostTransfers;
    this.pendingRefunds = [];
    this.flaky = flakyRpc;
    this.sendCount = 0;
    this.ambiguousDone = false;
    this.events = [];
    this.clockMs = (BASE_TS + 1000) * 1000;
    this.balances = new Map();
    this.state = { rules: {}, cases: {}, members: {}, claimable: {}, ruleCount: 0, caseCount: 0, escrow: 0n, claimableTotal: 0n };
    this.contract = null;
    this.contractBalance = 0n;
    this.transcript = [];
    this.accounts = [];
    this.txCounter = 0;
    this.pendingTransfers = [];
  }
  now() { return this.clockMs; }
  async sleep(ms) { this.clockMs += ms; this.flushTransfers(); }
  nowSec() { return Math.floor(this.clockMs / 1000); }
  newAccount() {
    const address = '0x' + randomBytes(20).toString('hex');
    const acct = { address, key: null, account: { address }, index: this.accounts.length };
    this.accounts.push(acct);
    this.balances.set(address, 0n);
    return acct;
  }
  async chainId() { return 61999; }
  async fundAccount(address, amount) { this.balances.set(address, (this.balances.get(address) ?? 0n) + BigInt(amount)); }
  async balance(address) { return address === this.contract ? this.contractBalance : (this.balances.get(address) ?? 0n); }
  flushTransfers() {
    for (const t of this.pendingTransfers) {
      if (t.ready <= this.clockMs) {
        if (t.fromContract) this.contractBalance -= t.amount;
        this.balances.set(t.to, (this.balances.get(t.to) ?? 0n) + t.amount);
        t.done = true;
      }
    }
    this.pendingTransfers = this.pendingTransfers.filter((t) => !t.done);
    for (const r of this.pendingRefunds) {
      if (r.ready <= this.clockMs) { this.contractBalance -= r.amount; this.balances.set(r.to, (this.balances.get(r.to) ?? 0n) + r.amount); r.done = true; }
    }
    this.pendingRefunds = this.pendingRefunds.filter((r) => !r.done);
  }
  async deploy(code, acct) {
    this.clockMs += 15_000;
    this.contract = '0x' + createHash('sha256').update(code).digest('hex').slice(0, 40);
    this.code = Buffer.from(code, 'utf8');
    return { receipt: this.receipt(true, null), address: this.contract };
  }
  receipt(ok, error) {
    this.txCounter += 1;
    return { hash: '0x' + createHash('sha256').update(String(this.txCounter)).digest('hex'), status: 'ACCEPTED', executionOk: ok, error, votes: ['AGREE', 'AGREE', 'AGREE', 'AGREE', 'AGREE'], resultName: 'AGREE', contractAddress: null };
  }
  async waitFinalized(hash) { this.clockMs += 30_000; this.flushTransfers(); return { hash, status: 'FINALIZED', executionOk: true, error: null, votes: [] }; }
  async transactionStatus() { return 'FINALIZED'; }
  async contractCode() { return this.code; }
  async read(address, fn, args = []) { return this.view(fn, args); }

  async send(acct, address, fn, args, value = 0n) {
    this.sendCount += 1;
    if (this.flaky && this.sendCount % 25 === 0) {
      const error = new Error('An unknown RPC error occurred.\n\nDetails: Rate limit exceeded: 30 requests per minute');
      error.submitted = 'no';
      throw error;
    }
    const llmFns = new Set(['run_probe', 'rule_case', 'appeal_case']);
    this.clockMs += llmFns.has(fn) ? 45_000 : 18_000;
    this.flushTransfers();
    const snapshot = clone(this.state);
    const balances = new Map(this.balances);
    const contractBalance = this.contractBalance;
    const entry = { t: this.nowSec(), from: acct.address, fn, args: args.map((a) => (typeof a === 'bigint' ? { $big: a.toString() } : a)), value: String(value), llm: null, ok: true };
    try {
      const ctx = { sender: acct.address, value: BigInt(value), now: this.nowSec(), entry };
      if (ctx.value > 0n) {
        need((this.balances.get(acct.address) ?? 0n) >= ctx.value, 'insufficient funds');
      }
      this.exec(ctx, fn, args);
      if (ctx.value > 0n) { this.balances.set(acct.address, this.balances.get(acct.address) - ctx.value); this.contractBalance += ctx.value; }
      this.transcript.push(entry);
      return this.receipt(true, null);
    } catch (error) {
      if (!(error instanceof Revert)) throw error;
      this.state = snapshot;
      this.balances = balances;
      this.contractBalance = contractBalance;
      if (this.retain && BigInt(value) > 0n && (balances.get(acct.address) ?? 0n) >= BigInt(value)) {
        this.balances.set(acct.address, balances.get(acct.address) - BigInt(value));
        this.contractBalance = contractBalance + BigInt(value);
      } else if (this.refundLate && BigInt(value) > 0n && (balances.get(acct.address) ?? 0n) >= BigInt(value)) {
        this.balances.set(acct.address, balances.get(acct.address) - BigInt(value));
        this.contractBalance = contractBalance + BigInt(value);
        this.pendingRefunds.push({ to: acct.address, amount: BigInt(value), ready: this.clockMs + 100_000 });
      }
      entry.ok = false;
      entry.llm = entry.llm;
      this.transcript.push(entry);
      if (this.flaky && fn === 'appeal_case' && !this.ambiguousDone) {
        this.ambiguousDone = true;   // the call was executed and rejected, but the client only sees a gateway page
        const lost = new Error('An unknown RPC error occurred.\n\nDetails: Unexpected token \'<\', \"<!DOCTYPE \"... is not valid JSON');
        lost.submitted = 'maybe';
        throw lost;
      }
      return this.receipt(false, error.message);
    }
  }

  // ---------------------------------------------------------------- contract model
  get S() { return this.state; }
  rule(id) { const r = this.S.rules[id]; need(r, 'rule not found'); return r; }
  kase(id) { const c = this.S.cases[id]; need(c, 'case not found'); return c; }
  member(rid, w) { return this.S.members[`${rid}:${w}`] ?? null; }
  credit(w, amt) { amt = BigInt(amt); if (amt <= 0n) return; this.S.claimable[w] = (this.S.claimable[w] ?? 0n) + amt; this.S.claimableTotal += amt; }
  receive(ctx, { exact = null, minimum = null } = {}) {
    if (exact !== null) need(ctx.value === exact, 'value must equal');
    if (minimum !== null) need(ctx.value >= minimum, 'value below minimum');
    this.S.escrow += ctx.value;
    return ctx.value;
  }
  onlyAuthor(ctx, rule) { need(ctx.sender === rule.author, 'only the rule author'); }
  maxPenalty(rule) { return BigInt(rule.penalty_unit) * BigInt(1 + rule.aggravators.length); }
  judge(ctx, mode, rule, statement, defense, grounds) {
    const r = mockRuling(mode, rule, statement, defense, grounds, this.flip);
    ctx.entry.llm = { verdict: r.verdict, factors: r.factors, reason: r.reason };
    return r;
  }

  exec(ctx, fn, a) {
    const S = this.S;
    switch (fn) {
      case 'forge_rule': {
        const [title, text, aggJson, probesJson, unit, deposit, stake, strikes, pass, defMin, appMin] = a;
        const t = bounded(title, 3, 80, 'title');
        const body = bounded(text, 20, 1200, 'rule text');
        const aggs = parseAggravators(aggJson);
        const probes = parseProbes(probesJson, aggs.length);
        need(unit >= 1n && deposit >= 1n, 'positive');
        need(stake >= unit * BigInt(1 + aggs.length), 'stake covers maximum penalty');
        need(strikes >= 1n && strikes <= 5n, 'strike limit');
        need(pass >= 75n && pass <= 100n, 'pass percent');
        need(defMin >= 2n && defMin <= 10080n, 'defense window');
        need(appMin >= 2n && appMin <= 10080n, 'appeal window');
        const bond = this.receive(ctx, { minimum: unit * 3n });
        const id = S.ruleCount + 1; S.ruleCount = id;
        S.rules[id] = {
          id, author: ctx.sender, title: t, text: body, version: 1, status: 'DRAFT', aggravators: aggs, probes,
          penalty_unit: unit.toString(), filing_deposit: deposit.toString(), enlist_stake: stake.toString(),
          strike_limit: Number(strikes), pass_percent: Number(pass), defense_minutes: Number(defMin), appeal_minutes: Number(appMin),
          bond: bond.toString(), bond_remaining: bond.toString(), probes_run: 0, probes_passed: 0, clarity: 0, digest: '',
          members: 0, open_cases: 0, cases: 0, breaches: 0, dismissed: 0, inconclusive: 0, voided: 0, reversals: 0,
          created_at: ctx.now, certified_at: 0, retired_at: 0,
        };
        return;
      }
      case 'run_probe': {
        const [rid, index] = a; const r = this.rule(Number(rid)); this.onlyAuthor(ctx, r);
        need(r.status === 'DRAFT', 'only DRAFT');
        const i = Number(index); need(i >= 0 && i < r.probes.length, 'index');
        const p = r.probes[i]; need(p.result === 'PENDING', 'already run');
        const g = this.judge(ctx, 'PROBE', r, p.scenario, '', '');
        p.got = g.verdict; p.got_factors = g.factors; p.reason = g.reason;
        const passed = g.verdict === p.expected && canon(g.factors) === canon(p.factors);
        p.result = passed ? 'PASS' : 'FAIL'; r.probes_run += 1; if (passed) r.probes_passed += 1;
        return;
      }
      case 'certify_rule': {
        const r = this.rule(Number(a[0])); this.onlyAuthor(ctx, r); need(r.status === 'DRAFT', 'only DRAFT');
        const total = r.probes.length; need(r.probes_run === total, 'run every probe first');
        need(r.probes_passed * 100 >= r.pass_percent * total, 'clarity below threshold');
        r.status = 'CERTIFIED'; r.clarity = Math.floor((r.probes_passed * 100) / total); r.digest = ruleDigest(r); r.certified_at = ctx.now;
        return;
      }
      case 'revise_rule': { const r = this.rule(Number(a[0])); this.onlyAuthor(ctx, r); need(r.status === 'DRAFT', 'only a DRAFT rule can be revised'); throw new Error('mock chain: revising a DRAFT rule is not modelled'); }
      case 'retire_rule': { const r = this.rule(Number(a[0])); this.onlyAuthor(ctx, r); need(r.status !== 'RETIRED', 'already'); r.status = 'RETIRED'; r.retired_at = ctx.now; return; }
      case 'reclaim_bond': {
        const r = this.rule(Number(a[0])); this.onlyAuthor(ctx, r); need(r.status === 'RETIRED', 'retire first'); need(r.open_cases === 0, 'open');
        const rem = BigInt(r.bond_remaining); need(rem > 0n, 'no bond'); r.bond_remaining = '0'; this.credit(r.author, rem); return;
      }
      case 'enlist': {
        const rid = Number(a[0]); const r = this.rule(rid); need(r.status === 'CERTIFIED', 'not certified');
        const stake = BigInt(r.enlist_stake); let m = this.member(rid, ctx.sender);
        need(m === null || m.status === 'LEFT', 'already enlisted or expelled');
        this.receive(ctx, { exact: stake });
        if (m === null) m = { rule_id: rid, wallet: ctx.sender, stake: '0', locked: '0', strikes: 0, status: 'ACTIVE', open_cases: 0, joined_at: ctx.now, filed: 0, defended: 0, upheld_against: 0, dismissed_filings: 0 };
        m.stake = stake.toString(); m.status = 'ACTIVE'; r.members += 1; S.members[`${rid}:${ctx.sender}`] = m; return;
      }
      case 'leave': {
        const rid = Number(a[0]); const r = this.rule(rid); const m = this.member(rid, ctx.sender);
        need(m !== null && ['ACTIVE', 'EXPELLED'].includes(m.status), 'not a member'); need(m.open_cases === 0, 'open cases pending');
        const stake = BigInt(m.stake); need(stake > 0n || m.status === 'ACTIVE', 'nothing to release');
        m.stake = '0'; if (m.status === 'ACTIVE') { m.status = 'LEFT'; r.members -= 1; } this.credit(ctx.sender, stake); return;
      }
      case 'file_case': {
        const rid = Number(a[0]); const r = this.rule(rid); need(r.status === 'CERTIFIED', 'not certified');
        const plaintiff = ctx.sender; const defendant = String(a[1]).toLowerCase();
        need(/^0x[0-9a-f]{40}$/.test(defendant), 'invalid wallet'); need(!/^0x0{40}$/.test(defendant), 'zero wallet');
        need(plaintiff !== defendant, 'yourself'); const statement = bounded(a[2], 12, 700, 'statement');
        const pm = this.member(rid, plaintiff); const dm = this.member(rid, defendant);
        need(pm && pm.status === 'ACTIVE', 'plaintiff not active'); need(dm && dm.status === 'ACTIVE', 'defendant not active');
        need(pm.open_cases < 3, 'plaintiff too many'); need(dm.open_cases < 3, 'defendant too many');
        const reserve = this.maxPenalty(r); need(BigInt(dm.stake) - BigInt(dm.locked) >= reserve, 'stake cannot cover');
        const deposit = BigInt(r.filing_deposit); this.receive(ctx, { exact: deposit });
        const id = S.caseCount + 1; S.caseCount = id;
        S.cases[id] = {
          id, rule_id: rid, plaintiff, defendant, statement, defense: '', defense_at: 0, opened_at: ctx.now, defense_due: ctx.now + r.defense_minutes * 60,
          status: 'OPEN', deposit: deposit.toString(), reserve: reserve.toString(), verdict: '', factors: [], severity: 0, reason: '', ruled_at: 0, appeal_due: 0, waived: false,
          appeal_by: '', appeal_grounds: '', appeal_deposit: '0', appeal_at: 0, appeal_verdict: '', appeal_factors: [], appeal_reason: '',
          final_verdict: '', final_factors: [], final_severity: 0, penalty: '0', reversed: false, settled_at: 0,
        };
        pm.open_cases += 1; pm.filed += 1; dm.open_cases += 1; dm.defended += 1; dm.locked = (BigInt(dm.locked) + reserve).toString();
        r.open_cases += 1; r.cases += 1; return;
      }
      case 'submit_defense': {
        const c = this.kase(Number(a[0])); need(ctx.sender === c.defendant, 'only the defendant'); need(c.status === 'OPEN', 'not open');
        need(c.defense === '', 'already'); need(ctx.now <= c.defense_due, 'window closed');
        c.defense = bounded(a[1], 8, 700, 'defense'); c.defense_at = ctx.now; return;
      }
      case 'rule_case': {
        const c = this.kase(Number(a[0])); need(c.status === 'OPEN', 'not open'); need(c.defense !== '' || ctx.now >= c.defense_due, 'defense window still open');
        const r = this.rule(c.rule_id); const g = this.judge(ctx, 'CASE', r, c.statement, c.defense, '');
        c.verdict = g.verdict; c.factors = g.factors; c.severity = g.verdict === 'BREACH' ? 1 + g.factors.filter(Boolean).length : 0;
        c.reason = g.reason; c.ruled_at = ctx.now; c.appeal_due = ctx.now + r.appeal_minutes * 60; c.status = 'RULED'; return;
      }
      case 'waive_appeal': {
        const c = this.kase(Number(a[0])); need(c.status === 'RULED', 'not awaiting'); need(ctx.sender === this.appellant(c), 'only the party that may appeal'); c.waived = true; return;
      }
      case 'appeal_case': {
        const c = this.kase(Number(a[0])); need(c.status === 'RULED', 'not awaiting'); need(ctx.sender === this.appellant(c), 'adversely affected');
        need(!c.waived, 'waived'); need(ctx.now <= c.appeal_due, 'window closed'); const grounds = bounded(a[1], 8, 500, 'grounds');
        const r = this.rule(c.rule_id); const deposit = BigInt(r.filing_deposit); need(ctx.value === deposit, 'value must equal');
        const g = this.judge(ctx, 'APPEAL', r, c.statement, c.defense, grounds); this.receive(ctx, { exact: deposit });
        c.appeal_by = ctx.sender; c.appeal_grounds = grounds; c.appeal_deposit = deposit.toString(); c.appeal_at = ctx.now;
        c.appeal_verdict = g.verdict; c.appeal_factors = g.factors; c.appeal_reason = g.reason;
        this.settle(ctx, c, r, g, true); return;
      }
      case 'settle_case': {
        const c = this.kase(Number(a[0])); need(c.status === 'RULED', 'not awaiting'); need(c.waived || ctx.now > c.appeal_due, 'window still open');
        this.settle(ctx, c, this.rule(c.rule_id), { verdict: c.verdict, factors: c.factors, reason: c.reason }, false); return;
      }
      case 'void_case': {
        const c = this.kase(Number(a[0])); need(c.status === 'OPEN', 'not open'); need(ctx.now > c.defense_due + 7 * 24 * 3600, 'not yet voidable');
        const r = this.rule(c.rule_id); const pm = this.member(c.rule_id, c.plaintiff); const dm = this.member(c.rule_id, c.defendant);
        dm.locked = (BigInt(dm.locked) - BigInt(c.reserve)).toString(); pm.open_cases -= 1; dm.open_cases -= 1; r.open_cases -= 1; r.voided += 1;
        this.credit(c.plaintiff, BigInt(c.deposit));
        c.final_verdict = 'VOID'; c.final_factors = []; c.final_severity = 0; c.penalty = '0'; c.status = 'SETTLED'; c.settled_at = ctx.now; return;
      }
      case 'withdraw': {
        const amount = S.claimable[ctx.sender] ?? 0n; need(amount > 0n, 'nothing to withdraw'); need(this.contractBalance >= amount, 'balance too low');
        S.claimable[ctx.sender] = 0n; S.claimableTotal -= amount; S.escrow -= amount;
        ctx.entry.transfer = amount.toString();
        if (this.lostTransfers) return;   // finalizes, but the emitted transfer is never applied
        if (this.lateTransfers) { this.pendingTransfers.push({ to: ctx.sender, amount, ready: this.clockMs + 150_000, fromContract: true }); return; }
        this.contractBalance -= amount;
        this.pendingTransfers.push({ to: ctx.sender, amount, ready: this.clockMs + 60_000 });
        return;
      }
      default: throw new Error(`mock chain does not implement ${fn}`);
    }
  }
  appellant(c) { return c.verdict === 'BREACH' ? c.defendant : c.plaintiff; }
  settle(ctx, c, r, final, appealed) {
    const rid = c.rule_id; const unit = BigInt(r.penalty_unit); const deposit = BigInt(c.deposit);
    const pm = this.member(rid, c.plaintiff); const dm = this.member(rid, c.defendant);
    const verdict = final.verdict; const factors = final.factors;
    const severity = verdict === 'BREACH' ? 1 + factors.filter(Boolean).length : 0;
    let penalty = 0n; dm.locked = (BigInt(dm.locked) - BigInt(c.reserve)).toString();
    if (verdict === 'BREACH') {
      penalty = unit * BigInt(severity); if (penalty > BigInt(dm.stake)) penalty = BigInt(dm.stake);
      dm.stake = (BigInt(dm.stake) - penalty).toString(); dm.strikes += 1; dm.upheld_against += 1; this.credit(c.plaintiff, penalty + deposit); r.breaches += 1;
      if (dm.strikes >= r.strike_limit && dm.status === 'ACTIVE') { dm.status = 'EXPELLED'; r.members -= 1; }
    } else if (verdict === 'COMPLIANT') { this.credit(c.defendant, deposit); r.dismissed += 1; pm.dismissed_filings += 1; }
    else { this.credit(c.plaintiff, deposit); r.inconclusive += 1; }
    if (appealed) {
      const appellant = c.appeal_by; const ad = BigInt(c.appeal_deposit);
      const favorable = appellant === c.defendant ? verdict !== 'BREACH' : verdict === 'BREACH';
      if (favorable) {
        this.credit(appellant, ad); let slash = unit; if (slash > BigInt(r.bond_remaining)) slash = BigInt(r.bond_remaining);
        r.bond_remaining = (BigInt(r.bond_remaining) - slash).toString(); this.credit(appellant, slash); r.reversals += 1;
        if (r.reversals >= 2 && r.status === 'CERTIFIED') r.status = 'SUSPENDED';
      } else this.credit(appellant === c.defendant ? c.plaintiff : c.defendant, ad);
      c.reversed = favorable;
    }
    pm.open_cases -= 1; dm.open_cases -= 1; r.open_cases -= 1;
    c.final_verdict = verdict; c.final_factors = factors; c.final_severity = severity; c.penalty = penalty.toString(); c.status = 'SETTLED'; c.settled_at = ctx.now;
  }

  // ------------------------------------------------------------------------ views
  view(fn, a) {
    const S = this.S; const J = (x) => JSON.stringify(x);
    switch (fn) {
      case 'get_stats': return J({ rules: S.ruleCount, cases: S.caseCount, escrow_total: S.escrow.toString(), claimable_total: S.claimableTotal.toString() });
      case 'get_limits': return J({ min_probes: 4, max_probes: 6, max_factors: 3, max_revisions: 2, max_open_cases_per_member: 3, reversals_to_suspend: 2, max_page_size: 10, void_after_seconds: 604800 });
      case 'get_rule': return J(this.rule(Number(a[0])));
      case 'get_case': return J(this.kase(Number(a[0])));
      case 'get_member': return J(this.member(Number(a[0]), String(a[1]).toLowerCase()));
      case 'get_claimable': return (S.claimable[String(a[0]).toLowerCase()] ?? 0n).toString();
      case 'list_rules': { const out = []; const first = Math.max(1, Number(a[0])); for (let i = first; i <= Math.min(S.ruleCount, first + Math.min(Number(a[1]), 10) - 1); i++) out.push(S.rules[i]); return J(out); }
      case 'list_cases': { const out = []; const first = Math.max(1, Number(a[0])); for (let i = first; i <= Math.min(S.caseCount, first + Math.min(Number(a[1]), 10) - 1); i++) out.push(S.cases[i]); return J(out); }
      default: throw new Error(`mock chain does not implement view ${fn}`);
    }
  }
}
