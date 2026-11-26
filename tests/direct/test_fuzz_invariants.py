"""Seeded random walks over the whole API. After every call the solvency books must balance."""
import json
import random

import pytest

from conftest import BASE_TS, DEPOSIT, P_BREACH_BOTH, P_BREACH_PLAIN, P_OK_BLOG, P_OK_FREE, PROBES, R, llm

ACTORS = 6


def random_ruling(rng, n, allow_inconclusive=True):
    roll = rng.random()
    if roll < 0.45:
        return R("BREACH", *[rng.random() < 0.5 for _ in range(n)])
    if roll < 0.8 or not allow_inconclusive:
        return R("COMPLIANT", *([False] * n))
    return R("INCONCLUSIVE", *([False] * n))


def try_call(w, who, fn, *args, value=0):
    try:
        w.call(who, fn, *args, value=value)
        return True
    except Exception as exc:       # contract refusals are expected; anything else is re-raised by audit
        assert "Unknown" not in repr(exc) and not isinstance(exc, (KeyError, TypeError, AttributeError, NameError)), repr(exc)
        return False


def idx(w, wallet):
    return next(i for i in range(ACTORS) if w.w(i) == wallet)


def author_index(w, rid):
    return idx(w, w.rule(rid)["author"])


def pick_actor(rng, w, who, good):
    """With high probability act as the party that is supposed to act, otherwise as a random wallet."""
    return good if rng.random() < 0.85 else who


COVERED = set()


