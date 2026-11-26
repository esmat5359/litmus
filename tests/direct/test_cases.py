"""Cases, defenses, rulings, appeals and exact payouts."""
import pytest

from conftest import BASE_TS, BOND, DEPOSIT, R, S_BOTH, S_FREE, S_PLAIN, S_VAGUE, STAKE, UNIT, llm

PLAINTIFF, DEFENDANT, OTHER = 1, 2, 3
CASE = r"(?s)MODE: CASE.*"
APPEAL = r"(?s)MODE: APPEAL.*"


@pytest.fixture
def ready(world):
    rid = world.certified_rule()
    for who in (PLAINTIFF, DEFENDANT, OTHER):
        world.enlist(who, rid)
    return world


def ruled_case(world, statement, ruling, *, defense=None, pattern=None):
    cid = world.file(PLAINTIFF, DEFENDANT, statement)
    if defense:
        world.call(DEFENDANT, "submit_defense", cid, defense)
    else:
        world.at(world.case(cid)["defense_due"] - BASE_TS)
    llm(world.vm, (pattern or CASE + statement.split()[-5], ruling))
    world.call(OTHER, "rule_case", cid)
    return cid


def test_file_case_checks_and_escrows_deposit(ready):
    w = ready
    w.fail(4, "file_case", 1, w.w(DEFENDANT), S_PLAIN, match="plaintiff is not an active member")
    w.fail(PLAINTIFF, "file_case", 1, w.w(4), S_PLAIN, match="defendant is not an active member")
    w.fail(PLAINTIFF, "file_case", 1, w.w(PLAINTIFF), S_PLAIN, match="yourself")
    w.fail(PLAINTIFF, "file_case", 1, w.w(DEFENDANT), S_PLAIN, match="insufficient ledger balance")
    w.fail(PLAINTIFF, "file_case", 1, w.w(DEFENDANT), "too short", match="statement")
    w.fail(PLAINTIFF, "file_case", 1, "0x" + "0" * 40, S_PLAIN, match="zero wallet")
    w.fail(PLAINTIFF, "file_case", 1, "garbage", S_PLAIN, match="invalid wallet")
    before = int(w.stats()["escrow_total"])
    cid = w.file(PLAINTIFF, DEFENDANT, S_PLAIN)
    case = w.case(cid)
    assert cid == 1 and case["status"] == "OPEN" and case["deposit"] == str(DEPOSIT)
    assert int(w.stats()["escrow_total"]) == before + DEPOSIT
    assert w.member(1, DEFENDANT)["locked"] == str(3 * UNIT) and w.member(1, DEFENDANT)["open_cases"] == 1
    assert w.rule()["open_cases"] == 1 and w.rule()["cases"] == 1
    w.audit()


def test_cannot_file_against_draft_retired_or_suspended_rules(world):
    rid = world.forge()
    with world.vm.expect_revert("not certified"):
        world.file(1, 2, S_PLAIN, rid)


def test_defense_rules_and_ruling_window(ready):
    w = ready
    cid = w.file(PLAINTIFF, DEFENDANT, S_PLAIN)
    w.fail(OTHER, "rule_case", cid, match="defense window still open")
    w.fail(PLAINTIFF, "submit_defense", cid, "I did nothing wrong at all.", match="only the defendant")
    w.fail(DEFENDANT, "submit_defense", cid, "no", match="defense")
    w.call(DEFENDANT, "submit_defense", cid, "It was a free resource, not a product.")
    w.fail(DEFENDANT, "submit_defense", cid, "A second defense is not allowed.", match="already submitted")
    llm(w.vm, (CASE + "buy their paid", R("BREACH", False, False)))
    w.call(OTHER, "rule_case", cid)         # a submitted defense makes the case ready immediately
    assert w.case(cid)["status"] == "RULED"
    w.fail(OTHER, "rule_case", cid, match="not open")


