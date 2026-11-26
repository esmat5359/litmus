"""Rule forging, probing, certification and the members' stake lifecycle."""
import json

import pytest

from conftest import (AGGRAVATORS, BOND, DEPOSIT, P_BREACH_BOTH, P_BREACH_PLAIN, P_OK_BLOG, P_OK_FREE,
                      PROBE_PAIRS, PROBES, R, STAKE, UNIT, llm)


def test_empty_deployment(world):
    assert world.stats() == {"cases": 0, "claimable_total": "0", "escrow_total": "0", "rules": 0}
    limits = world.view("get_limits")
    assert limits["min_probes"] == 4 and limits["max_factors"] == 3


def test_forge_stores_rule_and_escrows_bond(world):
    rid = world.forge()
    rule = world.rule(rid)
    assert rid == 1 and rule["status"] == "DRAFT" and rule["version"] == 1
    assert rule["bond"] == rule["bond_remaining"] == str(BOND)
    assert rule["author"] == world.w(0)
    assert [p["result"] for p in rule["probes"]] == ["PENDING"] * 4
    assert world.audit()["escrow_total"] == str(BOND)


@pytest.mark.parametrize("kwargs,match", [
    ({"title": "ab"}, "title"),
    ({"text": "too short"}, "rule text"),
    ({"probes": PROBES[:3]}, "golden probes"),
    ({"probes": PROBES + PROBES[:3]}, "golden probes"),
    ({"probes": [PROBES[0], PROBES[1], PROBES[1] | {"scenario": "Another breach scenario text."}, PROBES[2]]},
     "at least 2 BREACH and 2 COMPLIANT"),
    ({"aggravators": ["one factor long enough"] * 2}, "distinct"),
    ({"aggravators": [f"factor number {i} text" for i in range(4)], "probes": [
        {"scenario": p["scenario"], "expected": p["expected"], "factors": [False] * 4} for p in PROBES]},
     "at most 3"),
    ({"unit": 0}, "positive"),
    ({"deposit": 0}, "positive"),
    ({"stake": 2999}, "cover the maximum penalty"),
    ({"bond": 2999}, "bond must be at least"),
    ({"strikes": 0}, "strike limit"),
    ({"strikes": 6}, "strike limit"),
    ({"pass_percent": 74}, "pass percent"),
    ({"pass_percent": 101}, "pass percent"),
    ({"defense_minutes": 1}, "defense window"),
    ({"appeal_minutes": 10081}, "appeal window"),
])
def test_forge_rejects_invalid_input(world, kwargs, match):
    with world.vm.expect_revert(match):
        world.forge(**kwargs)
    assert world.stats()["rules"] == 0


def test_probe_factor_shape_is_enforced(world):
    bad_len = [dict(PROBES[0], factors=[False])] + PROBES[1:]
    with world.vm.expect_revert("one boolean per aggravating factor"):
        world.forge(probes=bad_len)
    bad_label = [dict(PROBES[2], factors=[True, False])] + [PROBES[0], PROBES[1], PROBES[3]]
    with world.vm.expect_revert("only a BREACH probe"):
        world.forge(probes=bad_label)
    bad_type = [dict(PROBES[0], factors=["yes", "no"])] + PROBES[1:]
    with world.vm.expect_revert("one boolean per aggravating factor"):
        world.forge(probes=bad_type)
    bad_enum = [dict(PROBES[0], expected="INCONCLUSIVE")] + PROBES[1:]
    with world.vm.expect_revert("BREACH or COMPLIANT"):
        world.forge(probes=bad_enum)
    dup = [PROBES[0], dict(PROBES[1], scenario=PROBES[0]["scenario"].upper()), PROBES[2], PROBES[3]]
    with world.vm.expect_revert("distinct"):
        world.forge(probes=dup)


def test_zero_aggravator_rule_is_allowed(world):
    probes = [{"scenario": p["scenario"], "expected": p["expected"], "factors": []} for p in PROBES]
    rid = world.forge(aggravators=[], probes=probes, stake=UNIT)
    llm(world.vm,
        (r"paid video course|warned twice", R("BREACH")),
        (r"free", R("COMPLIANT")))
    for i in range(4):
        world.call(0, "run_probe", rid, i)
    world.call(0, "certify_rule", rid)
    assert world.rule(rid)["status"] == "CERTIFIED"


def test_only_author_runs_probes_and_each_probe_runs_once(world):
    rid = world.forge()
    llm(world.vm, *PROBE_PAIRS)
    world.fail(1, "run_probe", rid, 0, match="only the rule author")
    world.call(0, "run_probe", rid, 0)
    world.fail(0, "run_probe", rid, 0, match="already run")
    world.fail(0, "run_probe", rid, 4, match="out of range")
    assert world.rule(rid)["probes_run"] == 1


