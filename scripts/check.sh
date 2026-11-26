#!/usr/bin/env bash
# One command for every offline quality gate: lint, static guards, Direct Mode suite, dry-run of the live script.
set -euo pipefail
cd "$(dirname "$0")/.."

# Throw-away dry-run reports never go into the repository tree.
TMP_REPORTS="$(mktemp -d)"
trap 'rm -rf "$TMP_REPORTS"' EXIT

python scripts/setup_genvm_toolchain.py > /dev/null
export GENVMROOT="$PWD/.genvmroot"

echo "== py_compile";            python -m py_compile contracts/Litmus.py
echo "== genvm-lint";            genvm-lint check contracts/Litmus.py
echo "== static guards";         python -m pytest tests/test_static_guards.py -q
echo "== Direct Mode suite";     python -m pytest tests/direct -q
if command -v node >/dev/null 2>&1; then
  echo "== live script (dry run, no network)"
  node scripts/live_e2e.mjs --dry-run --out-dir="$TMP_REPORTS"
  echo "== live script (dry run, Studionet-like: rejected payable calls keep their value)"
  node scripts/live_e2e.mjs --dry-run --retain-rejected --out-dir="$TMP_REPORTS"
  echo "== live script (dry run, injected transport faults: rate limit + lost receipt)"
  node scripts/live_e2e.mjs --dry-run --retain-rejected --flaky-rpc --out-dir="$TMP_REPORTS"
else
  echo "== node not found: skipping the dry run"
fi
echo "ALL OFFLINE GATES PASSED"
