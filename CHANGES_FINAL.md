# Changes on top of litmus__1_ (deposit/ledger funding model)

The contract (`contracts/Litmus.py`) is unchanged. Only the live-run tooling was hardened.

1. **Audit tolerates debit lag.** `withdraw()` lowers escrow at once, but the network may debit the contract later.
   `audit()` now waits (up to 90 s after a withdrawal) and accepts a surplus balance only up to the value already
   withdrawn (WARN). A surplus beyond that, or a balance below escrow, is still a hard FAIL.
2. **Wind-down no longer asserts instantly.** "Contract balance is zero" waits up to 120 s (WARN if late) and is
   re-checked after every withdrawal has finalized (FAIL if value remains with escrow = 0).
3. **Rejections must be proven.** An `expectFail` step passes only when a receipt with a hash shows `exec=false`.
   A send that never reached the chain is retried (3 attempts) and otherwise reported as WARN, never a silent PASS.
4. **Transient RPC errors are retried** (rate limit, gateway HTML, dropped socket) for reads, balances and child lookups.
5. **Child-transaction lookup is optional.** If this genlayer-js build lacks `getTriggeredTransactionIds`, the
   transfer inspection is skipped with an INFO line instead of failing.
6. **Mock chain** now implements `revise_rule` (it was missing, which made the "revise on a certified rule is rejected"
   dry-run check pass vacuously).

Verification done offline: dry-run 214 pass / 0 fail / 0 warn; delayed-debit simulation passes with one WARN and a
final PASS; injected stranded value (+7 wei) is still caught as FAIL; AST static guards 11/11.
Not run offline (needs network/deps): `scripts/check.sh` (genvm-lint, Direct Mode pytest suite) and the real Studionet run.