def test_certify_requires_every_probe_and_exact_clarity(world):
    rid = world.forge()
    llm(world.vm, *PROBE_PAIRS)
    world.fail(0, "certify_rule", rid, match="run every probe first")
    for i in range(4):
        world.call(0, "run_probe", rid, i)
    world.fail(1, "certify_rule", rid, match="only the rule author")
    world.call(0, "certify_rule", rid)
    rule = world.rule(rid)
    assert rule["status"] == "CERTIFIED" and rule["clarity"] == 100 and rule["probes_passed"] == 4
    assert len(rule["digest"]) == 64
    world.fail(0, "run_probe", rid, 0, match="only a DRAFT rule runs probes")
    world.fail(0, "certify_rule", rid, match="only a DRAFT rule can be certified")


def test_one_wrong_probe_blocks_a_100_percent_rule_but_not_a_75_percent_rule(world):
    llm(world.vm,
        (r"warned twice before tagged all members", R("BREACH", True, False)),   # one flag wrong
        *PROBE_PAIRS[1:])
    strict = world.forge(pass_percent=100)
    lenient = world.forge(pass_percent=75, title="Lenient promotion rule")
    for rid in (strict, lenient):
        for i in range(4):
            world.call(0, "run_probe", rid, i)
        assert world.rule(rid)["probes_passed"] == 3
        assert [p["result"] for p in world.rule(rid)["probes"]] == ["PASS", "FAIL", "PASS", "PASS"]
    world.fail(0, "certify_rule", strict, match="clarity below")
    world.call(0, "certify_rule", lenient)
    assert world.rule(lenient)["clarity"] == 75 and world.rule(lenient)["status"] == "CERTIFIED"


def test_probe_with_wrong_verdict_or_factor_fails_but_exact_match_passes(world):
    rid = world.forge()
    llm(world.vm,
        (r"warned twice before tagged all members", R("BREACH", True, True)),
        (r"paid video course in the general", R("BREACH", True, False)),      # expected [F,F]
        (r"free open-source tutorial", R("BREACH", False, False)),           # expected COMPLIANT
        (r"free blog post", R("COMPLIANT", False, False)))
    for i in range(4):
        world.call(0, "run_probe", rid, i)
    probes = world.rule(rid)["probes"]
    assert [p["result"] for p in probes] == ["FAIL", "PASS", "FAIL", "PASS"]
    assert probes[0]["got_factors"] == [True, False] and probes[2]["got"] == "BREACH"
    assert world.rule(rid)["probes_passed"] == 2


def test_probe_rejects_inconclusive_and_malformed_model_output(world):
    rid = world.forge()
    for bad in (R("INCONCLUSIVE", False, False), R("MAYBE", False, False), "not json at all",
                {"verdict": "BREACH", "factors": [True], "reason": "short list"},
                {"verdict": "BREACH", "factors": ["maybe", True], "reason": "bad flag"},
                {"verdict": "BREACH", "reason": "missing factors"}):
        llm(world.vm, (r"paid video course in the general", bad))
        with world.vm.expect_revert("LLM_ERROR"):
            world.call(0, "run_probe", rid, 0)
    assert world.rule(rid)["probes_run"] == 0


def test_model_output_normalisation_is_not_a_tolerance(world):
    """String booleans and lower-case enums are parsed, flags on non-BREACH are forced to false."""
    rid = world.forge()
    llm(world.vm,
        (r"paid video course in the general", {"verdict": "breach", "factors": ["false", "FALSE"], "reason": "ok"}),
        (r"free open-source tutorial", {"verdict": "COMPLIANT", "factors": [True, True], "reason": "ok"}))
    world.call(0, "run_probe", rid, 0)
    world.call(0, "run_probe", rid, 2)
    probes = world.rule(rid)["probes"]
    assert probes[0]["result"] == "PASS" and probes[0]["got_factors"] == [False, False]
    assert probes[2]["result"] == "PASS" and probes[2]["got_factors"] == [False, False]


def test_revise_resets_probes_and_is_bounded(world):
    rid = world.forge()
    llm(world.vm, *PROBE_PAIRS)
    world.call(0, "run_probe", rid, 0)
    world.fail(1, "revise_rule", rid, "Members must not sell paid things in general.", json.dumps(AGGRAVATORS),
               json.dumps(PROBES), match="only the rule author")
    for version in (2, 3):
        world.call(0, "revise_rule", rid, f"Revision {version}: members must not advertise paid products here.",
                   json.dumps(AGGRAVATORS), json.dumps(PROBES))
        rule = world.rule(rid)
        assert rule["version"] == version and rule["probes_run"] == 0
        assert [p["result"] for p in rule["probes"]] == ["PENDING"] * 4
    world.fail(0, "revise_rule", rid, "Revision four should not be accepted at all.", json.dumps(AGGRAVATORS),
               json.dumps(PROBES), match="revision limit")


