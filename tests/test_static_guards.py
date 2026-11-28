"""Source-level guards. They need no GenVM and run in milliseconds.

They pin the exact-equality law into the code shape: the validator may compare the decision fields
only with ``==``; no ordering, no ``abs``, no counting, no model-based comparison helpers.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "contracts" / "Litmus.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
RUNNER_HASH = "kzybg95x1gn21dw72m7hyg0maaap04n4nx55j9yt6fkdqxtz6bz0"   # py-genlayer runner of the GenVM v0.4 executor line (Studio v0.123)
LEGACY_V02_HASH = "1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6"


def function(name, within=None):
    for node in ast.walk(within or TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def test_header_pins_the_genvm_v0_4_runner():
    lines = SOURCE.splitlines()
    assert lines[0] == "# v0.4.0", lines[0]
    assert lines[1] == f'# {{ "Depends": "py-genlayer:{RUNNER_HASH}" }}', lines[1]
    assert "latest" not in lines[1] and "test" not in lines[1].split(":")[-1]
    assert LEGACY_V02_HASH not in SOURCE


def test_contract_source_is_english_ascii_only():
    assert SOURCE.isascii(), "contract must contain plain ASCII only"
    for path in list(ROOT.glob("**/*")):
        if path.is_file() and path.suffix in {".py", ".md", ".mjs", ".sh", ".json", ".yml", ".yaml", ".txt"} \
                and ".git" not in path.parts and "node_modules" not in path.parts and ".genvmroot" not in path.parts:
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"[\u0600-\u06FF\u0750-\u077F]", text), f"non-English (Arabic-script) text in {path}"


def test_validator_compares_only_with_equality():
    validator = function("validator", function("judge"))
    forbidden_calls = {"abs", "round", "sum", "min", "max", "len", "int", "float", "set", "sorted", "count"}
    forbidden_ops = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)
    for node in ast.walk(validator):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden_calls, f"validator calls {node.func.id}()"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"count", "index", "get"}, f"validator calls .{node.func.attr}()"
        if isinstance(node, ast.Compare):
            for op in node.ops:
                assert not isinstance(op, forbidden_ops), "validator uses a non-equality comparison"
    equalities = [n for n in ast.walk(validator) if isinstance(n, ast.Compare) and any(isinstance(o, ast.Eq) for o in n.ops)]
    rendered = " ".join(ast.unparse(n) for n in equalities)
    rendered = rendered.replace("'", '"')
    assert 'proposed["verdict"] == mine["verdict"]' in rendered
    assert 'proposed["factors"] == mine["factors"]' in rendered


def test_validator_has_no_arithmetic_on_decision_fields():
    validator = function("validator", function("judge"))
    for node in ast.walk(validator):
        assert not isinstance(node, ast.BinOp), f"arithmetic in validator: {ast.unparse(node)}"
        if isinstance(node, ast.UnaryOp):
            assert isinstance(node.op, ast.Not), f"arithmetic in validator: {ast.unparse(node)}"


def test_no_model_based_or_lenient_equivalence_helpers():
    assert "eq_principle" not in SOURCE, "eq_principle helpers compare leniently; use the explicit exact validator"
    assert "prompt_comparative" not in SOURCE and "prompt_non_comparative" not in SOURCE
    assert "tolerance" not in SOURCE.lower().replace("no tolerance", "")


def test_nondeterminism_is_confined():
    calls = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    for call in calls:
        text = ast.unparse(call.func)
        if text.endswith("exec_prompt"):
            owner = [f for f in ast.walk(TREE) if isinstance(f, ast.FunctionDef) and call in list(ast.walk(f))
                     and f.name == "assess"]
            assert owner, "exec_prompt may only be called inside assess()"
        if text.endswith("run_nondet"):
            owner = [f for f in ast.walk(TREE) if isinstance(f, ast.FunctionDef) and call in list(ast.walk(f))
                     and f.name == "judge"]
            assert owner, "run_nondet may only be called inside judge()"
    assert SOURCE.count("exec_prompt(") == 1 and SOURCE.count("run_nondet(") == 1


def test_nondeterministic_closures_do_not_touch_storage_or_time():
    judge = function("judge")
    text = ast.unparse(judge)
    for banned in ("self.", "datetime", "gl.message", "emit_transfer", "gl.storage"):
        assert banned not in text, f"judge() must not use {banned}"


def test_every_model_verdict_goes_through_the_single_judge_path():
    callers = []
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "judge":
                    callers.append(node.name)
    assert sorted(set(callers)) == ["appeal_case", "rule_case", "run_probe"]


def test_state_is_written_after_the_model_call_in_every_judging_method():
    """A failed model call must leave storage untouched, even in Direct Mode where there is no rollback."""
    mutators = ("self._save_", "self._credit", "self._receive", "self.rule_count", "self.case_count",
                "self.escrow_total", "self.claimable")
    for name in ("run_probe", "rule_case", "appeal_case"):
        body = ast.unparse(function(name)).splitlines()
        judge_line = next(i for i, line in enumerate(body) if "judge(" in line)
        for line in body[:judge_line]:
            assert not any(m in line for m in mutators), f"{name} mutates state before judge(): {line.strip()}"


# ------------------------------------------------------------------------------------- GenVM v0.4 rules
STORAGE_TYPES = {"u8", "u16", "u32", "u64", "u128", "u256", "i8", "i16", "i32", "i64", "i128", "i256", "bigint",
                 "TreeMap", "DynArray", "Array", "Address", "bool", "str", "bytes"}


def contract_class():
    return next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Litmus")


def test_v0_4_imports_and_base_class():
    """`from genlayer import *` no longer binds `gl` in v0.4; the base class lives in gl.contract."""
    imports = [ast.unparse(n) for n in TREE.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert "import genlayer as gl" in imports
    assert "from genlayer.types import *" in imports and "from genlayer.storage import TreeMap" in imports
    assert "from genlayer import *" not in imports
    assert [ast.unparse(b) for b in contract_class().bases] == ["gl.contract.Contract"]


def test_no_removed_v0_2_or_v0_3_apis():
    for banned in ("salt_nonce", "__receive__", "gl.Contract", "gl.get_contract_at", "gl.ContractAt", "gl.deploy_contract",
                   "gl.contract.get_at", "allow_storage", "run_nondet_unsafe", "gl.exec_prompt", "gl.get_webpage"):
        assert banned not in SOURCE, f"{banned} was removed in GenVM v0.4"
    assert "leader_result.message" not in SOURCE, "gl.vm.UserError carries .data in v0.4, not .message"


def test_special_methods_carry_no_public_decorator():
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("__") and node.name.endswith("__"):
            assert not node.decorator_list, f"{node.name} must not have decorators in v0.4"


def test_value_is_only_accepted_by_named_payable_methods_and_only_paid_by_withdraw():
    payable = sorted(n.name for n in ast.walk(TREE) if isinstance(n, ast.FunctionDef)
                     and any(ast.unparse(d) == "gl.public.write.payable" for d in n.decorator_list))
    assert payable == ["appeal_case", "enlist", "file_case", "forge_rule"]
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "emit_transfer":
            assert ast.unparse(node.func.value).startswith("gl.chain.Account("), ast.unparse(node)
            owner = [f for f in ast.walk(TREE) if isinstance(f, ast.FunctionDef) and node in list(ast.walk(f))]
            assert any(f.name == "withdraw" for f in owner), "emit_transfer may only be called inside withdraw()"
    assert SOURCE.count("emit_transfer(") == 1


def test_persistent_fields_use_genvm_storage_types_only():
    fields = [n for n in contract_class().body if isinstance(n, ast.AnnAssign)]
    assert fields, "contract declares no storage"
    for field in fields:
        root = field.annotation
        while isinstance(root, ast.Subscript):
            root = root.value
        assert isinstance(root, ast.Name) and root.id in STORAGE_TYPES, f"{field.target.id}: {ast.unparse(field.annotation)}"
        assert root.id not in {"int", "float", "list", "dict", "set", "tuple"}


def test_nondeterministic_blocks_read_no_storage():
    """Closures only capture plain data, so no gl.storage.copy_to_memory() is needed (and none may be added silently)."""
    assert "copy_to_memory" not in SOURCE
    judge = function("judge")
    captured = {a.arg for a in judge.args.args}
    assert captured == {"prompt", "n_factors", "allowed"}
    for node in ast.walk(judge):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert node.value.id != "self", "judge() must not read contract storage"
