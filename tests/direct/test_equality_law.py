"""THE EXACT-EQUALITY LAW.

Every value that is stored, counted, or that decides a path or a payout is derived from the
validator-agreed ``verdict`` and ``factors``.  The validator must accept a leader proposal
if and only if both are IDENTICAL to its own independent answer.  Nothing adjacent, nothing
"one step stricter", no tolerance.  Only the free-text ``reason`` is compared loosely.

These tests drive the real validator closure captured by Direct Mode with every combination.
"""
import itertools
import json

import pytest

from conftest import P_BREACH_BOTH, P_BREACH_PLAIN, P_OK_BLOG, P_OK_FREE, R, S_PLAIN, UNIT, llm

MODES = ("PROBE", "CASE", "APPEAL")


def states(n, allow_inconclusive=True):
    out = [("BREACH", flags) for flags in itertools.product([False, True], repeat=n)]
    out.append(("COMPLIANT", (False,) * n))
    if allow_inconclusive:
        out.append(("INCONCLUSIVE", (False,) * n))
    return out


def payload(state, reason="leader reasoning"):
    verdict, flags = state
    return {"verdict": verdict, "factors": list(flags), "reason": reason}


def build(world, n):
    """A rule with n aggravators; returns its id (still DRAFT)."""
    aggravators = [f"Aggravating circumstance number {i} is stated." for i in range(n)]
    probes = [
        {"scenario": P_BREACH_PLAIN, "expected": "BREACH", "factors": [False] * n},
        {"scenario": P_BREACH_BOTH, "expected": "BREACH", "factors": [True] * n},
        {"scenario": P_OK_FREE, "expected": "COMPLIANT", "factors": [False] * n},
        {"scenario": P_OK_BLOG, "expected": "COMPLIANT", "factors": [False] * n},
    ]
    return world.forge(aggravators=aggravators, probes=probes), n


def probe_pairs(n):
    return [(r"warned twice before tagged all members", R("BREACH", *([True] * n))),
            (r"paid video course in the general", R("BREACH", *([False] * n))),
            (r"free open-source tutorial", R("COMPLIANT", *([False] * n))),
            (r"free blog post", R("COMPLIANT", *([False] * n)))]


def agree(mode, leader, validator):
    if mode == "PROBE" and "INCONCLUSIVE" in (leader[0], validator[0]):
        return False
    return leader == validator


def run_matrix(world, n, mode):
    allow = mode != "PROBE"
    leader_states = states(n, True)          # an INCONCLUSIVE probe proposal must be rejected as well
    for leader, validator in itertools.product(leader_states, leader_states):
        llm(world.vm, (r"LITMUS", payload(validator, "validator reasoning written in other words")))
        got = world.vm.run_validator(leader_result=payload(leader))
        assert got is agree(mode, leader, validator), f"{mode} n={n} leader={leader} validator={validator} -> {got}"
    return allow


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_exact_equality_matrix_all_modes(world, n):
    rid, n = build(world, n)
    llm(world.vm, *probe_pairs(n))
    world.call(0, "run_probe", rid, 0)
    run_matrix(world, n, "PROBE")
    llm(world.vm, *probe_pairs(n))
    for i in (1, 2, 3):
        world.call(0, "run_probe", rid, i)
    world.call(0, "certify_rule", rid)
    for who in (1, 2):
        world.enlist(who, rid)

    cid = world.file(1, 2, S_PLAIN, rid)
    world.call(2, "submit_defense", cid, "I deny it but give no concrete facts here.")
    llm(world.vm, (r"LITMUS", R("BREACH", *([False] * n))))
    world.call(3, "rule_case", cid)
    run_matrix(world, n, "CASE")

    llm(world.vm, (r"LITMUS", R("BREACH", *([False] * n))))
    world.appeal(2, cid, "The statement does not prove the breach.")
    run_matrix(world, n, "APPEAL")


@pytest.fixture
def case_validator(world):
    """A rule_case call whose captured validator the tests below hammer with odd payloads."""
    rid, n = build(world, 2)
    llm(world.vm, *probe_pairs(2))
    for i in range(4):
        world.call(0, "run_probe", rid, i)
    world.call(0, "certify_rule", rid)
    world.enlist(1, rid)
    world.enlist(2, rid)
    cid = world.file(1, 2, S_PLAIN, rid)
    world.call(2, "submit_defense", cid, "I deny it but give no concrete facts here.")
    llm(world.vm, (r"LITMUS", R("BREACH", True, False)))
    world.call(3, "rule_case", cid)
    return world


def verdict_of(world, payload_, own=R("BREACH", True, False)):
    llm(world.vm, (r"LITMUS", own))
    return world.vm.run_validator(leader_result=payload_)


def test_reason_is_free_text_everything_else_is_not(case_validator):
    w = case_validator
    base = R("BREACH", True, False)
    assert verdict_of(w, dict(base, reason="completely different words")) is True
    assert verdict_of(w, dict(base, reason="x")) is True
    assert verdict_of(w, dict(base, reason="é" * 200)) is True               # exactly 400 bytes
    assert verdict_of(w, dict(base, reason="")) is False
    assert verdict_of(w, dict(base, reason="   ")) is False
    assert verdict_of(w, dict(base, reason="a" * 401)) is False
    assert verdict_of(w, dict(base, reason=None)) is False
    assert verdict_of(w, dict(base, reason=7)) is False
    # the validator's own reason may be missing or odd: it is free text, not decision data
    assert verdict_of(w, base, own={"verdict": "BREACH", "factors": [True, False]}) is True
    assert verdict_of(w, base, own={"verdict": "BREACH", "factors": [True, False], "reason": 123}) is True


