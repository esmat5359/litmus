"""Mutation testing: deliberately break the contract and prove the suite notices.

Each mutant is the real contract source with ONE surgical change.  A mutant is "killed" when the
check functions below fail on it.  The unmodified contract must pass every check (control test).
The first group are violations of the EXACT-EQUALITY LAW (tolerance, one-step, adjacent levels).
"""
from pathlib import Path

import pytest

from conftest import CONTRACT, DEPOSIT, P_BREACH_BOTH, R, S_BOTH, S_FREE, S_PLAIN, UNIT, build_world, llm
from test_equality_law import build, payload, probe_pairs


# ----------------------------------------------------------------------------- checks
def check_equality_law(w):
    rid, n = build(w, 2)
    llm(w.vm, *probe_pairs(2))
    for i in range(4):
        w.call(0, "run_probe", rid, i)
    w.call(0, "certify_rule", rid)
    for who in (1, 2):
        w.enlist(who, rid)
    cid = w.file(1, 2, S_PLAIN, rid)
    w.call(2, "submit_defense", cid, "I deny it but give no concrete facts here.")
    llm(w.vm, (r"LITMUS", R("BREACH", True, False)))
    w.call(3, "rule_case", cid)

    def run(leader, own):
        llm(w.vm, (r"LITMUS", own))
        return w.vm.run_validator(leader_result=leader)

    B = lambda *f: R("BREACH", *f)
    C = R("COMPLIANT", False, False)
    I = R("INCONCLUSIVE", False, False)
    assert run(B(True, False), B(True, False)) is True
    assert run(B(True, False), dict(B(True, False), reason="other words")) is True
    assert run(B(True, False), B(True, True)) is False            # one step stricter
    assert run(B(True, True), B(True, False)) is False            # one step more lenient
    assert run(B(False, True), B(True, False)) is False           # same severity, other flags
    assert run(B(False, False), C) is False and run(C, I) is False and run(I, B(False, False)) is False
    assert run(dict(B(True, False), verdict="breach"), B(True, False)) is False
    assert run(dict(B(True, False), factors=["true", False]), B(True, False)) is False
    assert run(R("COMPLIANT", True, False), C) is False           # flags on a non-BREACH proposal
    assert run(dict(B(True, False), extra=1), B(True, False)) is False
    llm(w.vm, (r"LITMUS", R("BREACH", True, False)))
    assert w.vm.run_validator(leader_error=Exception("[LLM_ERROR] malformed ruling")) is False   # leader lied about failing


def check_behaviour(w):
    rid = w.certified_rule()
    for who in (1, 2, 3):
        w.enlist(who, rid)
    # BREACH severity 3, waive, settle, exact payout
    a = w.file(1, 2, S_BOTH)
    w.at(130)
    llm(w.vm, (r"(?s)MODE: CASE.*tagged forty", R("BREACH", True, True)),
        (r"(?s)MODE: CASE.*free community map", R("COMPLIANT", False, False)))
    w.call(3, "rule_case", a)
    w.call(2, "waive_appeal", a)
    w.call(3, "settle_case", a)
    assert w.case(a)["penalty"] == str(3 * UNIT)
    assert w.member(1, 2)["stake"] == str(9000 - 3 * UNIT) and w.member(1, 2)["strikes"] == 1
    assert w.claimable(1) == DEPOSIT + 3 * UNIT
    # COMPLIANT deposit goes to the defendant
    b = w.file(1, 2, S_FREE)
    w.at(300)
    w.call(3, "rule_case", b)
    w.call(1, "waive_appeal", b)
    w.call(3, "settle_case", b)
    assert w.claimable(2) == DEPOSIT and w.claimable(1) == DEPOSIT + 3 * UNIT
    # cannot leave with an open case
    c = w.file(1, 2, S_FREE + " Third.")
    with w.vm.expect_revert("open cases pending"):
        w.call(2, "leave", 1)
    w.at(600)
    llm(w.vm, (r"(?s)MODE: CASE", R("BREACH", False, False)))
    w.call(3, "rule_case", c)
    # favourable appeal slashes the bond
    llm(w.vm, (r"(?s)MODE: APPEAL", R("COMPLIANT", False, False)))
    w.appeal(2, c, "The page linked here was free of charge.")
    assert w.rule()["bond_remaining"] == str(6000 - UNIT) and w.rule()["reversals"] == 1
    # withdraw pays once
    w.call(1, "withdraw")
    with w.vm.expect_revert("nothing to withdraw"):
        w.call(1, "withdraw")
    w.audit()
    # probes: a wrong flag must fail the probe
    rid2 = w.forge(title="Second rule here")
    llm(w.vm, (r"warned twice before tagged all members", R("BREACH", True, False)))
    w.call(0, "run_probe", rid2, 1)
    assert w.rule(rid2)["probes"][1]["result"] == "FAIL"


