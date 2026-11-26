// Real Studionet adapter built on genlayer-js 1.1.8 (the version pinned in package.json).
import { createClient, createAccount, generatePrivateKey } from 'genlayer-js';
import { studionet } from 'genlayer-js/chains';
import { TransactionStatus } from 'genlayer-js/types';
import { sleepReal, retryTransient } from './common.mjs';

const STATUS_NAMES = {
  0: 'UNINITIALIZED', 1: 'PENDING', 2: 'PROPOSING', 3: 'COMMITTING', 4: 'REVEALING', 5: 'ACCEPTED',
  6: 'UNDETERMINED', 7: 'FINALIZED', 8: 'CANCELED', 9: 'APPEAL_REVEALING', 10: 'APPEAL_COMMITTING',
  11: 'READY_TO_FINALIZE', 12: 'VALIDATORS_TIMEOUT', 13: 'LEADER_TIMEOUT',
};

/** Turn whatever shape the RPC returned into one flat summary. Primary truth is always contract state. */
export function normalizeReceipt(hash, r) {
  const statusName = r?.status_name ?? r?.statusName ?? STATUS_NAMES[String(r?.status)] ?? 'UNKNOWN';
  const leaders = Array.isArray(r?.consensus_data?.leader_receipt) ? r.consensus_data.leader_receipt
    : r?.consensus_data?.leader_receipt ? [r.consensus_data.leader_receipt] : [];
  const leader = leaders.find((x) => x?.mode === 'leader') ?? leaders[0] ?? null;
  let executionOk = null;
  if (leader?.execution_result !== undefined) {
    const text = String(leader.execution_result).toUpperCase();
    if (/^(SUCCESS|FINISHED_WITH_RETURN)$/.test(text)) executionOk = true;
    else if (/ERROR/.test(text)) executionOk = false;
  }
  if (executionOk === null && (r?.txExecutionResultName || r?.tx_execution_result_name)) {
    executionOk = (r.txExecutionResultName ?? r.tx_execution_result_name) === 'FINISHED_WITH_RETURN';
  }
  const payload = leader?.result?.payload?.readable ?? leader?.genvm_result?.stderr ?? leader?.genvm_result?.stdout ?? null;
  const votes = r?.last_round?.validator_votes_name ?? r?.lastRound?.validatorVotesName ?? [];
  return {
    hash, status: statusName, executionOk,
    error: executionOk === false ? String(payload ?? 'execution failed').slice(0, 300) : null,
    votes, resultName: r?.result_name ?? r?.resultName ?? null,
    contractAddress: findContractAddress(r),
  };
}

/** The deployed address lives in data.contract_address on Studio; fall back to a recursive search, then to_address. */
function findContractAddress(r) {
  const seen = new Set();
  const walk = (node, depth) => {
    if (!node || typeof node !== 'object' || depth > 6 || seen.has(node)) return null;
    seen.add(node);
    for (const [k, v] of Object.entries(node)) {
      if ((k === 'contract_address' || k === 'contractAddress') && typeof v === 'string' && /^0x[0-9a-fA-F]{40}$/.test(v)) return v;
    }
    for (const v of Object.values(node)) { const found = walk(v, depth + 1); if (found) return found; }
    return null;
  };
  const direct = walk(r, 0);
  if (direct) return direct;
  const to = r?.to_address ?? r?.toAddress;
  return typeof to === 'string' && /^0x[0-9a-fA-F]{40}$/.test(to) && !/^0x0{40}$/.test(to) ? to : null;
}

/**
 * The public Studionet RPC allows 30 requests per minute and answers with an error (or an HTML page) beyond that.
 * Every request to the RPC host goes through one queue with a minimum gap, and rate-limit answers are retried.
 */
function installThrottle(rpcUrl) {
  if (globalThis.__litmusThrottle) return;
  const host = new URL(rpcUrl).host;
  const original = globalThis.fetch.bind(globalThis);
  const gap = Number(process.env.LITMUS_MIN_GAP_MS ?? 2300);
  let nextSlot = 0;
  globalThis.__litmusThrottle = true;
  globalThis.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : (input?.url ?? String(input));
    if (!url.includes(host)) return original(input, init);
    for (let attempt = 0; ; attempt += 1) {
      const now = Date.now();
      const wait = Math.max(0, nextSlot - now);
      nextSlot = Math.max(now, nextSlot) + gap;
      if (wait) await sleepReal(wait);
      const response = await original(input, init);
      const type = response.headers.get('content-type') ?? '';
      let limited = [429, 502, 503, 504].includes(response.status) || type.includes('text/html');
      if (!limited && type.includes('json')) limited = /rate limit/i.test(await response.clone().text());
      if (!limited || attempt >= 6) return response;
      nextSlot = Math.max(nextSlot, Date.now() + Number(process.env.LITMUS_BACKOFF_MS ?? 8000) * (attempt + 1));
    }
  };
}

