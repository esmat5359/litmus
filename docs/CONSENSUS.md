# Consensus law

## The law

For every value that is **stored**, **changes a counter**, or **decides the contract path or a payout**
(statuses, booleans, scores, levels, severities, penalties), leader and validator must be **exactly equal**.
A difference of any size and in any direction - including "the validator was one step stricter or more
lenient" - is a disagreement. Only free-text explanations (`reason`) are compared loosely.

In Litmus the model produces exactly one structured answer per call:

```json
{"verdict": "BREACH | COMPLIANT | INCONCLUSIVE", "factors": [true, false], "reason": "free text"}
```

| Field | Role | Comparison |
|---|---|---|
| `verdict` | decides probe pass/fail, case path, refunds, strikes | `==` |
| `factors[i]` | one boolean per aggravating factor; severity = 1 + number of true flags; severity decides the penalty | list `==` (position by position) |
| `reason` | explanation shown to humans | non-empty, <= 400 bytes, nothing else |

Derived values (severity, penalty, strikes, expulsion, clarity, slashing, suspension) are computed by the
contract from the agreed `verdict` and `factors` only, so they are equal on every node by construction.

## How the contract enforces it

* `judge()` runs `gl.vm.run_nondet_unsafe(leader, validator)` with an **explicit** validator. No
  `gl.eq_principle` helper is used: those are lenient or model-based.
* The validator re-runs the whole assessment **independently**. The leader's answer is never put in the
  validator's prompt, so a validator cannot be anchored into agreement.
* The validator returns `proposed["verdict"] == mine["verdict"] and proposed["factors"] == mine["factors"]`.
  Nothing else participates.
* **No repair of leader output.** `is_canonical()` rejects any leader answer that is not already canonical:
  wrong key set, lower-case or unknown verdict, string booleans, integers as booleans, a wrong number of
  flags, flags set on a non-BREACH verdict, empty or oversized reason.
* **Normalisation of the validator's own model output** (parsing JSON, `"true"` -> `true`, upper-casing the
  verdict, forcing flags to false when the verdict is not BREACH) happens *before* comparison and on the
  *stored* form. It is input parsing, not tolerance: the stored value is what is compared, and two different
  stored values can never be accepted together.
* **Agreeing to fail.** If the leader reports `[LLM_ERROR]` (no usable model answer), a validator agrees only
  if its own independent attempt fails the same way. Nothing is stored in that case.
* `reason` is trimmed to 400 bytes, defaulted if missing, and never read by any decision.
* State is written only **after** the model call succeeded, so a failed call leaves storage untouched.

## How the law is locked in

| Layer | What it proves |
|---|---|
| `tests/direct/test_equality_law.py` | exhaustive matrix: for 0, 1, 2, 3 aggravating factors and each of PROBE / CASE / APPEAL, every ordered pair of states (BREACH with every flag combination, COMPLIANT, INCONCLUSIVE) is accepted **iff** the two states are identical. Plus named cases for one-step-stricter, one-step-lenient, same severity with different flags, adjacent verdicts, 22 non-canonical leader payloads, reason handling, leader/validator failures. |
| `tests/direct/test_mutations.py` | 26 mutants of the real source (one-step tolerance, verdict-only, flag-count-only, accept-stricter, accept-more-lenient, adjacent verdicts equal, skipped canonical check, repaired leader output, always-true validator, accept-any-leader-error, plus accounting and funding faults) must all be **killed**. |
| `tests/test_static_guards.py` | AST: validator has only `==` comparisons on the decision fields; no `abs`, `sum`, `min`, `max`, `len`, ordering, `in`, arithmetic; no `eq_principle`; exactly one model call and one `run_nondet_unsafe`; no storage or clock in the nondeterministic block; state mutated only after `judge()`. |
| `tests/direct/test_vm_compat.py` | closures pickle (as they must when shipped to validators) and capture plain data only. |

## What exact equality costs, and how Litmus copes

Strict agreement means two validators running different models will sometimes disagree on a subtle
statement. Litmus handles this without loosening the law:

* the prompt is a numbered **decision procedure** (scope, conflict, verdict, factors) that only allows facts
  stated explicitly in the data, treats bare denials as non-facts, and keeps the answer a closed form;
* aggravating factors are **per-rule, concrete, explicitly-stated circumstances**, not a "severity 1-5" level
  judged by feel; a flag is true only when the text states it;
* the **clarity gate** measures exactly this: a rule whose golden probes the validators cannot reproduce
  never reaches certification;
* failed calls change nothing and can be retried; a case that never reaches a quorum is freed by the
  deterministic `void_case` after seven days.
