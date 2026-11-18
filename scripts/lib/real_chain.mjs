// Real Studionet adapter built on genlayer-js 1.1.8 (the version pinned in package.json).
import { createClient, createAccount, generatePrivateKey } from 'genlayer-js';
import { studionet } from 'genlayer-js/chains';
import { TransactionStatus } from 'genlayer-js/types';
import { sleepReal } from './common.mjs';

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

export class RealChain {
  constructor({ rpc, retries = 400, intervalMs = 3000 } = {}) {
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
  async balance(address) { return BigInt(await this.client.getBalance({ address })); }
  async deploy(code, acct) {
    const hash = await this.client.deployContract({ account: acct.account, code, args: [] });
    const raw = await this.client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: this.retries, interval: this.intervalMs, fullTransaction: true });
    const receipt = normalizeReceipt(hash, raw);
    return { receipt, address: receipt.contractAddress, raw };
  }
  async send(acct, address, fn, args, value = 0n) {
    const hash = await this.client.writeContract({ account: acct.account, address, functionName: fn, args, value: BigInt(value) });
    const raw = await this.client.waitForTransactionReceipt({ hash, status: TransactionStatus.ACCEPTED, retries: this.retries, interval: this.intervalMs });
    return normalizeReceipt(hash, raw);
  }
  async waitFinalized(hash, timeoutMs) {
    const retries = Math.max(1, Math.ceil(timeoutMs / this.intervalMs));
    try {
      const raw = await this.client.waitForTransactionReceipt({ hash, status: TransactionStatus.FINALIZED, retries, interval: this.intervalMs });
      return normalizeReceipt(hash, raw);
    } catch (error) {
      return null;
    }
  }
  async read(address, fn, args = []) {
    return this.client.readContract({ address, functionName: fn, args });
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
