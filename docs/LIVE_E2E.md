# Live end-to-end run

```bash
npm ci
node scripts/live_e2e.mjs                 # deploys a fresh contract on Studionet and runs everything
node scripts/live_e2e.mjs --skip-slow     # omit the timeout case that waits for the 3-minute defense and 4-minute appeal windows
node scripts/live_e2e.mjs --address=0x... # reuse a FRESH deployment (rules and cases must be 0)
node scripts/live_e2e.mjs --strict        # warnings also fail the run
node scripts/live_e2e.mjs --dry-run       # no network: in-process model with a virtual clock
node scripts/live_e2e.mjs --dry-run --retain-rejected             # model the Studionet behaviour: rejected payable calls keep their value
node scripts/live_e2e.mjs --dry-run --retain-rejected --flaky-rpc # ... and inject a rate-limit refusal and a lost receipt
node scripts/verify_withdrawals.mjs reports/live_e2e_studionet_<stamp>.json   # re-check wallet credits after finalization
```

Options: `--llm-attempts=3`, `--finality-timeout-min=10`, `--out-dir=reports`, `--rpc=<url>`.
Environment: `GENLAYER_RPC` (default `https://studio.genlayer.com/api`), `LITMUS_KEYS` (five pre-funded
private keys: author, plaintiff, defendant, observer, outsider; if absent the script creates five accounts
and funds them with the Studio RPC method `sim_fundAccount`; if that is unavailable it stops and tells you
which addresses to fund).

Expected duration on Studionet: about 25-40 minutes (each model-backed transaction takes roughly 30-120 s;
the timeout case runs in the background of the other cases so the wait is mostly hidden).

## What it does

| Stage | Checks |
|---|---|
| Setup | five funded accounts; contract source LF-only and BOM-free |
| Deploy | deployed address; fresh state; **deployed bytes equal the committed source** |
| Forge | three rejected forges leave state untouched; whether rejected payable calls retain value is reported; the real forge escrows the exact bond |
| Litmus test | five golden probes are judged by the validators under exact consensus; each result is compared with the author's expectation; certification only if the clarity threshold holds; the stored digest equals the digest the script computes independently in JavaScript |
| Members | a one-wei-short stake is rejected; exact stakes escrowed; double enlist rejected |
| Case T (timeout) | filed early; ruled with **no defense** once its window closed; settled with **no waiver** after the appeal window; reaches the strike limit and the defendant is expelled |
| Case A | wrong-deposit / self / non-member filings rejected; early ruling rejected; non-defendant defense rejected; ruling; early settle rejected; winner cannot waive; waive; settle |
| Case B | compliant path: deposit forfeited to the defendant |
| Case C | appeal path: winner cannot appeal, wrong deposit rejected, appeal judged afresh; reversal flag and bond slash (if any) verified |
| Every settlement | an independent oracle recomputes penalty, severity, each wallet's credit, defendant stake, strikes, released reservation, rule counters, bond and reversals **from the on-chain verdict** and compares |
| Audits | after every stage `escrow_total` equals the recomputed sum of every liability; the native balance covers it |
| Exit | members leave (expelled members get their remaining stake), rule retired, bond reclaimed, everyone withdraws, wallet balances are polled until credited, escrow and contract balance end at zero |

## Reading the report

`reports/live_e2e_<mode>_<timestamp>.md` (human) and `.json` (machine) list every check and every
transaction with its explorer link.

* **PASS / FAIL** are hard results. The process exits 1 on any FAIL.
* **WARN** means the run is correct but something is worth reading: a model-backed step needed a retry; the
  validators' ruling differed from the designed expectation (this is *information about the model*, the
  accounting is still verified against what they agreed); a rejection was not proven by a receipt (see below);
  the contract balance is only explained with submissions whose receipt was lost; a wallet was not credited
  within the finality wait.
* **INFO** is neutral context.

Language-model steps are retried up to `--llm-attempts` times because the exact-equality law can make the
validators legitimately fail to agree; each retry is a WARN and nothing is hidden.

## Why it trusts state, not receipts

Receipt shapes differ between SDK versions and networks. Every step is accepted only if the **contract
state read back through the views** shows the expected effect (for example a probe's `result` is no longer
`PENDING`).

A **rejection** check needs both: the state is unchanged *and* there is a transaction hash whose execution result
is a contract error (`exec=false`). "State unchanged" alone proves nothing, because a request that never reached the
chain (rate limit, gateway page) leaves the state unchanged too. Without a receipt the step is repeated and
reported as a WARN; a call that was expected to fail but executed successfully is a FAIL.

