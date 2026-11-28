#!/usr/bin/env node
/**
 * Re-check the wallet side of a finished live run, later.
 *
 *   node scripts/verify_withdrawals.mjs reports/live_e2e_studionet_<stamp>.json [--rpc=<url>]
 *
 * withdraw() emits the native transfer, and Studionet applies emitted transfers on FINALIZATION, which can come long
 * after the run stopped waiting. This script reads the report, asks the node for the current status of every
 * withdraw transaction and for the wallets' current balances.
 *
 *   - Reports written by the current live script carry `withdrawals` (balance before, amount, hash): each wallet is
 *     compared with `balanceBefore + amount`, less a transaction-fee allowance (the withdraw transaction's own fee left
 *     the wallet at the same time, so an exact `+amount` can never be seen). The check is only meaningful while the
 *     wallet has done nothing else since the run: spending from it afterwards can hide a credit.
 *   - Older reports only carry the transactions and the expected amounts: the script prints status and balance and
 *     cannot prove the credit by itself (there is no balance-before to compare with).
 *
 * Exit code: 0 all credited, 2 something still pending / not provable, 1 error.
 */
import { readFileSync } from 'node:fs';
import { createClient } from 'genlayer-js';
import { studionet } from 'genlayer-js/chains';
import { payoutArrived, retryTransient } from './lib/common.mjs';
import { normalizeReceipt } from './lib/real_chain.mjs';

const file = process.argv.slice(2).find((a) => !a.startsWith('--'));
const rpcArg = process.argv.slice(2).find((a) => a.startsWith('--rpc='));
if (!file) { console.error('usage: node scripts/verify_withdrawals.mjs <report.json> [--rpc=<url>]'); process.exit(1); }

const report = JSON.parse(readFileSync(file, 'utf8'));
const client = createClient({ chain: studionet, ...(rpcArg ? { endpoint: rpcArg.slice(6) } : {}) });
const retry = (fn) => retryTransient(fn, { attempts: 5 });

let rows = report.withdrawals;
if (!Array.isArray(rows) || rows.length === 0) {
  // older report: rebuild what it still knows
  rows = [];
  for (const t of report.txs.filter((x) => x.fn === 'withdraw' && x.hash && x.executionOk)) {
    const name = /^withdraw (\w+) /.exec(t.label)?.[1];
    const detail = report.checks.find((c) => c.name.startsWith(`${name}: wallet balance received`))?.detail ?? '';
    const amount = /expected \+(\d+) wei/.exec(detail)?.[1] ?? null;
    if (name && report.accounts[name]) rows.push({ name, address: report.accounts[name], amount, balanceBefore: null, hash: t.hash });
  }
}
if (rows.length === 0) { console.error('no withdraw transactions found in this report'); process.exit(1); }

if (report.contract) {
  const contractBalance = BigInt(await retry(() => client.getBalance({ address: report.contract })));
  console.log(`contract ${report.contract} native balance now: ${contractBalance} wei (a fully wound-down contract holds 0, or only value that rejected payable calls left behind)`);
}
let allProven = true;
for (const row of rows) {
  const status = normalizeReceipt(row.hash, await retry(() => client.getTransaction({ hash: row.hash }))).status;
  const balance = BigInt(await retry(() => client.getBalance({ address: row.address })));
  let verdict;
  if (row.balanceBefore != null && row.amount != null) {
    verdict = payoutArrived(row.balanceBefore, balance, row.amount) ? 'CREDITED' : (status === 'FINALIZED' ? 'FINALIZED BUT NOT CREDITED' : 'PENDING FINALIZATION');
  } else {
    verdict = status === 'FINALIZED' ? 'FINALIZED (credit not provable: no balance-before in this report)' : 'PENDING FINALIZATION';
  }
  if (!verdict.startsWith('CREDITED')) allProven = false;
  console.log(`${row.name.padEnd(10)} tx ${status.padEnd(18)} balance ${balance}  expected +${row.amount ?? '?'}  -> ${verdict}`);
}
process.exit(allProven ? 0 : 2);