def test_defense_window_boundaries(ready):
    w = ready
    cid = w.file(PLAINTIFF, DEFENDANT, S_PLAIN)
    due = w.case(cid)["defense_due"]
    w.at(119)
    w.fail(OTHER, "rule_case", cid, match="still open")
    w.at(120)                                             # exactly at due: ruling allowed, defense also allowed
    w.call(DEFENDANT, "submit_defense", cid, "Submitted exactly at the deadline second.")
    assert w.case(cid)["defense_at"] == due
    cid2 = w.file(PLAINTIFF, DEFENDANT, S_PLAIN + " Again.")
    w.at(120 + 121)
    w.fail(DEFENDANT, "submit_defense", cid2, "Too late for the defense window.", match="window closed")
    llm(w.vm, (CASE, R("COMPLIANT", False, False)))
    w.call(OTHER, "rule_case", cid2)
    assert w.case(cid2)["defense"] == ""


@pytest.mark.parametrize("flags,severity", [((False, False), 1), ((True, False), 2), ((False, True), 2), ((True, True), 3)])
def test_breach_penalty_is_exactly_severity_times_unit(ready, flags, severity):
    w = ready
    cid = w.file(PLAINTIFF, DEFENDANT, S_BOTH)
    w.call(DEFENDANT, "submit_defense", cid, "I deny it, but I give no concrete facts.")
    llm(w.vm, (CASE, R("BREACH", *flags)))
    w.call(OTHER, "rule_case", cid)
    case = w.case(cid)
    assert case["verdict"] == "BREACH" and case["severity"] == severity and case["factors"] == list(flags)
    w.fail(PLAINTIFF, "settle_case", cid, match="appeal window still open")
    w.at(121)
    w.call(PLAINTIFF, "settle_case", cid)
    case = w.case(cid)
    assert case["status"] == "SETTLED" and case["penalty"] == str(UNIT * severity)
    assert w.member(1, DEFENDANT)["stake"] == str(STAKE - UNIT * severity)
    assert w.member(1, DEFENDANT)["strikes"] == 1 and w.member(1, DEFENDANT)["locked"] == "0"
    assert w.claimable(PLAINTIFF) == DEPOSIT + UNIT * severity and w.claimable(DEFENDANT) == 0
    assert w.rule()["breaches"] == 1 and w.rule()["open_cases"] == 0
    w.audit()


def test_compliant_forfeits_deposit_to_defendant_and_inconclusive_refunds(ready):
    w = ready
    a = w.file(PLAINTIFF, DEFENDANT, S_FREE)
    b = w.file(PLAINTIFF, DEFENDANT, S_VAGUE)
    llm(w.vm, (CASE + "free community map", R("COMPLIANT", False, False)),
        (CASE + "does not say what", R("INCONCLUSIVE", False, False)))
    w.at(130)
    w.call(OTHER, "rule_case", a)
    w.call(OTHER, "rule_case", b)
    w.call(PLAINTIFF, "waive_appeal", a)
    w.call(PLAINTIFF, "waive_appeal", b)
    w.call(OTHER, "settle_case", a)
    w.call(OTHER, "settle_case", b)
    assert w.claimable(DEFENDANT) == DEPOSIT and w.claimable(PLAINTIFF) == DEPOSIT
    assert w.case(a)["final_verdict"] == "COMPLIANT" and w.case(a)["penalty"] == "0"
    assert w.case(b)["final_verdict"] == "INCONCLUSIVE"
    assert w.member(1, DEFENDANT)["strikes"] == 0 and w.member(1, PLAINTIFF)["dismissed_filings"] == 1
    rule = w.rule()
    assert (rule["dismissed"], rule["inconclusive"], rule["breaches"]) == (1, 1, 0)
    w.audit()


