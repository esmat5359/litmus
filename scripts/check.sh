#!/usr/bin/env bash
# One command for every offline quality gate: lint, static guards, Direct Mode suite, dry-run of the live script.
set -euo pipefail
cd "$(dirname "$0")/.."

python scripts/setup_genvm_toolchain.py > /tmp/litmus_toolchain.txt
export GENVMROOT="$PWD/.genvmroot"

echo "== py_compile";            python -m py_compile contracts/Litmus.py
echo "== genvm-lint";            genvm-lint check contracts/Litmus.py
echo "== static guards";         python -m pytest tests/test_static_guards.py -q
echo "== Direct Mode suite";     python -m pytest tests/direct -q
if command -v node >/dev/null 2>&1; then
  echo "== live script (dry run, no network)"
  node scripts/live_e2e.mjs --dry-run --out-dir=reports
else
  echo "== node not found: skipping the dry run"
fi
echo "ALL OFFLINE GATES PASSED"