def test_revise_cannot_break_stake_coverage_or_edit_certified_rules(world):
    rid = world.forge(stake=3000)
    three = [f"Aggravating circumstance number {i}." for i in range(3)]
    probes = [{"scenario": p["scenario"], "expected": p["expected"], "factors": [p["expected"] == "BREACH"] * 3}
              for p in PROBES]
    with world.vm.expect_revert("cover the maximum penalty"):
        world.call(0, "revise_rule", rid, "A longer rule text for the revision.", json.dumps(three), json.dumps(probes))
    world.certify(rid=rid)
    world.fail(0, "revise_rule", rid, "A longer rule text for the revision.", json.dumps(AGGRAVATORS),
               json.dumps(PROBES), match="only a DRAFT rule can be revised")


def test_retire_and_reclaim_bond(world):
    rid = world.certified_rule()
    world.fail(0, "reclaim_bond", rid, match="retire the rule first")
    world.fail(1, "retire_rule", rid, match="only the rule author")
    world.call(0, "retire_rule", rid)
    world.fail(0, "retire_rule", rid, match="already retired")
    world.call(0, "reclaim_bond", rid)
    assert world.claimable(0) == BOND and world.rule(rid)["bond_remaining"] == "0"
    world.fail(0, "reclaim_bond", rid, match="no bond left")
    world.audit()
    world.call(0, "withdraw")
    assert world.claimable(0) == 0
    assert world.transfers == [{"to": world.w(0), "value": BOND, "method": None, "on": "finalized"}]
    assert world.audit()["escrow_total"] == "0"


def test_enlist_debits_the_ledger_exactly_and_follows_state_rules(world):
    rid = world.forge()
    world.fund(1, STAKE)
    with world.vm.expect_revert("not certified"):
        world.call(1, "enlist", rid)
    world.certify(rid=rid)
    world.fund(2, STAKE - 1)
    world.fail(2, "enlist", rid, match="insufficient ledger balance")        # one wei short is refused
    assert world.member(rid, 2) is None and world.claimable(2) == STAKE - 1  # and nothing was taken
    world.call(1, "enlist", rid)                                              # funded earlier, ledger pays
    member = world.member(rid, 1)
    assert member["status"] == "ACTIVE" and member["stake"] == str(STAKE) and world.rule(rid)["members"] == 1
    assert world.claimable(1) == 0
    world.fund(1, STAKE)
    world.fail(1, "enlist", rid, match="already enlisted")
    assert world.claimable(1) == STAKE                                        # the refused call kept the ledger intact
    world.audit()


def test_leave_returns_stake_and_allows_reenlist(world):
    rid = world.certified_rule()
    world.fail(1, "leave", rid, match="not a member")
    world.enlist(1, rid)
    world.call(1, "leave", rid)
    assert world.member(rid, 1)["status"] == "LEFT" and world.claimable(1) == STAKE
    assert world.rule(rid)["members"] == 0
    world.fail(1, "leave", rid, match="not a member")
    world.call(1, "enlist", rid)                       # the released stake is in the ledger: re-enlist needs no new value
    assert world.member(rid, 1)["status"] == "ACTIVE" and world.rule(rid)["members"] == 1 and world.claimable(1) == 0
    world.audit()


def test_withdraw_pays_once_and_zeroes_ledger(world):
    rid = world.certified_rule()
    world.enlist(1, rid)
    world.call(1, "leave", rid)
    world.fail(2, "withdraw", match="nothing to withdraw")
    world.call(1, "withdraw")
    assert world.transfers == [{"to": world.w(1), "value": STAKE, "method": None, "on": "finalized"}]
    world.fail(1, "withdraw", match="nothing to withdraw")
    stats = world.audit()
    assert stats["claimable_total"] == "0" and stats["escrow_total"] == str(BOND)


def test_withdraw_refuses_when_contract_balance_is_short(world):
    rid = world.certified_rule()
    world.enlist(1, rid)
    world.call(1, "leave", rid)
    world.vm.deal(world.vm._contract_address, STAKE - 1)
    world.fail(1, "withdraw", match="balance too low")
    assert world.claimable(1) == STAKE and world.transfers == []


def test_views_paginate_and_reject_bad_input(world):
    for n in range(3):
        world.forge(title=f"Rule number {n}")
    page = world.view("list_rules", 2, 10)
    assert [r["id"] for r in page] == [2, 3]
    assert [r["id"] for r in world.view("list_rules", 1, 2)] == [1, 2]
    assert world.view("list_rules", 0, 1)[0]["id"] == 1
    with world.vm.expect_revert("rule not found"):
        world.c.get_rule(99)
    with world.vm.expect_revert("case not found"):
        world.c.get_case(1)
    with world.vm.expect_revert("invalid wallet"):
        world.c.get_claimable("not-an-address")