def test_settlement_gates_and_waiver_eligibility(ready):
    w = ready
    cid = ruled_case(w, S_PLAIN, R("BREACH", False, False))
    w.fail(PLAINTIFF, "waive_appeal", cid, match="only the party that may appeal")
    w.fail(OTHER, "waive_appeal", cid, match="only the party that may appeal")
    w.fail(OTHER, "settle_case", cid, match="still open")
    due = w.case(cid)["appeal_due"]
    w.at(due - BASE_TS)                             # exactly appeal_due: appeal allowed, settle not
    w.fail(OTHER, "settle_case", cid, match="still open")
    w.at(due - BASE_TS + 1)
    w.call(OTHER, "settle_case", cid)
    w.fail(OTHER, "settle_case", cid, match="not awaiting settlement")
    w.fail(DEFENDANT, "appeal_case", cid, "Late appeal grounds here.", match="not awaiting settlement")


def test_waiver_blocks_appeal_and_enables_early_settlement(ready):
    w = ready
    cid = ruled_case(w, S_PLAIN, R("BREACH", True, False))
    w.call(DEFENDANT, "waive_appeal", cid)
    w.fail(DEFENDANT, "appeal_case", cid, "I changed my mind about this.", match="waived")
    w.call(OTHER, "settle_case", cid)
    assert w.case(cid)["penalty"] == str(2 * UNIT)


def test_appeal_requirements(ready):
    w = ready
    cid = ruled_case(w, S_PLAIN, R("BREACH", False, False))
    llm(w.vm, (APPEAL, R("COMPLIANT", False, False)))
    w.fail(PLAINTIFF, "appeal_case", cid, "Plaintiff cannot appeal a win.", match="adversely affected")
    w.fail(OTHER, "appeal_case", cid, "Bystanders cannot appeal a case.", match="adversely affected")
    w.fail(DEFENDANT, "appeal_case", cid, "Short grounds.", match="insufficient ledger balance")
    w.fail(DEFENDANT, "appeal_case", cid, "x", match="appeal grounds")
    w.at(250)
    w.fail(DEFENDANT, "appeal_case", cid, "Appeal after the window closed.", match="window closed")
    assert w.case(cid)["status"] == "RULED"


def test_failed_appeal_model_call_reverts_everything(ready):
    w = ready
    cid = ruled_case(w, S_PLAIN, R("BREACH", False, False))
    llm(w.vm, (APPEAL, "garbage"))
    w.fund(DEFENDANT, DEPOSIT)
    before = w.stats()
    with w.vm.expect_revert("LLM_ERROR"):
        w.call(DEFENDANT, "appeal_case", cid, "The post linked to a free page.")
    assert w.case(cid)["status"] == "RULED" and w.case(cid)["appeal_by"] == ""
    assert w.stats() == before


def test_favorable_defendant_appeal_slashes_bond_and_pays_exactly(ready):
    w = ready
    cid = ruled_case(w, S_PLAIN, R("BREACH", True, False))
    llm(w.vm, (APPEAL, R("COMPLIANT", False, False)))
    w.appeal(DEFENDANT, cid, "The linked page was a free resource.")
    case = w.case(cid)
    assert case["status"] == "SETTLED" and case["reversed"] is True and case["final_verdict"] == "COMPLIANT"
    assert case["verdict"] == "BREACH" and case["appeal_verdict"] == "COMPLIANT" and case["penalty"] == "0"
    assert w.claimable(DEFENDANT) == DEPOSIT + DEPOSIT + UNIT      # forfeited filing + own appeal refund + slash
    assert w.claimable(PLAINTIFF) == 0
    rule = w.rule()
    assert rule["reversals"] == 1 and rule["bond_remaining"] == str(BOND - UNIT) and rule["status"] == "CERTIFIED"
    assert w.member(1, DEFENDANT)["strikes"] == 0 and w.member(1, DEFENDANT)["stake"] == str(STAKE)
    w.audit()


