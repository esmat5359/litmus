# Architecture

Single contract, `Litmus` (`contracts/Litmus.py`), GenVM v0.4 (header `# v0.4.0`), runner
`py-genlayer:kzybg95x1gn21dw72m7hyg0maaap04n4nx55j9yt6fkdqxtz6bz0`.

## Storage

| Field | Type | Meaning |
|---|---|---|
| `rule_count`, `case_count` | `u32` | id counters (ids start at 1) |
| `rules` | `TreeMap[u32, str]` | JSON rule records |
| `cases` | `TreeMap[u32, str]` | JSON case records |
| `members` | `TreeMap[str, str]` | key `"<rule_id>:<wallet>"`, JSON member records |
| `claimable` | `TreeMap[str, u256]` | pull-ledger per lower-case wallet |
| `escrow_total` | `u256` | everything the contract holds for others |
| `claimable_total` | `u256` | the part of `escrow_total` already owed to wallets |

Money inside records is a **decimal string** so no client ever loses precision.

Solvency invariant (asserted by every test via `World.audit()` and by the live script after every stage):

```
escrow_total = sum(bond_remaining) + sum(member stakes) + sum(deposits of non-settled cases) + claimable_total
escrow_total = total value received - total value withdrawn
```

## Records

**Rule**: id, author, title, text, version, status (`DRAFT|CERTIFIED|SUSPENDED|RETIRED`), aggravators[0..3],
probes[4..6] (scenario, expected, expected factors, result `PENDING|PASS|FAIL`, got, got_factors, reason),
penalty_unit, filing_deposit, enlist_stake, strike_limit (1-5), pass_percent (75-100), defense_minutes,
appeal_minutes (2-10080), bond, bond_remaining, probes_run, probes_passed, clarity, digest (sha256 of the
canonical rule+probes, set at certification), counters (members, open_cases, cases, breaches, dismissed,
inconclusive, voided, reversals), timestamps.

**Member**: stake, locked (reservations for open cases against them), strikes, status
(`ACTIVE|EXPELLED|LEFT`), open_cases, filed, defended, upheld_against, dismissed_filings.

**Case**: parties, statement, defense, deadlines, first ruling (verdict, factors, severity, reason),
appeal (by, grounds, deposit, second ruling), final (verdict incl. `VOID`, factors, severity), penalty,
`reversed`, status (`OPEN|RULED|SETTLED`).

## Rules about amounts

* `enlist_stake >= penalty_unit x (1 + number of aggravators)`: one stake always covers one worst-case
  penalty. Filing a case against a member reserves that maximum from their stake (`locked`); the reservation
  is released at settlement, so a penalty is never partial.
* `bond >= 3 x penalty_unit`. Each favourable appeal slashes `min(penalty_unit, bond_remaining)` to the
  appellant. The remainder is reclaimable after retirement once no case is open.
* Payable calls take an **exact** value (`enlist`, `file_case`, `appeal_case`). `forge_rule` takes any value
  at or above the minimum bond.

## Timing

All deadlines use the transaction timestamp. `submit_defense` is allowed while `now <= defense_due`;
`rule_case` once a defense exists or `now >= defense_due`. `appeal_case` while `now <= appeal_due`;
`settle_case` once waived or `now > appeal_due`. `void_case` once `now > defense_due + 7 days` on a case that
is still OPEN. The windows are fixed per rule at forge time (2 minutes to 7 days).

## Who can do what

The appellant is the **adversely affected party**: the defendant after a BREACH ruling, the plaintiff after
COMPLIANT or INCONCLUSIVE. Only they can `waive_appeal` or `appeal_case`. Everything else that only depends
on time or on already agreed data is permissionless.

## Withdrawals

`withdraw` zeroes the wallet's ledger entry and the totals **before** emitting
`gl.chain.Account(address).emit_transfer(value=amount)` (the v0.4 form; `gl.get_contract_at(...).emit_transfer` is gone) and
refuses if the contract's native balance is below the amount. The transfer is an emitted message that sends value only and
never runs code at the receiver; it is applied on finalization, so a wallet is credited when the transaction finalizes. On
Studio v0.123 (Consensus v0.6) the transaction that emits it must carry `messageAllocations` in its fees; the live script
obtains them from `estimateTransactionFeesForWrite`.

## GenVM v0.4 port (Studio v0.123)

The v0.2.16 contract was ported without changing a rule, an amount or a method signature (24 methods: 8 views, 16 writes):

| v0.2 | v0.4 |
|---|---|
| `# { "Depends": "py-genlayer:1jb45aa8..." }` on line 1 | `# v0.4.0` on line 1, `# { "Depends": "py-genlayer:kzybg95x1gn21dw72m7hyg0maaap04n4nx55j9yt6fkdqxtz6bz0" }` on line 2 |
| `from genlayer import *` (bound `gl`) | `import genlayer as gl`, `from genlayer.types import *`, `from genlayer.storage import TreeMap` |
| `class Litmus(gl.Contract)` | `class Litmus(gl.contract.Contract)` |
| `gl.vm.run_nondet_unsafe(leader, validator)` | `gl.vm.run_nondet(leader, validator)` |
| `leader_result.message` of a `gl.vm.UserError` | `leader_result.data` |
| `gl.get_contract_at(Address(who)).emit_transfer(value=...)` | `gl.chain.Account(Address(who)).emit_transfer(value=...)` |

Audited and already compliant, no change needed: no child contracts (`salt_nonce` / `gl.contract.Salted` do not apply, so no
child address changes), no `__receive__` (value enters only through the four named `@gl.public.write.payable` methods),
no decorator on `__init__`, every persistent field is a GenVM storage type (`u32`, `u256`, `TreeMap`), and the
nondeterministic closures capture only a `str`, an `int` and a tuple, so no `gl.storage.copy_to_memory` is needed.
`tests/test_static_guards.py` pins all of this.

**The v0.4 contract is a new contract**: v0.2 contracts keep their address and state on Studio v0.123 for now, but nothing
migrates. Deploy it fresh (`node scripts/live_e2e.mjs` deploys, source-checks and records the new address) and use the new
address everywhere.
