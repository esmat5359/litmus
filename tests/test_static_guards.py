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
RUNNER_HASH = "1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6"   # py-genlayer runner shipped with GenVM v0.2.16


def function(name, within=None):
    for node in ast.walk(within or TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def test_header_pins_the_genvm_v0_2_16_runner():
    first = SOURCE.splitlines()[0]
    assert first == f'# {{ "Depends": "py-genlayer:{RUNNER_HASH}" }}', first
    assert "latest" not in first and "test" not in first.split(":")[-1]


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
        if text.endswith("run_nondet_unsafe"):
            owner = [f for f in ast.walk(TREE) if isinstance(f, ast.FunctionDef) and call in list(ast.walk(f))
                     and f.name == "judge"]
            assert owner, "run_nondet_unsafe may only be called inside judge()"
    assert SOURCE.count("exec_prompt(") == 1 and SOURCE.count("run_nondet_unsafe(") == 1


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