export class RealChain {
  constructor({ rpc, retries = 200, intervalMs = 5000 } = {}) {
    installThrottle(rpc ?? studionet.rpcUrls.default.http[0]);
    this.rpc = rpc;
    this.retries = retries;
    this.intervalMs = intervalMs;
    this.client = createClient({ chain: studionet, ...(rpc ? { endpoint: rpc } : {}) });
    this.isDryRun = false;
  }
  now() { return Date.now(); }
  async sleep(ms) { return sleepReal(ms); }
  newAccount(privateKey) {
    const key = privateKey ?? generatePrivateKey();
    const account = createAccount(key);
    return { address: account.address.toLowerCase(), key, account };
  }
  async fundAccount(address, weiAmount) {
    await this.client.request({ method: 'sim_fundAccount', params: [address, Number(weiAmount)] });
  }
  async balance(address) { return BigInt(await retryTransient(() => this.client.getBalance({ address }))); }
  async deploy(code, acct) {
    const hash = await this.client.deployContract({ account: acct.account, code, args: [] });
    const raw = await this.client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: this.retries, interval: this.intervalMs, fullTransaction: true });
    const receipt = normalizeReceipt(hash, raw);
    return { receipt, address: receipt.contractAddress, raw };
  }
  async send(acct, address, fn, args, value = 0n) {
    let hash = null;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      try {
        hash = await this.client.writeContract({ account: acct.account, address, functionName: fn, args, value: BigInt(value) });
        break;
      } catch (error) {
        const message = String(error?.message ?? error);
        // A rate-limited submission was never broadcast, so it is safe to submit again; anything else is surfaced.
        if (attempt === 3 || !/rate limit/i.test(message)) throw error;
        await sleepReal(10_000 * (attempt + 1));
      }
    }
    // The hash is known from here on: never resubmit, only keep asking for the receipt.
    for (let attempt = 0; attempt < 10; attempt += 1) {
      try {
        const raw = await this.client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: this.retries, interval: this.intervalMs });
        return normalizeReceipt(hash, raw);
      } catch (error) {
        if (attempt === 9) return { hash, status: 'WAIT_ERROR', executionOk: null, error: String(error?.shortMessage ?? error?.message ?? error).slice(0, 200), votes: [] };
        await sleepReal(10_000);
      }
    }
    return { hash, status: 'WAIT_ERROR', executionOk: null, error: 'receipt unavailable', votes: [] };
  }
  /** Polls the transaction until FINALIZED. Transient RPC errors never end the wait early. */
  async waitFinalized(hash, timeoutMs) {
    const started = Date.now();
    let last = 'UNKNOWN';
    while (Date.now() - started < timeoutMs) {
      try {
        const summary = normalizeReceipt(hash, await this.client.getTransaction({ hash }));
        last = summary.status;
        if (['FINALIZED', 'CANCELED', 'UNDETERMINED'].includes(last)) return { ...summary, waitedSec: Math.round((Date.now() - started) / 1000), timedOut: false };
      } catch (error) { /* transient: keep waiting */ }
      await sleepReal(20_000);
    }
    return { hash, status: last, waitedSec: Math.round((Date.now() - started) / 1000), timedOut: true };
  }
  async txStatus(hash) {
    try { return normalizeReceipt(hash, await this.client.getTransaction({ hash })).status; } catch (error) { return 'UNKNOWN'; }
  }
  /** Child transactions created by emitted messages (emit_transfer shows up here on Studio). */
  async triggered(hash) {
    // Older/newer genlayer-js builds may not expose this call: report 'unavailable' (null) instead of throwing.
    if (typeof this.client.getTriggeredTransactionIds !== 'function') return null;
    const ids = await retryTransient(() => this.client.getTriggeredTransactionIds({ hash }));
    const out = [];
    for (const id of ids) {
      const tx = await this.client.getTransaction({ hash: id });
      out.push({ id, status: normalizeReceipt(id, tx).status, to: String(tx?.to_address ?? tx?.toAddress ?? tx?.to ?? '').toLowerCase(), value: tx?.value ?? null, type: tx?.type ?? null });
    }
    return out;
  }
  async read(address, fn, args = []) {
    return retryTransient(() => this.client.readContract({ address, functionName: fn, args }));
  }
  async contractCode(address) {
    const response = await fetch(this.rpc ?? studionet.rpcUrls.default.http[0], {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'gen_getContractCode', params: [address] }),
    });
    const payload = await response.json();
    if (typeof payload.result !== 'string') throw new Error(`gen_getContractCode failed: ${JSON.stringify(payload).slice(0, 200)}`);
    return Buffer.from(payload.result, 'base64');
  }
}