Transport errors are handled in `scripts/lib/real_chain.mjs`: a transaction is submitted once (only an explicit
rate-limit refusal is repeated, because nothing was accepted then); after that, transient polling errors (rate limit,
HTML gateway pages) are retried against the **same hash**, so a rate-limited poll can no longer turn an accepted
transaction into a send error. The public Studio RPC allows 30 requests per minute, so polling runs every 5 s.

## Dry run and the differential test

`--dry-run` runs the identical script against `scripts/lib/mock_chain.mjs`, a JavaScript model of the
contract with a deterministic stand-in for the validators and a virtual clock. It records a transcript.
`tests/direct/test_dryrun_replay.py` replays that transcript against the real Python contract in Direct Mode
and requires the final stats, rules, cases, members and ledgers to be identical, which keeps the model, the
script's accounting oracle and the contract from drifting apart.

## Value attached to rejected payable calls

When the contract rejects a payable call (`forge_rule`, `enlist`, `file_case`, `appeal_case` with a wrong amount, on
a retired rule, twice ...), the call is rolled back, but **Studionet keeps the attached value in the contract's native
balance**. The contract cannot refund it: raising an error reverts everything the call did, including a refund.
This value is not owed to anybody and is not part of `escrow_total`; `get_stats` and the books stay exact.

The script measures this once (three rejected forges, `value attached to rejected payable calls ...`), then tracks
the value of every rejection it can prove from a receipt and audits **exactly**:

    contract balance = escrow_total + value retained by rejected calls

After the wind-down the balance must equal exactly the retained value. Submissions whose receipt was lost might or
might not have reached the chain; they widen the expected balance by their value and are reported as a WARN.
The retained total is listed as an INFO and stored in the JSON report (`retainedValue`). Practical rule for users:
attach exactly the required amount; a rejected call does not refund.

## Wallet credits and finalization

`withdraw` zeroes the ledger and emits the transfer. Two things can be checked, and they are checked separately:

1. *contract side*, immediately: the contract's native balance fell by exactly the amount withdrawn;
2. *wallet side*: the recipient's balance rises when the transfer is applied, which Studionet does on finalization
   of the withdraw transaction. The script waits for finalization (`--finality-timeout-min`, default 10) and says
   in the check which status the withdraw transaction had. If it ends in a WARN, run
   `node scripts/verify_withdrawals.mjs <report.json>` later: it reads the withdraw transactions' current status and
   the wallets' current balances (the report stores `withdrawals` with balance-before and amount).

## The Studionet run of 2026-11-18 (`reports/live_e2e_studionet_2026-11-18T14-18-08-667Z.*`)

Contract `0x259C458f4f12552dcCa28d7751809d391CACbe10`, 162 PASS, 0 FAIL, 15 WARN, 3 INFO. The 15 warnings, by cause:

| Checks | Cause | Status |
|---|---|---|
| C012, C021, C047, C079, C105, C130, C148, C164, C180 (9) | Value attached to rejected payable calls stays in the contract (see above). Every figure reconciles to the wei: the three rejected forges attached 14999999999999999 wei (C012 says the same); the final balance of 42499999999999996 wei equals the value attached to the 11 rejected payable calls of the run, including the rejected appeal of C113. Escrow and claimable ended at 0. | Explained. Not a bookkeeping error; the old script could not name the cause. |
| C083, C084 (2) | `Rate limit exceeded: 30 requests per minute` from the public RPC while polling `submit_defense B`; the call was repeated and took effect (state verified). | Transport. Fixed by retrying polls on the same hash and polling every 5 s. |
| C174-C177 (4) | The wallets showed +0 after the finalization wait. The contract side did happen: the balance fell from 75999999999999996 to 42499999999999996, exactly the 33500000000000000 wei withdrawn. The old script ignored the result of its finalization wait, so the report cannot say whether the withdraw transactions had finalized. | **Not proven in that run.** Re-check with `scripts/verify_withdrawals.mjs`; later runs report the transaction status in the check. |

Also noted in that run: **C113** (`appeal_case C rejected for the party that won`) passed although it ended as
`SEND_ERROR` with a gateway page (`Unexpected token '<'`) and no receipt; the old check treated "state unchanged" as proof. The balance
arithmetic above shows the call did reach the chain (its 100000000000000 wei deposit is part of the retained
balance), but the report itself does not prove the rejection. The current script requires a receipt for every rejection.