def test_unfavorable_defendant_appeal_pays_plaintiff_and_uses_appeal_factors(ready):
    w = ready
    cid = ruled_case(w, S_PLAIN, R("BREACH", False, False))
    llm(w.vm, (APPEAL, R("BREACH", True, True)))
    w.appeal(DEFENDANT, cid, "Nothing in the statement says I tagged anyone.")
    case = w.case(cid)
    assert case["reversed"] is False and case["final_severity"] == 3 and case["penalty"] == str(3 * UNIT)
    assert w.claimable(PLAINTIFF) == DEPOSIT + 3 * UNIT + DEPOSIT   # filing refund + penalty + forfeited appeal deposit
    assert w.claimable(DEFENDANT) == 0 and w.rule()["reversals"] == 0
    assert w.rule()["bond_remaining"] == str(BOND)
    w.audit()


def test_plaintiff_appeal_paths(ready):
    w = ready
    # COMPLIANT -> BREACH: favorable
    a = ruled_case(w, S_FREE, R("COMPLIANT", False, False))
    llm(w.vm, (APPEAL, R("BREACH", False, True)))
    w.appeal(PLAINTIFF, a, "The map page also sells paid tours.")
    assert w.case(a)["reversed"] is True and w.case(a)["penalty"] == str(2 * UNIT)
    assert w.claimable(PLAINTIFF) == DEPOSIT + 2 * UNIT + DEPOSIT + UNIT
    # COMPLIANT -> INCONCLUSIVE: changed but not favorable; appeal deposit goes to defendant
    b = ruled_case(w, S_FREE + " Second.", R("COMPLIANT", False, False), pattern=CASE + "Second")
    llm(w.vm, (APPEAL, R("INCONCLUSIVE", False, False)))
    claim_before = w.claimable(DEFENDANT)
    w.appeal(PLAINTIFF, b, "The statement leaves a decisive fact open.")
    assert w.case(b)["reversed"] is False and w.case(b)["final_verdict"] == "INCONCLUSIVE"
    assert w.claimable(DEFENDANT) == claim_before + DEPOSIT
    # INCONCLUSIVE -> INCONCLUSIVE: unfavorable
    c = ruled_case(w, S_VAGUE, R("INCONCLUSIVE", False, False), pattern=CASE + "does not say what")
    llm(w.vm, (APPEAL, R("INCONCLUSIVE", False, False)))
    w.appeal(PLAINTIFF, c, "More detail should be accepted here.")
    assert w.case(c)["reversed"] is False and w.rule()["reversals"] == 1
    w.audit()


def test_two_reversals_suspend_the_rule_but_open_cases_still_settle(ready):
    w = ready
    open_case = w.file(OTHER, PLAINTIFF, S_VAGUE)
    for n in range(2):
        cid = ruled_case(w, S_PLAIN + f" Number {n}.", R("BREACH", False, False), pattern=CASE + f"Number {n}")
        llm(w.vm, (APPEAL, R("COMPLIANT", False, False)))
        w.appeal(DEFENDANT, cid, "It was a free resource for everyone.")
    rule = w.rule()
    assert rule["status"] == "SUSPENDED" and rule["reversals"] == 2
    assert rule["bond_remaining"] == str(BOND - 2 * UNIT)
    w.fail(PLAINTIFF, "file_case", 1, w.w(DEFENDANT), S_PLAIN, match="not certified")
    w.fail(4, "enlist", 1, match="not certified")
    llm(w.vm, (CASE, R("INCONCLUSIVE", False, False)))
    w.at(300)
    w.call(PLAINTIFF, "rule_case", open_case)                 # existing cases continue
    w.call(OTHER, "waive_appeal", open_case)
    w.call(PLAINTIFF, "settle_case", open_case)
    w.call(0, "retire_rule", 1)
    w.call(0, "reclaim_bond", 1)
    assert w.claimable(0) == BOND - 2 * UNIT
    w.audit()


