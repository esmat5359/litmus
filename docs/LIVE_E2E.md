# Live end-to-end run

```bash
npm ci
node scripts/live_e2e.mjs                 # deploys a fresh contract on Studionet and runs everything
node scripts/live_e2e.mjs --skip-slow     # omit the timeout case that waits for two 3-minute windows
node scripts/live_e2e.mjs --address=0x... # reuse a FRESH deployment (rules and cases must be 0)
node scripts/live_e2e.mjs --strict        # warnings also fail the run
node scripts/live_e2e.mjs --dry-run       # no network: in-process model with a virtual clock
```

Options: `--llm-attempts=3`, `--finality-timeout-min=35` (how long to wait at the end for the withdraw transactions to finalize), `--out-dir=reports`, `--rpc=<url>`.
The public RPC allows 30 requests per minute, so every request goes through one throttled queue
(`LITMUS_MIN_GAP_MS`, default 2300) that also retries rate-limit answers (`LITMUS_BACKOFF_MS`, default 8000).
Environment: `GENLAYER_RPC` (default `https://studio.genlayer.com/api`), `LITMUS_KEYS` (five pre-funded
private keys: author, plaintiff, defendant, observer, outsider; if absent the script creates five accounts
and funds them with the Studio RPC method `sim_fundAccount`; if that is unavailable it stops and tells you
which addresses to fund).

Expected duration on Studionet: about 45-90 minutes (the last part is the wait for finalization, which ends as soon as the withdraw transactions finalize). The public RPC allows 30 requests per minute, which is
the real bottleneck (each transaction needs several requests); model-backed transactions take roughly 30-120 s
and the timeout case runs in the background of the other cases so its wait is mostly hidden.

## What it does

| Stage | Checks |
|---|---|
| Setup | five funded accounts; contract source LF-only and BOM-free |
| Deploy | deployed address; fresh state; **deployed bytes equal the committed source** |
| Forge | forging with an empty ledger and three invalid forges are rejected and leave state **and ledger** untouched; the audit then demands contract balance == escrow exactly (rejected calls strand nothing); the real forge spends exactly the bond |
| Litmus test | five golden probes are judged by the validators under exact consensus; each result is compared with the author's expectation; certification only if the clarity threshold holds; the stored digest equals the digest the script computes independently in JavaScript |
| Members | a ledger one wei short is rejected and untouched; exact stakes spent; a double enlist is rejected and the ledger kept; filing with an empty ledger is rejected |
| Case T (timeout) | filed early; ruled with **no defense** once its window closed; settled with **no waiver** after the appeal window; reaches the strike limit and the defendant is expelled |
| Case A | wrong-deposit / self / non-member filings rejected; early ruling rejected; non-defendant defense rejected; ruling; early settle rejected; winner cannot waive; waive; settle |
| Case B | compliant path: deposit forfeited to the defendant |
| Case C | appeal path: winner cannot appeal, wrong deposit rejected, appeal judged afresh; reversal flag and bond slash (if any) verified |
| Every settlement | an independent oracle recomputes penalty, severity, each wallet's credit, defendant stake, strikes, released reservation, rule counters, bond and reversals **from the on-chain verdict** and compares |
| Audits | after every stage `escrow_total` equals the recomputed sum of every liability; the native balance covers it |
| Withdrawals | the plaintiff and the defendant withdraw **early** (right after cases A and B) so that finality latency overlaps the rest of the run; every withdrawal's emitted transfer message (child transaction) is read back and must name the wallet and the exact amount; at the end the script waits for finalization and reports each transaction's status and the child transfer's status |
| Exit | members leave (expelled members get their remaining stake), rule retired, bond reclaimed, everyone withdraws, escrow and contract balance end at zero, and **every wallet is reconciled**: start - value sent + value withdrawn must equal the final balance, any gap is printed |

## Reading the report

`reports/live_e2e_<mode>_<timestamp>.md` (human) and `.json` (machine) list every check and every
transaction with its explorer link.

* **PASS / FAIL** are hard results. The process exits 1 on any FAIL.
* **WARN** means the run is correct but something is worth reading (a wallet reconciliation gap, for
  example, tells you whether the network credited the withdrawals): a model-backed step needed a retry; the
  validators' ruling differed from the designed expectation (this is *information about the model*, the
  accounting is still verified against what they agreed); a wallet was not credited within the finality
  wait (native transfers are emitted on finalization); the contract balance differs from escrow.
* **INFO** is neutral context.

Language-model steps are retried up to `--llm-attempts` times because the exact-equality law can make the
validators legitimately fail to agree; each retry is a WARN and nothing is hidden.

## Why it trusts state, not receipts

Receipt shapes differ between SDK versions and networks. Every step is accepted only if the **contract
state read back through the views** shows the expected effect (for example a probe's `result` is no longer
`PENDING`), and every rejection check passes only if the state is unchanged. The receipt (status, leader
execution result, votes) is recorded for diagnosis.

## Dry run and the differential test

`--dry-run` runs the identical script against `scripts/lib/mock_chain.mjs`, a JavaScript model of the
contract with a deterministic stand-in for the validators and a virtual clock. It records a transcript.
`tests/direct/test_dryrun_replay.py` replays that transcript against the real Python contract in Direct Mode
and requires the final stats, rules, cases, members and ledgers to be identical, which keeps the model, the
script's accounting oracle and the contract from drifting apart.

## Post-mortem of a finished run

```bash
source .env                                   # LITMUS_KEYS from the run
node scripts/check_balances.mjs --report=reports/live_e2e_studionet_<stamp>.json
```

For every withdraw transaction in the report it prints the transaction status, the transfer message the
contract emitted (recipient, value, status) and the current wallet and contract balances. Reading it:

* transfer message `FINALIZED`, wallet raised by the amount: everything worked, the run simply ended before finality;
* transfer message `PENDING`/`ACCEPTED`: not processed yet, look again later;
* transfer message `FINALIZED` but the wallet did **not** rise: a network accounting issue, not the contract;
* no transfer message at all: the contract did not emit one (would be a contract bug).
