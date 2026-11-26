#!/usr/bin/env node
/**
 * Post-mortem for a finished live run: what became of the withdrawals?
 *
 *   source .env                     # exports LITMUS_KEYS (author,plaintiff,defendant,observer,outsider)
 *   node scripts/check_balances.mjs --report=reports/live_e2e_studionet_<stamp>.json
 *
 * For every `withdraw` transaction of the report it prints the transaction status, the transfer message
 * (child transaction) the contract emitted, its recipient, value and status, then the current wallet
 * balances and the contract balance. A transfer that is FINALIZED while the wallet did not rise is a network
 * issue; a transfer that is still PENDING/ACCEPTED just has not been processed yet.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fmtGen } from './lib/common.mjs';
import { RealChain } from './lib/real_chain.mjs';

const argv = Object.fromEntries(process.argv.slice(2).map((a) => { const [k, v] = a.replace(/^--/, '').split('='); return [k, v ?? true]; }));
if (!argv.report || argv.report === true) throw new Error('usage: node scripts/check_balances.mjs --report=<report.json> [--rpc=<url>]');
const report = JSON.parse(readFileSync(resolve(String(argv.report)), 'utf8'));
const keys = (process.env.LITMUS_KEYS ?? '').split(',').map((k) => k.trim()).filter(Boolean);
const chain = new RealChain({ rpc: argv.rpc ? String(argv.rpc) : (process.env.GENLAYER_RPC ?? 'https://studio.genlayer.com/api') });
const names = ['author', 'plaintiff', 'defendant', 'observer', 'outsider'];
const accounts = Object.fromEntries(names.map((n, i) => [n, report.accounts?.[n] ?? (keys[i] ? chain.newAccount(keys[i]).address : null)]));

console.log(`contract ${report.contract}  (report ${report.startedAt})\n`);
const withdraws = (report.txs ?? []).filter((t) => t.fn === 'withdraw' && t.hash && t.executionOk !== false);
for (const t of withdraws) {
  const status = await chain.txStatus(t.hash);
  console.log(`${t.label}\n  tx ${t.hash}  status ${status}`);
  try {
    const kids = await chain.triggered(t.hash);
    if (!kids.length) console.log('  no child transaction: the contract did not emit a transfer message');
    for (const k of kids) console.log(`  transfer message ${k.id}\n    to ${k.to}  value ${k.value}  status ${k.status}`);
  } catch (error) { console.log(`  child transactions unavailable: ${String(error?.shortMessage ?? error?.message ?? error).split('\n')[0]}`); }
}
const safeBalance = async (address) => { try { return await chain.balance(address); } catch (error) { return null; } };
console.log('\nbalances now:');
for (const [name, address] of Object.entries(accounts)) {
  if (!address) continue;
  const bal = await safeBalance(address);
  console.log(`  ${name.padEnd(9)} ${address}  ${bal === null ? 'unavailable (RPC unreachable?)' : `${bal} wei  (${fmtGen(bal)})`}`);
}
if (report.contract) {
  const bal = await safeBalance(report.contract);
  console.log(`  contract  ${report.contract}  ${bal === null ? 'unavailable (RPC unreachable?)' : `${bal} wei`}`);
}
