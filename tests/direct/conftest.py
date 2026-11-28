"""Shared Direct Mode helpers for the Litmus test-suite (real GenVM v0.4 SDK from the Studio v0.123 release, no RPC)."""
import json
import sys

import pytest

CONTRACT = "contracts/Litmus.py"
BASE_TIME = "2026-11-01T12:00:00+00:00"
BASE_TS = 1793534400  # 2026-11-01T12:00:00Z

UNIT = 1000          # penalty unit (wei)
DEPOSIT = 100        # filing / appeal deposit
STAKE = 9000         # enlist stake: three concurrent maximum penalties (3 x 3 x UNIT)
BOND = 6000          # author bond

RULE_TEXT = ("Members must not advertise paid products or services in the general channel. "
             "Sharing free resources is allowed.")
AGGRAVATORS = ["The member had already been warned about promotion before.",
                "The member tagged or mentioned many people in the same message."]

P_BREACH_PLAIN = "A member posted a link to a paid video course in the general channel."
P_BREACH_BOTH = "A member who was warned twice before tagged all members to sell a paid course."
P_OK_FREE = "A member shared a free open-source tutorial in the general channel."
P_OK_BLOG = "A member answered a question using their own free blog post."

PROBES = [
    {"scenario": P_BREACH_PLAIN, "expected": "BREACH", "factors": [False, False]},
    {"scenario": P_BREACH_BOTH, "expected": "BREACH", "factors": [True, True]},
    {"scenario": P_OK_FREE, "expected": "COMPLIANT", "factors": [False, False]},
    {"scenario": P_OK_BLOG, "expected": "COMPLIANT", "factors": [False, False]},
]

S_PLAIN = "On Monday the member posted a link to buy their paid design course in the general channel."
S_BOTH = "The member, already warned last week, tagged forty people to sell their paid design course."
S_FREE = "The member posted a link to a free community map in the general channel."
S_VAGUE = "Someone says the member promoted something but does not say what."


def R(verdict, *flags, reason="Because the rule text says so."):
    """Build a ruling payload as an LLM would return it."""
    return {"verdict": verdict, "factors": list(flags), "reason": reason}


def llm(vm, *pairs):
    """Replace all LLM mocks. Earlier pairs win (first-match semantics)."""
    vm.clear_mocks()
    for pattern, ruling in pairs:
        vm.mock_llm(pattern, ruling if isinstance(ruling, str) else json.dumps(ruling))


PROBE_PAIRS = [
    (r"warned twice before tagged all members", R("BREACH", True, True)),
    (r"paid video course in the general", R("BREACH", False, False)),
    (r"free open-source tutorial", R("COMPLIANT", False, False)),
    (r"free blog post", R("COMPLIANT", False, False)),
]