def check_void(w):
    rid = w.certified_rule()
    for who in (1, 2):
        w.enlist(who, rid)
    cid = w.file(1, 2, S_PLAIN)
    due = w.case(cid)["defense_due"] - 1793534400
    with w.vm.expect_revert("not yet voidable"):
        w.call(3, "void_case", cid)
    w.at(due + 7 * 24 * 3600 + 1)
    w.call(3, "void_case", cid)
    assert w.claimable(1) == DEPOSIT and w.member(rid, 2)["locked"] == "0"
    assert w.rule(rid)["voided"] == 1 and w.rule(rid)["open_cases"] == 0
    w.audit()


def check_funding(w):
    """Every action must spend ledger balance exactly once; deposits must be credited; nothing is free."""
    rid = w.certified_rule()                         # forge: bond debited
    assert w.claimable(0) == 0
    w.fund(1, STAKE_)
    w.call(1, "enlist", rid)
    assert w.claimable(1) == 0 and w.member(rid, 1)["stake"] == str(STAKE_)
    w.fund(2, STAKE_)
    w.call(2, "enlist", rid)
    w.fund(1, DEPOSIT)
    w.call(1, "file_case", rid, w.w(2), S_PLAIN)
    assert w.claimable(1) == 0
    with w.vm.expect_revert("insufficient ledger balance"):
        w.call(1, "file_case", rid, w.w(2), S_PLAIN + " Second time.")
    w.audit()


STAKE_ = 9000


# ---------------------------------------------------------------------------- mutants
VALIDATOR_TAIL = '''        return (proposed["verdict"] == mine["verdict"]
                and proposed["factors"] == mine["factors"])'''

