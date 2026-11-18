# LITMUS

**Rules that must pass their own test before they are allowed to rule.**

Litmus is a GenLayer Intelligent Contract (GenVM **v0.2.16**, built for **Studionet**) for communities that
enforce written rules with money at stake. Most on-chain "AI court" contracts let any text become a rule and
then trust the model to interpret it. Litmus turns that around:

1. A **rule author** writes the rule, up to three *aggravating factors*, and 4-6 **golden probes**: short
   scenarios with the verdict (and factor flags) the author expects.
2. The validator network has to **reproduce every golden probe under consensus**. The share of probes it
   gets exactly right is the rule's **clarity score**. Below the author's threshold (75-100 %) the rule can
   not be certified. Vague rules die at the gate, before anybody's money is bound by them.
3. Only a **certified rule** can enlist members (who post a stake), accept cases (plaintiff posts a deposit)
   and punish breaches. Every ruling is appealable once, with a fresh-eyes re-judgement.
4. Rulings feed back into the rule: an appeal that **overturns** a ruling slashes the author's bond and two
   overturns **suspend** the rule. Authors are paid in reputation and bond for writing rules the validators
   read the same way twice.

It is deliberately **not** another dispute-resolution toy: the model never decides money directly. It only
fills in a closed, tiny form (`verdict` plus one boolean per aggravating factor); everything else - severity,
penalty, strikes, expulsion, slashing, refunds - is deterministic contract arithmetic on that form.

## The exact-equality law

> Every value that is stored, changes a counter, or decides the contract path or a payout (statuses,
> booleans, scores, levels, severities, penalties) must be **identical** between leader and validator.
> No tolerance. No adjacent level. No "one step stricter". Only the free-text `reason` is compared loosely.

How the code enforces it (details in [docs/CONSENSUS.md](docs/CONSENSUS.md)):

