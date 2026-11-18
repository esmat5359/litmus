"""Checks that mirror constraints of the real GenVM v0.2.16 runtime."""
import json

from conftest import R, S_PLAIN, llm
from test_equality_law import build, probe_pairs


def test_nondeterministic_closures_pickle_and_capture_only_plain_data(world):
    """The real VM ships leader/validator closures to other nodes: they must pickle and carry no storage."""
    world.vm.check_pickling = True
    rid, n = build(world, 2)
    llm(world.vm, *probe_pairs(2))
    for i in range(4):
        world.call(0, "run_probe", rid, i)
    world.call(0, "certify_rule", rid)
    for who in (1, 2):
        world.enlist(who, rid)
    cid = world.file(1, 2, S_PLAIN, rid)
    world.call(2, "submit_defense", cid, "I deny it but give no concrete facts here.")
    llm(world.vm, (r"LITMUS", R("BREACH", True, False)))
    world.call(3, "rule_case", cid)
    llm(world.vm, (r"LITMUS", R("COMPLIANT", False, False)))
    world.call(2, "appeal_case", cid, "The statement does not prove the breach.", value=100)
    assert len(world.vm._captured_validators) == 4 + 1 + 1
    for _, leader_fn, validator_fn in world.vm._captured_validators:
        for fn in (leader_fn, validator_fn):
            for cell in fn.__closure__ or ():
                assert isinstance(cell.cell_contents, (str, int, tuple, bool)) or callable(cell.cell_contents), \
                    f"closure captured non-plain data: {type(cell.cell_contents)}"
    world.vm.check_pickling = False


def test_views_return_valid_json_and_wei_amounts_are_strings(world):
    rid = world.forge()
    for text in (world.c.get_stats(), world.c.get_limits(), world.c.get_rule(rid), world.c.list_rules(1, 5),
                 world.c.list_cases(1, 5)):
        json.loads(text)
    rule = world.rule(rid)
    for key in ("penalty_unit", "filing_deposit", "enlist_stake", "bond", "bond_remaining"):
        assert isinstance(rule[key], str) and rule[key].isdigit()   # no JS precision loss for 1e18-sized wei
    stats = world.stats()
    assert isinstance(stats["escrow_total"], str) and isinstance(stats["claimable_total"], str)
    assert world.c.get_claimable(world.w(0)) == "0"