class World:
    def __init__(self, vm, accounts, contract):
        self.vm, self.accounts, self.c = vm, accounts, contract
        self.inflow = 0
        self.transfers = []

    def w(self, i):
        return "0x" + bytes(self.accounts[i]).hex()

    def at(self, seconds):
        from datetime import datetime, timedelta, timezone
        stamp = datetime.fromtimestamp(BASE_TS + seconds, tz=timezone.utc).isoformat()
        self.vm.warp(stamp)

    def call(self, who, fn, *args, value=0):
        self.vm.sender = self.accounts[who]
        self.vm.value = value
        try:
            result = getattr(self.c, fn)(*args)
        finally:
            self.vm.value = 0
        self.inflow += value
        return result

    def fail(self, who, fn, *args, value=0, match=None):
        before_inflow = self.inflow
        with self.vm.expect_revert(match):
            self.vm.sender = self.accounts[who]
            self.vm.value = value
            try:
                getattr(self.c, fn)(*args)
            finally:
                self.vm.value = 0
        assert self.inflow == before_inflow

    def view(self, fn, *args):
        raw = getattr(self.c, fn)(*args)
        return json.loads(raw) if raw and raw[0] in "[{n" else raw

    def rule(self, rid=1):
        return self.view("get_rule", rid)

    def case(self, cid=1):
        return self.view("get_case", cid)

    def member(self, rid, who):
        return self.view("get_member", rid, self.w(who))

    def claimable(self, who):
        return int(self.c.get_claimable(self.w(who)))

    def stats(self):
        return self.view("get_stats")

    def forge(self, who=0, *, text=RULE_TEXT, aggravators=None, probes=None, unit=UNIT, deposit=DEPOSIT,
              stake=STAKE, strikes=3, pass_percent=100, defense_minutes=2, appeal_minutes=2, bond=BOND,
              title="No paid promotion"):
        aggravators = AGGRAVATORS if aggravators is None else aggravators
        probes = PROBES if probes is None else probes
        return self.call(who, "forge_rule", title, text, json.dumps(aggravators), json.dumps(probes),
                         unit, deposit, stake, strikes, pass_percent, defense_minutes, appeal_minutes,
                         value=bond)

    def certify(self, who=0, rid=1, pairs=None):
        llm(self.vm, *(PROBE_PAIRS if pairs is None else pairs))
        for index in range(len(self.rule(rid)["probes"])):
            self.call(who, "run_probe", rid, index)
        self.call(who, "certify_rule", rid)

    def certified_rule(self, **kwargs):
        rid = self.forge(**kwargs)
        self.certify(rid=rid)
        return rid

    def enlist(self, who, rid=1):
        self.call(who, "enlist", rid, value=int(self.rule(rid)["enlist_stake"]))

    def file(self, who, defendant, statement, rid=1):
        return self.call(who, "file_case", rid, self.w(defendant), statement,
                         value=int(self.rule(rid)["filing_deposit"]))

    def audit(self):
        """Recompute every liability from raw records and compare with the counters."""
        stats = self.stats()
        rules = self.view("list_rules", 1, 10) if stats["rules"] else []
        cases = []
        for start in range(1, stats["cases"] + 1, 10):
            cases += self.view("list_cases", start, 10)
        held = 0
        members = {}
        for rule in rules:
            held += int(rule["bond_remaining"])
            for wallet_index in range(len(self.accounts)):
                member = self.view("get_member", rule["id"], self.w(wallet_index))
                if member:
                    members[(rule["id"], wallet_index)] = member
                    held += int(member["stake"])
        open_by_rule, open_by_member, locked = {}, {}, {}
        for case in cases:
            if case["status"] != "SETTLED":
                held += int(case["deposit"])
                open_by_rule[case["rule_id"]] = open_by_rule.get(case["rule_id"], 0) + 1
                for role in ("plaintiff", "defendant"):
                    key = (case["rule_id"], case[role])
                    open_by_member[key] = open_by_member.get(key, 0) + 1
                key = (case["rule_id"], case["defendant"])
                locked[key] = locked.get(key, 0) + int(case["reserve"])
        total_claimable = sum(self.claimable(i) for i in range(len(self.accounts)))
        assert total_claimable == int(stats["claimable_total"]), "claimable ledger drifted"
        held += total_claimable
        assert held == int(stats["escrow_total"]), f"escrow {stats['escrow_total']} != recomputed {held}"
        paid_out = sum(t["value"] for t in self.transfers)
        assert int(stats["escrow_total"]) == self.inflow - paid_out, "inflow - outflow != escrow"
        for rule in rules:
            assert rule["open_cases"] == open_by_rule.get(rule["id"], 0)
            assert rule["members"] == sum(1 for (rid, _), m in members.items()
                                          if rid == rule["id"] and m["status"] == "ACTIVE")
            settled = rule["breaches"] + rule["dismissed"] + rule["inconclusive"] + rule["voided"]
            assert settled + rule["open_cases"] == rule["cases"]
        for (rid, idx), member in members.items():
            wallet = self.w(idx)
            assert member["open_cases"] == open_by_member.get((rid, wallet), 0)
            assert int(member["locked"]) == locked.get((rid, wallet), 0)
            assert int(member["stake"]) >= int(member["locked"])
        return stats


import contextlib


@contextlib.contextmanager
def build_world(direct_deploy, direct_vm, direct_accounts, path=CONTRACT):
    """Deploy ``path`` in Direct Mode and wrap it with accounting spies (reused by mutation tests)."""
    contract = direct_deploy(path)
    direct_vm.warp(BASE_TIME)
    direct_vm.deal(direct_vm._contract_address, 10 ** 30)
    w = World(direct_vm, direct_accounts, contract)

    import _genlayer_wasi as wasi
    from genlayer.py import calldata
    original = wasi.gl_call

    def spy(data, /):
        request = calldata.decode(data)
        if isinstance(request, dict) and "PostMessage" in request:
            message = request["PostMessage"]
            address = message["address"]
            hexed = (address.as_hex if hasattr(address, "as_hex") else "0x" + bytes(address).hex()).lower()
            body = message.get("calldata")
            # the v0.6 calldata ABI names the method key "" (older ABIs used "method"); a plain value transfer has none
            method = (body.get("method", body.get("")) if isinstance(body, dict) else None)
            w.transfers.append({"to": hexed, "value": int(message["value"]), "method": method, "on": message.get("on")})
        return original(data)

    wasi.gl_call = spy
    try:
        yield w
    finally:
        wasi.gl_call = original


@pytest.fixture
def world(direct_deploy, direct_vm, direct_accounts):
    with build_world(direct_deploy, direct_vm, direct_accounts) as w:
        yield w
