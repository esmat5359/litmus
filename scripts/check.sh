#!/usr/bin/env bash
# One command for every offline quality gate: lint, static guards, Direct Mode suite, dry-run of the live script.
set -euo pipefail
cd "$(dirname "$0")/.."

# Studio v0.123 pins GenVM v0.6.0-rc14 (executor line v0.4, runner py-genlayer:kzybg95x...). Override with GENVM_VERSION=...
export GENVM_VERSION="${GENVM_VERSION:-v0.6.0-rc14}"
python scripts/setup_genvm_toolchain.py > /tmp/litmus_toolchain.txt

echo "== py_compile";            python -m py_compile contracts/Litmus.py
echo "== genvm-lint";            genvm-lint check contracts/Litmus.py
echo "== ABI / schema";         SCHEMA_DIR="$(mktemp -d)"; genvm-lint schema contracts/Litmus.py --output "$SCHEMA_DIR/Litmus.schema.json" && test -s "$SCHEMA_DIR/Litmus.schema.json"
echo "== static guards";         python -m pytest tests/test_static_guards.py -q
echo "== Direct Mode suite";     python -m pytest tests/direct -q
if command -v node >/dev/null 2>&1; then
  echo "== live script (dry run, no network)"
  node scripts/live_e2e.mjs --dry-run --out-dir=reports
  TMP_REPORTS="$(mktemp -d)"
  echo "== live script (dry run, Studionet-like: rejected payable calls keep their value)"
  node scripts/live_e2e.mjs --dry-run --retain-rejected --out-dir="$TMP_REPORTS"
  echo "== live script (dry run, injected transport faults: rate limit + lost receipt)"
  node scripts/live_e2e.mjs --dry-run --retain-rejected --flaky-rpc --out-dir="$TMP_REPORTS"
  echo "== live script (dry run, Studionet 2026-11-26: rejected value returned after finalization, transfers applied late)"
  node scripts/live_e2e.mjs --dry-run --refund-rejected-late --late-transfers --out-dir="$TMP_REPORTS"
  echo "== live script (negative test: a finalized withdraw whose transfer is never applied must FAIL the run)"
  if node scripts/live_e2e.mjs --dry-run --lost-transfers --out-dir="$TMP_REPORTS" > /dev/null 2>&1; then
    echo "ERROR: the live script accepted a payout that never arrived" >&2; exit 1
  fi
else
  echo "== node not found: skipping the dry run"
fi
echo "ALL OFFLINE GATES PASSED"