def test_bond_slash_is_capped_by_remaining_bond(world):
    w = world
    rid = w.certified_rule(bond=3 * UNIT, strikes=5)            # exactly the minimum bond
    for who in (PLAINTIFF, DEFENDANT):
        w.enlist(who, rid)
    for n in range(2):
        cid = w.file(PLAINTIFF, DEFENDANT, S_PLAIN + f" Number {n}.")
        w.at(130 + 300 * n)
        llm(w.vm, (CASE + f"Number {n}", R("BREACH", False, False)))
        w.call(OTHER, "rule_case", cid)
        llm(w.vm, (APPEAL, R("COMPLIANT", False, False)))
        w.appeal(DEFENDANT, cid, "It was a free resource for everyone.")
    assert w.rule()["bond_remaining"] == str(UNIT)
    w.audit()


def test_open_case_limits_and_stake_reservation(world):
    w = world
    rid = w.certified_rule(stake=4 * 3 * UNIT)                  # room for four reservations
    for who in (1, 2, 3, 4, 5):
        w.enlist(who, rid)
    for plaintiff in (1, 3, 4):
        w.file(plaintiff, 2, S_PLAIN + f" By {plaintiff}.")
    w.fail(5, "file_case", rid, w.w(2), S_PLAIN, match="defendant has too many open cases")
    for n in range(3):                                         # plaintiff 1 already has 1 open; fill to 3
        if n < 2:
            w.file(1, 3 + n, S_PLAIN + f" Extra {n}.")
    w.fail(1, "file_case", rid, w.w(5), S_PLAIN, match="plaintiff has too many open cases")
    w.audit()
    tight = w.certified_rule(stake=3 * UNIT, title="Tight stake rule")
    for who in (6, 7, 8):
        w.enlist(who, tight)
    w.file(6, 7, S_PLAIN, tight)
    w.fail(8, "file_case", tight, w.w(7), S_PLAIN, match="cannot cover another case")
    w.audit()


def test_strikes_expel_and_expelled_members_are_handled(ready):
    w = ready
    rid = w.certified_rule(strikes=1, title="One strike rule")
    for who in (PLAINTIFF, DEFENDANT, OTHER):
        w.enlist(who, rid)
    first = w.file(PLAINTIFF, DEFENDANT, S_PLAIN, rid)
    second = w.file(OTHER, DEFENDANT, S_FREE, rid)
    w.at(130)
    llm(w.vm, (CASE + "buy their paid", R("BREACH", False, False)), (CASE + "free community map", R("COMPLIANT", False, False)))
    w.call(1, "rule_case", first)
    w.call(1, "rule_case", second)
    w.call(DEFENDANT, "waive_appeal", first)
    w.call(1, "settle_case", first)
    member = w.member(rid, DEFENDANT)
    assert member["status"] == "EXPELLED" and member["strikes"] == 1 and w.rule(rid)["members"] == 2
    w.fail(DEFENDANT, "leave", rid, match="open cases pending")
    w.fail(DEFENDANT, "enlist", rid, match="already enlisted or expelled")
    w.fail(DEFENDANT, "file_case", rid, w.w(PLAINTIFF), S_PLAIN, match="not an active member")
    w.fail(PLAINTIFF, "file_case", rid, w.w(DEFENDANT), S_PLAIN, match="defendant is not an active member")
    w.call(OTHER, "waive_appeal", second)
    w.call(1, "settle_case", second)
    w.call(DEFENDANT, "leave", rid)
    assert w.claimable(DEFENDANT) == (STAKE - UNIT) + DEPOSIT   # remaining stake + forfeited filing deposit
    assert w.member(rid, DEFENDANT)["status"] == "EXPELLED" and w.member(rid, DEFENDANT)["stake"] == "0"
    w.fail(DEFENDANT, "leave", rid, match="nothing to release")
    assert w.rule(rid)["members"] == 2
    w.audit()


def test_leaving_with_prior_strikes_keeps_strikes_on_return(ready):
    w = ready
    cid = ruled_case(w, S_PLAIN, R("BREACH", False, False))
    w.call(DEFENDANT, "waive_appeal", cid)
    w.call(OTHER, "settle_case", cid)
    w.call(DEFENDANT, "leave", 1)
    w.enlist(DEFENDANT, 1)
    assert w.member(1, DEFENDANT)["strikes"] == 1 and w.member(1, DEFENDANT)["stake"] == str(STAKE)
    assert w.claimable(DEFENDANT) == STAKE - UNIT
    w.audit()