@pytest.mark.parametrize("seed", range(10))
def test_random_walk_keeps_books_balanced(world, seed):
    rng = random.Random(seed)
    w = world
    w.certified_rule(strikes=rng.randint(1, 3), pass_percent=rng.choice([75, 100]))   # bootstrap: rule 1 is live
    for who in (1, 2, 3, 4):
        w.enlist(who, 1)
    clock = 0
    successes = 0
    seen = set()
    for step in range(600):
        stats = w.stats()
        rules, cases = stats["rules"], stats["cases"]
        who = rng.randrange(ACTORS)
        op = rng.choice(["forge", "probe", "certify", "enlist", "enlist", "enlist", "leave", "file", "file", "file", "file", "file",
                         "defense", "rule", "rule", "waive", "appeal", "appeal", "settle", "settle",
                         "withdraw", "retire", "reclaim", "void", "tick_due", "tick_due", "tick_due"])
        live = [r["id"] for r in w.view("list_rules", 1, 10) if r["status"] == "CERTIFIED"]
        rid = rng.choice(live) if live and rng.random() < 0.8 else rng.randint(1, rules)
        cid = rng.randint(1, max(cases, 1))
        case = w.case(cid) if cases else None
        n = 2
        ok = False
        if op == "forge" and rules < 6 and (not live or rng.random() < 0.3):
            bond = rng.choice([3000, 6000])
            try_call(w, who, "deposit", value=bond + rng.choice([0, 0, 0, -1, 1]))
            ok = try_call(w, who, "forge_rule", "Fuzz rule title", "Fuzz rule: members must not do the forbidden thing.",
                          json.dumps(["Aggravating circumstance one holds.", "Aggravating circumstance two holds."]),
                          json.dumps(PROBES), 1000, 100, rng.choice([6000, 9000]), bond, rng.randint(1, 3),
                          rng.choice([75, 100]), 2, 2)
        elif op == "probe":
            index = rng.randrange(4)
            truth = PROBES[index]
            ruling = R(truth["expected"], *truth["factors"]) if rng.random() < 0.85 else random_ruling(rng, n, False)
            llm(w.vm, (r"LITMUS", ruling))
            ok = try_call(w, pick_actor(rng, w, who, author_index(w, rid)), "run_probe", rid, index)
        elif op == "certify":
            ok = try_call(w, pick_actor(rng, w, who, author_index(w, rid)), "certify_rule", rid)
        elif op == "enlist":
            try_call(w, who, "deposit", value=int(w.rule(rid)["enlist_stake"]) + rng.choice([0, 0, 0, -1, 1]))
            ok = try_call(w, who, "enlist", rid)
        elif op == "leave" and rng.random() < 0.25:
            ok = try_call(w, who, "leave", rid)
        elif op == "file":
            plaintiff = rng.choice([1, 2, 3, 4]) if rng.random() < 0.85 else who
            defendant = rng.choice([1, 2, 3, 4])
            try_call(w, plaintiff, "deposit", value=DEPOSIT + rng.choice([0, 0, 0, -1, 5]))
            ok = try_call(w, plaintiff, "file_case", rid, w.w(defendant), "A statement about what happened here.")
        elif op == "defense" and case:
            ok = try_call(w, pick_actor(rng, w, who, idx(w, case["defendant"])), "submit_defense", cid,
                          "A concrete defense with facts of its own.")
        elif op == "rule" and case:
            llm(w.vm, (r"LITMUS", random_ruling(rng, n)))
            ok = try_call(w, who, "rule_case", cid)
        elif op == "waive" and case:
            party = case["defendant"] if case["verdict"] == "BREACH" else case["plaintiff"]
            ok = try_call(w, pick_actor(rng, w, who, idx(w, party)), "waive_appeal", cid)
        elif op == "appeal" and case:
            party = case["defendant"] if case["verdict"] == "BREACH" else case["plaintiff"]
            llm(w.vm, (r"LITMUS", random_ruling(rng, n)))
            actor = pick_actor(rng, w, who, idx(w, party))
            try_call(w, actor, "deposit", value=DEPOSIT + rng.choice([0, 0, -1]))
            ok = try_call(w, actor, "appeal_case", cid, "Grounds for the appeal are stated here.")
        elif op == "settle" and case:
            ok = try_call(w, who, "settle_case", cid)
        elif op == "withdraw":
            ok = try_call(w, who, "withdraw")
        elif op == "void" and case:
            if rng.random() < 0.15:
                clock += 8 * 24 * 3600
                w.at(clock)
            ok = try_call(w, who, "void_case", cid)
        elif op == "retire" and (rid != 1 or step > 500) and rng.random() < 0.3:
            ok = try_call(w, author_index(w, rid), "retire_rule", rid)
        elif op == "reclaim":
            ok = try_call(w, author_index(w, rid), "reclaim_bond", rid)
        elif op == "tick_due" and case:
            due = case["appeal_due"] if case["status"] == "RULED" else case["defense_due"]
            clock = max(clock, due - BASE_TS + rng.choice([-1, 0, 1, 2]))
            w.at(clock)
        else:
            clock += rng.choice([30, 90])
            w.at(clock)
        if ok:
            successes += 1
            seen.add(op)
            w.audit()
    assert successes > 30, f"the random walk must actually exercise the contract ({successes})"
    assert {"file", "rule", "settle"} <= seen, seen
    for rule in w.view("list_rules", 1, 10):
        for key in ("breaches", "dismissed", "inconclusive", "voided", "reversals"):
            if rule[key]:
                COVERED.add(key)
        COVERED.add("status:" + rule["status"])
        for who in range(ACTORS):
            member = w.member(rule["id"], who)
            if member:
                COVERED.add("member:" + member["status"])


def test_random_walks_covered_every_outcome():
    expected = {"breaches", "dismissed", "inconclusive", "voided", "reversals", "status:SUSPENDED",
                "status:RETIRED", "member:ACTIVE", "member:LEFT", "member:EXPELLED"}
    assert expected <= COVERED, f"never reached: {sorted(expected - COVERED)}"


def test_random_walk_ends_with_everything_withdrawable(world):
    """Retire everything, settle everything, withdraw everything: the contract ends at zero."""
    w = world
    rid = w.certified_rule()
    for who in (1, 2, 3):
        w.enlist(who, rid)
    llm(w.vm, (r"LITMUS", R("BREACH", True, True)))
    cid = w.file(1, 2, "A statement about what happened here.")
    w.at(200)
    w.call(3, "rule_case", cid)
    w.call(2, "waive_appeal", cid)
    w.call(3, "settle_case", cid)
    for who in (1, 2, 3):
        w.call(who, "leave", rid)
    w.call(0, "retire_rule", rid)
    w.call(0, "reclaim_bond", rid)
    for who in range(ACTORS):
        if w.claimable(who):
            w.call(who, "withdraw")
    stats = w.audit()
    assert stats["escrow_total"] == "0" and stats["claimable_total"] == "0"
