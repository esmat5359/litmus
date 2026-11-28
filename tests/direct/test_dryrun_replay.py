"""Differential test: the live script's JS model vs the real Python contract.

``node scripts/live_e2e.mjs --dry-run`` records every call it makes (sender, arguments, value, clock and
the model ruling).  This test replays that transcript against contracts/Litmus.py in Direct Mode and
requires the final stats, rules, cases, members and ledgers to be IDENTICAL.  It therefore proves that
the in-process model, the accounting oracle inside the live script and the real contract agree, and it
cross-checks the rule digest between JavaScript and Python.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import R, llm

ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for the dry run")


def run_dry(tmp_path, *flags):
    transcript = tmp_path / "transcript.json"
    proc = subprocess.run(
        ["node", "scripts/live_e2e.mjs", "--dry-run", f"--transcript={transcript}", f"--out-dir={tmp_path}", *flags],
        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert "RESULT: PASSED" in proc.stdout
    return json.loads(transcript.read_text())


def convert(value, mapping):
    if isinstance(value, dict) and "$big" in value:
        return int(value["$big"])
    if isinstance(value, str):
        return mapping.get(value.lower(), value)
    return value


@pytest.mark.parametrize("flags", [(), ("--flip-appeals",), ("--skip-slow",), ("--refund-rejected-late", "--late-transfers")])
def test_js_model_and_script_match_the_python_contract(world, tmp_path, flags):
    data = run_dry(tmp_path, *flags)
    mapping = {addr.lower(): world.w(i) for i, addr in enumerate(data["accounts"])}
    senders = {addr.lower(): world.accounts[i] for i, addr in enumerate(data["accounts"])}
    reverted = succeeded = 0
    for entry in data["transcript"]:
        world.vm.warp(datetime.fromtimestamp(entry["t"], tz=timezone.utc).isoformat())
        world.vm.sender = senders[entry["from"].lower()]
        world.vm.value = int(entry["value"])
        args = [convert(a, mapping) for a in entry["args"]]
        if entry["llm"] is not None:
            llm(world.vm, (r"LITMUS", entry["llm"]))
        method = getattr(world.c, entry["fn"])
        try:
            if entry["ok"]:
                method(*args)
                succeeded += 1
                world.inflow += int(entry["value"])
            else:
                with world.vm.expect_revert():
                    method(*args)
                reverted += 1
        finally:
            world.vm.value = 0
    assert succeeded > 25 and reverted > 15, (succeeded, reverted)

    final = data["finalState"]
    text = json.dumps(final, sort_keys=True)
    for model_addr, real in mapping.items():
        text = text.replace(model_addr, real)
    expected = json.loads(text)

    assert world.stats() == expected["stats"]
    for rid, rule in expected["rules"].items():
        assert world.rule(int(rid)) == rule, f"rule {rid} differs"
    for cid, case in expected["cases"].items():
        assert world.case(int(cid)) == case, f"case {cid} differs"
    for key, member in expected["members"].items():
        rid, wallet = key.split(":")
        assert world.view("get_member", int(rid), wallet) == member, f"member {key} differs"
    for wallet, amount in expected["claimable"].items():
        assert int(world.c.get_claimable(wallet)) == int(amount), f"claimable {wallet} differs"
    world.audit()