def test_reclaim_bond_blocked_while_cases_open_then_allowed(ready):
    w = ready
    cid = w.file(PLAINTIFF, DEFENDANT, S_FREE)
    w.call(0, "retire_rule", 1)
    w.fail(0, "reclaim_bond", 1, match="cases are still open")
    w.fail(PLAINTIFF, "file_case", 1, w.w(DEFENDANT), S_PLAIN, match="not certified")
    llm(w.vm, (CASE, R("COMPLIANT", False, False)))
    w.at(130)
    w.call(OTHER, "rule_case", cid)
    w.call(PLAINTIFF, "waive_appeal", cid)
    w.call(OTHER, "settle_case", cid)
    w.call(0, "reclaim_bond", 1)
    assert w.claimable(0) == BOND
    w.audit()


def test_full_cash_out_leaves_contract_with_zero_escrow(ready):
    w = ready
    cid = ruled_case(w, S_BOTH, R("BREACH", True, True))
    w.call(DEFENDANT, "waive_appeal", cid)
    w.call(OTHER, "settle_case", cid)
    for who in (PLAINTIFF, DEFENDANT, OTHER):
        w.call(who, "leave", 1)
    w.call(0, "retire_rule", 1)
    w.call(0, "reclaim_bond", 1)
    for who in (0, PLAINTIFF, DEFENDANT, OTHER):
        if w.claimable(who):
            w.call(who, "withdraw")
    stats = w.audit()
    assert stats["escrow_total"] == "0" and stats["claimable_total"] == "0"
    assert sum(t["value"] for t in w.transfers) == w.inflow


# ---------------------------------------------------------------------------- liveness escape
VOID_AFTER = 7 * 24 * 3600


def test_void_case_frees_a_case_no_quorum_could_rule_on(ready):
    w = ready
    cid = w.file(PLAINTIFF, DEFENDANT, S_PLAIN)
    due = w.case(cid)["defense_due"] - BASE_TS
    w.fail(OTHER, "void_case", cid, match="not yet voidable")
    w.at(due + VOID_AFTER)                                        # exactly at the boundary: still not voidable
    w.fail(OTHER, "void_case", cid, match="not yet voidable")
    w.at(due + VOID_AFTER + 1)
    w.call(4, "void_case", cid)                                   # anybody may call it
    case = w.case(cid)
    assert case["status"] == "SETTLED" and case["final_verdict"] == "VOID" and case["penalty"] == "0"
    assert w.claimable(PLAINTIFF) == DEPOSIT and w.claimable(DEFENDANT) == 0
    assert w.member(1, DEFENDANT)["locked"] == "0" and w.member(1, DEFENDANT)["open_cases"] == 0
    assert w.member(1, PLAINTIFF)["open_cases"] == 0 and w.member(1, DEFENDANT)["strikes"] == 0
    rule = w.rule()
    assert rule["voided"] == 1 and rule["open_cases"] == 0 and rule["breaches"] == 0
    w.fail(OTHER, "void_case", cid, match="not open")
    w.fail(OTHER, "rule_case", cid, match="not open")
    w.call(DEFENDANT, "leave", 1)                                 # the defendant is no longer locked in
    w.audit()


def test_void_case_is_only_for_cases_nobody_ruled_on(ready):
    w = ready
    cid = ruled_case(w, S_PLAIN, R("BREACH", False, False))
    w.at(VOID_AFTER + 10_000)
    w.fail(OTHER, "void_case", cid, match="not open")             # RULED cases settle, they are never voided
    w.call(OTHER, "settle_case", cid)
    w.fail(OTHER, "void_case", cid, match="not open")
    w.audit()
