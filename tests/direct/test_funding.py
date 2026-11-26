"""The deposit-first funding model.

The first live Studionet run showed that a REJECTED call keeps the native value attached to it, and a
contract cannot refund inside a call that reverts.  Hence: only deposit() accepts value and it cannot fail
once value is attached; every other method is non-payable and spends ledger balance.  A rejected call can
therefore never strand funds.
"""
import json

import pytest

from conftest import BOND, DEPOSIT, PROBES, R, RULE_TEXT, S_PLAIN, STAKE, UNIT, AGGRAVATORS, llm

FORGE = ("Funding model rule", RULE_TEXT, json.dumps(AGGRAVATORS), json.dumps(PROBES), UNIT, DEPOSIT, STAKE, BOND,
         3, 100, 2, 2)


def test_deposit_credits_the_ledger_and_escrow(world):
    world.fund(1, 500)
    world.fund(1, 250)
    world.fund(2, 7)
    assert (world.claimable(1), world.claimable(2)) == (750, 7)
    stats = world.audit()
    assert stats["escrow_total"] == "757" and stats["claimable_total"] == "757"


def test_a_deposit_of_nothing_is_refused(world):
    with world.vm.expect_revert("positive value"):
        world.call(1, "deposit")
    assert world.stats()["escrow_total"] == "0"


def test_forge_needs_the_bond_in_the_ledger_and_takes_exactly_the_bond(world):
    world.fail(0, "forge_rule", *FORGE, match="insufficient ledger balance")
    world.fund(0, BOND - 1)
    world.fail(0, "forge_rule", *FORGE, match="insufficient ledger balance")
    assert world.claimable(0) == BOND - 1 and world.stats()["rules"] == 0
    world.fund(0, 1 + 500)                                   # 500 more than needed stays in the ledger
    world.call(0, "forge_rule", *FORGE)
    assert world.claimable(0) == 500 and world.rule(1)["bond_remaining"] == str(BOND)
    world.audit()


def test_bond_below_the_minimum_is_refused_even_when_funded(world):
    world.fund(0, 10 * UNIT)
    low = FORGE[:7] + (3 * UNIT - 1,) + FORGE[8:]
    world.fail(0, "forge_rule", *low, match="bond must be at least")
    assert world.claimable(0) == 10 * UNIT


def test_filing_and_appealing_need_ledger_funds_and_change_nothing_when_short(world):
    rid = world.certified_rule()
    world.enlist(1, rid)
    world.enlist(2, rid)
    before = world.stats()
    world.fail(1, "file_case", rid, world.w(2), S_PLAIN, match="insufficient ledger balance")
    assert world.stats() == before and world.rule(rid)["cases"] == 0
    cid = world.file(1, 2, S_PLAIN, rid)
    world.call(2, "submit_defense", cid, "I deny it but give no concrete facts here.")
    llm(world.vm, (r"LITMUS", R("BREACH", False, False)))
    world.call(3, "rule_case", cid)
    llm(world.vm, (r"LITMUS", R("COMPLIANT", False, False)))
    world.fund(2, DEPOSIT - 1)
    before = world.stats()
    world.fail(2, "appeal_case", cid, "The page linked here was free of charge.", match="insufficient ledger balance")
    assert world.stats() == before and world.case(cid)["status"] == "RULED" and world.claimable(2) == DEPOSIT - 1
    world.fund(2, 1)
    world.call(2, "appeal_case", cid, "The page linked here was free of charge.")
    assert world.case(cid)["status"] == "SETTLED" and world.claimable(2) > 0
    world.audit()


def test_ledger_balance_is_recycled_between_actions_without_new_value(world):
    """Winnings, released stakes and reclaimed bonds fund the next action: no round trip through a wallet."""
    rid = world.certified_rule()
    for who in (1, 2, 3):
        world.enlist(who, rid)
    cid = world.file(1, 2, S_PLAIN, rid)
    world.call(2, "submit_defense", cid, "I deny it but give no concrete facts here.")
    llm(world.vm, (r"LITMUS", R("BREACH", True, False)))
    world.call(3, "rule_case", cid)
    world.call(2, "waive_appeal", cid)
    world.call(3, "settle_case", cid)
    won = world.claimable(1)
    assert won == DEPOSIT + 2 * UNIT
    inflow = world.inflow
    second = world.call(1, "file_case", rid, world.w(3), "A second statement about what happened here.")
    assert world.claimable(1) == won - DEPOSIT and world.inflow == inflow     # paid from winnings, no new value
    assert world.case(second)["status"] == "OPEN"
    world.audit()


@pytest.mark.parametrize("fn,args", [
    ("enlist", (1,)), ("leave", (1,)), ("file_case", (1, "0x" + "11" * 20, "A statement about it.")),
    ("submit_defense", (1, "A defense with facts.")), ("rule_case", (1,)), ("settle_case", (1,)),
    ("waive_appeal", (1,)), ("appeal_case", (1, "Grounds for the appeal.")), ("void_case", (1,)),
    ("retire_rule", (9,)), ("reclaim_bond", (9,)), ("certify_rule", (9,)), ("run_probe", (9, 0)),
])
def test_rejected_calls_never_move_money(world, fn, args):
    world.fund(1, 12_345)
    before = (world.stats(), world.claimable(1))
    world.fail(1, fn, *args)                                   # no value attached, must be rejected
    assert (world.stats(), world.claimable(1)) == before
    world.audit()
