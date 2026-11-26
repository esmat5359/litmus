# Architecture

Single contract, `Litmus` (`contracts/Litmus.py`), GenVM v0.2.16, runner
`py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`.

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
`emit_transfer(value=amount)` and refuses if the contract's native balance is below the amount. Transfers
are emitted on finalization (the SDK default), so a wallet is credited when the transaction finalizes.