* the validator accepts a leader proposal only if `verdict ==` and `factors ==` hold against its own
  independent assessment (it never sees the leader's answer, so it cannot be anchored by it);
* a leader answer must already be in canonical form (exact key set, upper-case enum, real booleans, right
  number of flags, flags only on BREACH); a validator never repairs it;
* `reason` is bounded free text and never decides anything;
* the law is locked by an **exhaustive equality matrix**, **20 mutation tests** (tolerances, one-step
  acceptance, adjacent verdicts, skipped checks ...) and **AST guards** that fail if the validator ever uses
  `abs`, ordering comparisons, counting or arithmetic.

## Lifecycle

```
 forge_rule (bond) -> DRAFT --run_probe x N--> certify_rule --> CERTIFIED --retire_rule--> RETIRED
                        |  (clarity >= pass %)                     |  2 overturned appeals
                        +--revise_rule (max 2)                      +--------------------> SUSPENDED

 enlist (stake) -> ACTIVE --3 strikes (configurable)--> EXPELLED          leave -> stake to ledger
 file_case (deposit) -> OPEN --rule_case--> RULED --appeal_case--> SETTLED
                          |                    +--waive_appeal / window ends --settle_case--> SETTLED
                          +--7 days without a quorum ruling--void_case--> SETTLED (VOID, deposit refunded)
 every payout lands on a pull-ledger; withdraw() sends it
```

| Verdict (final) | Plaintiff | Defendant | Counters |
|---|---|---|---|
| BREACH, severity s = 1 + true flags | deposit back + `s x penalty_unit` | stake -= penalty, +1 strike | `breaches` |
| COMPLIANT | filing deposit forfeited | receives the forfeited deposit | `dismissed` |
| INCONCLUSIVE | deposit back | nothing | `inconclusive` |
| VOID (no quorum for 7 days) | deposit back | reservation released | `voided` |
| Appeal, favourable to appellant | appeal deposit back + `penalty_unit` slashed from the author bond | | rule `reversals` +1 |
| Appeal, not favourable | appeal deposit goes to the other party | | |

Each open case reserves the maximum possible penalty from the defendant's stake, so a penalty is always
fully collectable and never partial.

## Contract API

| Write | Who | Notes |
|---|---|---|
| `forge_rule(title, text, aggravators_json, probes_json, penalty_unit, filing_deposit, enlist_stake, strike_limit, pass_percent, defense_minutes, appeal_minutes)` payable | anyone | attached value = author bond (>= 3 penalty units) |
| `revise_rule`, `run_probe`, `certify_rule`, `retire_rule`, `reclaim_bond` | author | probes run once per version; max 2 revisions |
| `enlist` payable, `leave` | member | exact stake; re-enlist keeps strikes; expelled members can only leave |
| `file_case` payable, `submit_defense` | member / defendant | at most 3 open cases per member |
| `rule_case`, `settle_case`, `void_case` | anyone | permissionless once the clock allows |
| `waive_appeal`, `appeal_case` payable | the adversely affected party | appeal = fresh judgement without seeing the first ruling |
| `withdraw` | anyone owed money | pull-payment; ledger zeroed before the transfer is emitted |

Views: `get_stats`, `get_limits`, `get_rule`, `list_rules`, `get_case`, `list_cases`, `get_member`,
`get_claimable`. Amounts are returned as decimal strings, so JavaScript clients never lose wei precision.

Full state machines, record layouts and arithmetic: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt     # genlayer-test 0.29.2, genvm-linter 0.11.0, pytest
npm ci                                  # genlayer-js 1.1.8 (only needed for the live script)

bash scripts/check.sh                   # lint + static guards + Direct Mode suite + dry run
```

`scripts/check.sh` downloads the exact GenVM **v0.2.16** artifact through `genlayer-test`'s own loader and
points `GENVMROOT` at it, so the linter and Direct Mode use the real v0.2.16 SDK, not a mock.

### Live end-to-end run on Studionet (GitHub Codespaces friendly)

```bash
node scripts/live_e2e.mjs                 # full run, deploys a fresh contract, writes reports/*.md and *.json
node scripts/live_e2e.mjs --skip-slow     # skip the two-window timeout case (saves ~8 minutes)
node scripts/live_e2e.mjs --dry-run       # same script, no network, in-process model
```

It deploys the contract, checks the deployed bytes against the source, forges a rule, makes the validators
pass its golden probes, enlists members, runs four cases (waived, compliant, appealed, timed-out), audits
the books after every stage, withdraws everything and ends with a zero-balance contract. Every step is
verified against **contract state**, language-model steps are retried and reported honestly. See
[docs/LIVE_E2E.md](docs/LIVE_E2E.md).

## What was verified

| Gate | Result |
|---|---|
| `genvm-lint check` against the v0.2.16 runner | passes (24 methods: 8 views, 16 writes) |
| Direct Mode suite (real v0.2.16 SDK) | 139 tests: lifecycle, validation, payouts, appeals, expulsion, liveness escape, closures pickle like on the real VM |
| Exact-equality matrix | every (leader, validator) pair of every state for 0-3 factors in PROBE, CASE and APPEAL mode |
| Mutation tests | 20 deliberately broken contracts, all killed (tolerance, one-step, adjacent verdicts, accounting faults) |
| Seeded random walks | 10 walks x 600 steps, books re-audited after every successful call, every outcome reached |
| Differential replay | the live script's model and accounting oracle replayed on the real Python contract: identical final state |
| Static guards | validator uses `==` only; one model call site; no storage or time inside the nondeterministic block |

**Honest limit:** the authoring environment could not reach `studio.genlayer.com`, so the live script was
exercised against the in-process model and then replayed on the real contract, not against the public
network. Because every check reads contract state rather than trusting receipt field names, the first real
run is expected to work; if a receipt field differs, the report shows it as a warning with the raw status.

## Known limits (by design)

* **Exact equality costs liveness.** If the validators cannot agree on a vague statement the call fails and
  can be retried; a case that never reaches a quorum can be voided after 7 days. Write concrete rules and
  concrete probes: the clarity gate exists to teach exactly that.
* Native-token transfers are emitted **on finalization**; `withdraw` therefore credits the wallet after the
  transaction finalizes, not at acceptance.
* The contract trusts the model only for the closed form described above; it does not fetch web pages. The
  evidence is the text parties submit on-chain.
* One appeal per case; appeals do not chain.

## Layout

```
contracts/Litmus.py            the Intelligent Contract
tests/direct/                  Direct Mode suite (rules, cases, equality law, mutation, fuzz, replay)
tests/test_static_guards.py    AST guards for the equality law
scripts/live_e2e.mjs           live Studionet run (+ --dry-run)
scripts/lib/                   real chain adapter, in-process model, helpers
scripts/check.sh               every offline gate in one command
scripts/verify_deployed_source.mjs   byte parity of deployed code vs source
docs/                          CONSENSUS, ARCHITECTURE, LIVE_E2E
```