MUTANTS = {
    # --- violations of the exact-equality law -------------------------------------------
    "one_step_tolerance": (VALIDATOR_TAIL,
        '''        return (proposed["verdict"] == mine["verdict"]
                and abs(sum(proposed["factors"]) - sum(mine["factors"])) <= 1)''', check_equality_law),
    "ignore_factors": (VALIDATOR_TAIL, '''        return proposed["verdict"] == mine["verdict"]''', check_equality_law),
    "compare_flag_count_only": (VALIDATOR_TAIL,
        '''        return (proposed["verdict"] == mine["verdict"]
                and sum(proposed["factors"]) == sum(mine["factors"]))''', check_equality_law),
    "accept_stricter_validator": (VALIDATOR_TAIL,
        '''        return (proposed["verdict"] == mine["verdict"]
                and all(p <= m for p, m in zip(proposed["factors"], mine["factors"])))''', check_equality_law),
    "accept_more_lenient_validator": (VALIDATOR_TAIL,
        '''        return (proposed["verdict"] == mine["verdict"]
                and all(p >= m for p, m in zip(proposed["factors"], mine["factors"])))''', check_equality_law),
    "adjacent_verdicts_equal": (VALIDATOR_TAIL,
        '''        near = {"INCONCLUSIVE": "COMPLIANT"}
        return (near.get(proposed["verdict"], proposed["verdict"]) == near.get(mine["verdict"], mine["verdict"])
                and proposed["factors"] == mine["factors"])''', check_equality_law),
    "skip_canonical_check": ("        if not is_canonical(proposed, n_factors, allowed):\n            return False\n",
        "", check_equality_law),
    "repair_leader_output": ("        proposed = leader_result.calldata\n        if not is_canonical(proposed, n_factors, allowed):\n            return False\n",
        "        proposed = normalize_ruling(leader_result.calldata, n_factors, allowed)\n        if proposed is None:\n            return False\n",
        check_equality_law),
    "accept_any_leader_error": ("            if not str(leader_result.message).startswith(\"[LLM_ERROR]\"):\n                return False\n            try:\n                assess(prompt, n_factors, allowed)\n            except gl.vm.UserError:\n                return True\n            except Exception:\n                return False\n            return False\n",
        "            return True\n", check_equality_law),
    "validator_always_true": ("        if not isinstance(leader_result, gl.vm.Return):\n            return False\n",
        "        return True\n", check_equality_law),
    # --- logic and accounting faults ---------------------------------------------------
    "severity_off_by_one": ('severity = (1 + sum(1 for f in factors if f)) if verdict == "BREACH" else 0\n        penalty = 0',
        'severity = (2 + sum(1 for f in factors if f)) if verdict == "BREACH" else 0\n        penalty = 0', check_behaviour),
    "withdraw_keeps_ledger": ("        self.claimable[who] = u256(0)\n        self.claimable_total", "        self.claimable_total", check_behaviour),
    "penalty_not_deducted": ('            dm["stake"] = str(int(dm["stake"]) - penalty)\n', "", check_behaviour),
    "compliant_pays_plaintiff": ("            self._credit(defendant, deposit)\n            rule[\"dismissed\"]",
        "            self._credit(plaintiff, deposit)\n            rule[\"dismissed\"]", check_behaviour),
    "probe_ignores_flags": ('passed = ruling["verdict"] == probe["expected"] and ruling["factors"] == probe["factors"]',
        'passed = ruling["verdict"] == probe["expected"]', check_behaviour),
    "void_any_time": ('        self._require(now > case["defense_due"] + VOID_AFTER_SECONDS, "case is not yet voidable")\n', "", check_void),
    "void_without_refund": ('        self._credit(case["plaintiff"], int(case["deposit"]))\n        case["final_verdict"] = "VOID"', '        case["final_verdict"] = "VOID"', check_void),
    "void_keeps_reservation": ('        dm["locked"] = str(int(dm["locked"]) - int(case["reserve"]))\n        pm["open_cases"] -= 1\n        dm["open_cases"] -= 1\n        rule["open_cases"] -= 1\n        rule["voided"] += 1', '        pm["open_cases"] -= 1\n        dm["open_cases"] -= 1\n        rule["open_cases"] -= 1\n        rule["voided"] += 1', check_void),
    "forge_bond_not_debited": ("        self._debit(author, bond)\n", "", check_funding),
    "enlist_stake_not_debited": ("        self._debit(wallet, stake)\n        if member is None:", "        if member is None:", check_funding),
    "file_deposit_not_debited": ("        self._debit(plaintiff, deposit)\n        cid = int(self.case_count) + 1", "        cid = int(self.case_count) + 1", check_funding),
    "appeal_deposit_not_debited": ("        self._debit(who, deposit)         # state is only touched after the model call succeeded\n", "", check_behaviour),
    "deposit_not_credited": ("        self.escrow_total = u256(int(self.escrow_total) + value)\n        self._credit(wallet, value)\n", "        self.escrow_total = u256(int(self.escrow_total) + value)\n", check_funding),
    "debit_without_balance_check": ('        self._require(balance >= amount, "insufficient ledger balance: deposit first")\n', "", check_funding),
    "leave_with_open_cases": ('        self._require(member["open_cases"] == 0, "open cases pending")\n', "", check_behaviour),
    "no_bond_slash": ('                slash = min(unit, int(rule["bond_remaining"]))', '                slash = 0', check_behaviour),
}


def test_control_unmodified_contract_passes_every_check(direct_deploy, direct_vm, direct_accounts):
    with build_world(direct_deploy, direct_vm, direct_accounts) as w:
        check_equality_law(w)
    direct_vm.clear_mocks()


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_mutant_is_killed(name, direct_deploy, direct_vm, direct_accounts, tmp_path):
    old, new, check = MUTANTS[name]
    source = Path(CONTRACT).read_text()
    assert source.count(old) == 1, f"mutation anchor for {name} must match exactly once"
    mutant = tmp_path / f"litmus_mutant_{name}.py"
    mutant.write_text(source.replace(old, new))
    with build_world(direct_deploy, direct_vm, direct_accounts, str(mutant)) as w:
        with pytest.raises(Exception) as caught:
            check(w)
    print(f"MUTANT {name}: killed by {type(caught.value).__name__}: {str(caught.value)[:90]!r}")
    # a mutant must die because an assertion about behaviour failed (or the contract refused),
    # never because of a typo-style error such as KeyError / NameError / TypeError in the harness
    assert not isinstance(caught.value, (KeyError, NameError, TypeError, AttributeError, SyntaxError, IndexError)), \
        f"mutant {name} died of a harness error, not a behavioural difference: {caught.value!r}"


def test_control_behaviour_checks_pass_on_original(direct_deploy, direct_vm, direct_accounts):
    with build_world(direct_deploy, direct_vm, direct_accounts) as w:
        check_behaviour(w)


def test_control_funding_check_passes_on_original(direct_deploy, direct_vm, direct_accounts):
    with build_world(direct_deploy, direct_vm, direct_accounts) as w:
        check_funding(w)


def test_control_void_check_passes_on_original(direct_deploy, direct_vm, direct_accounts):
    with build_world(direct_deploy, direct_vm, direct_accounts) as w:
        check_void(w)
