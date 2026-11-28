#!/usr/bin/env python3
"""Pre-download the GenVM release that matches Studio v0.123 so `genvm-lint` and Direct Mode use the real v0.4 SDK.

Studio v0.123 (Consensus v0.6) pins GenVM release v0.6.0-rc14, which ships the v0.4 executor line and the
`py-genlayer:kzybg95x1gn21dw72m7hyg0maaap04n4nx55j9yt6fkdqxtz6bz0` runner named in contracts/Litmus.py.
genvm-linter 0.11.1rc2 and genlayer-test 0.30.0 both resolve the SDK from the GENVM_VERSION environment variable; without it
the linter asks the GitHub API for the newest release (and falls back to an older one when rate-limited), which is neither
reproducible nor the runner the contract pins.

    python scripts/setup_genvm_toolchain.py          # downloads once, prints: export GENVM_VERSION=<tag>
    GENVM_VERSION=v0.6.0-rc17 python scripts/setup_genvm_toolchain.py   # try another release

The v0.2 recipe (copying the std runner into .genvmroot for genvm-linter 0.11.0) is gone: that linter cannot load the v0.4 runner.
"""
import os
import shutil
import subprocess
import sys

GENVM_VERSION = os.environ.get("GENVM_VERSION", "v0.6.0-rc14")


def main() -> int:
    linter = shutil.which("genvm-lint")
    if linter is None:
        print("genvm-lint not found: run `python -m pip install -r requirements-dev.txt`", file=sys.stderr)
        return 1
    result = subprocess.run([linter, "download", "--version", GENVM_VERSION])
    if result.returncode != 0:
        print(f"could not download GenVM {GENVM_VERSION} (network, or the release does not exist)", file=sys.stderr)
        return result.returncode
    print(f"# GenVM {GENVM_VERSION} downloaded (cached by genvm-linter)")
    print(f"export GENVM_VERSION={GENVM_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
