#!/usr/bin/env python3
"""Prepare the pinned GenVM v0.2.16 runner so `genvm-lint` and Direct Mode work reproducibly.

The pinned genvm-linter looks for the std runner under $GENVMROOT.  This script downloads the exact
v0.2.16 artifact through genlayer-test's own loader and places the runner where the linter expects it.

    python scripts/setup_genvm_toolchain.py          # prints: export GENVMROOT=<path>
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENVM_VERSION = "v0.2.16"


def main() -> int:
    from gltest.direct.sdk_loader import setup_sdk_paths

    paths = setup_sdk_paths(ROOT / "contracts" / "Litmus.py", GENVM_VERSION)
    std = next((Path(p) for p in paths if "py-lib-genlayer-std" in str(p)), None)
    if std is None:
        print("could not locate py-lib-genlayer-std in the downloaded GenVM artifact", file=sys.stderr)
        return 1
    target = ROOT / ".genvmroot" / "runners" / "py-lib-genlayer-std" / "src"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(std, target, dirs_exist_ok=True)
    print(f"# GenVM {GENVM_VERSION} std runner prepared at {target}")
    print(f"export GENVMROOT={ROOT / '.genvmroot'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