def test_validator_parses_its_own_messy_model_output_but_compares_exactly(case_validator):
    w = case_validator
    base = R("BREACH", True, False)
    assert verdict_of(w, base, own={"verdict": " breach ", "factors": ["TRUE", "false"], "reason": "ok"}) is True
    assert verdict_of(w, base, own=json.dumps(R("BREACH", True, False))) is True
    assert verdict_of(w, base, own={"verdict": "BREACH", "factors": ["TRUE", "true"], "reason": "ok"}) is False
    assert verdict_of(w, base, own={"verdict": "COMPLIANT", "factors": [True, False], "reason": "ok"}) is False


@pytest.mark.parametrize("bad", [
    None, "BREACH", 5, [], ["BREACH", [True, False]],
    {}, {"verdict": "BREACH", "factors": [True, False]},                              # reason missing
    {"verdict": "BREACH", "reason": "no factors"},                                    # factors missing
    {"factors": [True, False], "reason": "no verdict"},
    {"verdict": "BREACH", "factors": [True, False], "reason": "ok", "extra": 1},      # extra key
    {"verdict": "breach", "factors": [True, False], "reason": "ok"},                  # lower case
    {"verdict": " BREACH", "factors": [True, False], "reason": "ok"},
    {"verdict": "GUILTY", "factors": [True, False], "reason": "ok"},
    {"verdict": None, "factors": [True, False], "reason": "ok"},
    {"verdict": "BREACH", "factors": ["true", "false"], "reason": "ok"},              # string booleans
    {"verdict": "BREACH", "factors": [1, 0], "reason": "ok"},                          # ints are not booleans
    {"verdict": "BREACH", "factors": [True], "reason": "ok"},                         # too short
    {"verdict": "BREACH", "factors": [True, False, False], "reason": "ok"},           # too long
    {"verdict": "BREACH", "factors": None, "reason": "ok"},
    {"verdict": "BREACH", "factors": "TF", "reason": "ok"},
    {"verdict": "COMPLIANT", "factors": [True, False], "reason": "ok"},               # flags on non-BREACH
    {"verdict": "INCONCLUSIVE", "factors": [False, True], "reason": "ok"},
])
def test_non_canonical_leader_proposals_are_always_rejected(case_validator, bad):
    assert verdict_of(case_validator, bad) is False


def test_leader_failure_and_validator_failure_are_disagreements(case_validator):
    w = case_validator
    llm(w.vm, (r"LITMUS", R("BREACH", True, False)))
    assert w.vm.run_validator(leader_error=Exception("leader crashed")) is False
    for broken in ("garbage", {"verdict": "BREACH"}, R("NOPE", True, False), R("BREACH", True)):
        assert verdict_of(w, R("BREACH", True, False), own=broken) is False
    w.vm.clear_mocks()
    assert w.vm.run_validator(leader_result=R("BREACH", True, False)) is False   # no model available at all


@pytest.mark.parametrize("leader,validator", [
    (("BREACH", (True, False)), ("BREACH", (True, True))),        # validator one step stricter
    (("BREACH", (True, True)), ("BREACH", (True, False))),        # validator one step more lenient
    (("BREACH", (False, False)), ("BREACH", (True, False))),
    (("BREACH", (False, False)), ("COMPLIANT", (False, False))),  # adjacent verdicts
    (("COMPLIANT", (False, False)), ("INCONCLUSIVE", (False, False))),
    (("INCONCLUSIVE", (False, False)), ("BREACH", (False, False))),
    (("BREACH", (False, True)), ("BREACH", (True, False))),       # same severity, different flags
])
def test_adjacent_or_one_step_differences_are_rejected(case_validator, leader, validator):
    assert verdict_of(case_validator, payload(leader), own=payload(validator)) is False


def test_same_severity_but_different_flags_is_not_equal(case_validator):
    """Severity is derived from the flags, but equality is on the flags themselves."""
    assert verdict_of(case_validator, payload(("BREACH", (True, False))), own=payload(("BREACH", (False, True)))) is False


def test_leader_decision_is_what_gets_stored(world):
    rid, n = build(world, 2)
    llm(world.vm, *probe_pairs(2))
    for i in range(4):
        world.call(0, "run_probe", rid, i)
    world.call(0, "certify_rule", rid)
    for who in (1, 2):
        world.enlist(who, rid)
    cid = world.file(1, 2, S_PLAIN, rid)
    world.call(2, "submit_defense", cid, "I deny it but give no concrete facts here.")
    llm(world.vm, (r"LITMUS", {"verdict": "breach", "factors": ["true", False], "reason": "r" * 900}))
    world.call(3, "rule_case", cid)
    case = world.case(cid)
    assert (case["verdict"], case["factors"], case["severity"]) == ("BREACH", [True, False], 2)
    assert len(case["reason"].encode()) <= 400
    world.at(10_000)
    world.call(3, "settle_case", cid)
    assert world.case(cid)["penalty"] == str(2 * UNIT)


def test_agreeing_to_fail_requires_an_independent_identical_failure(case_validator):
    """A leader error is only accepted when the validator also finds no usable answer; never otherwise."""
    w = case_validator
    llm(w.vm, (r"LITMUS", "garbage"))                                      # validator cannot decide either
    assert w.vm.run_validator(leader_error=Exception("[LLM_ERROR] malformed ruling")) is True
    assert w.vm.run_validator(leader_error=Exception("something else went wrong")) is False
    assert w.vm.run_validator(leader_error=Exception("")) is False
    llm(w.vm, (r"LITMUS", R("BREACH", True, False)))                       # validator CAN decide: leader lied
    assert w.vm.run_validator(leader_error=Exception("[LLM_ERROR] malformed ruling")) is False
