"""Language and runtime checks for declarative vector admission."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from romp_asr import LocalPaymentLedger, NoEffectError, PosixFileTools
from romp_asr.policy_v02 import CompileError, canonical_json
from romp_asr.policy_v03 import admit, compile_state_policy, MAX_INT
from romp_asr.state_effect_tools import StateEffectAdapter, StateEffectExecutor


def document():
    return json.loads((Path(__file__).resolve().parents[1] /
                       "examples/policy_v03.json").read_text(encoding="utf-8"))


def compiled(doc=None):
    return compile_state_policy(canonical_json(document() if doc is None else doc))


def facts(subject, args):
    if set(args) != {"amount_minor"}:
        raise ValueError("invalid arguments")
    return {"amount_minor": args["amount_minor"], "target_protected": False}


def executor(tmp_path, invoke=lambda identifier, args: None, doc=None):
    return StateEffectExecutor(compiled(doc), tmp_path / "requests.db",
                               {"Charge": StateEffectAdapter(facts, invoke, "charge-bounds-v3")})


@pytest.mark.parametrize("patch", [
    lambda d: d.update(version="romp-mu++/0.2"),
    lambda d: d["states"]["spent"].update(type="balance"),
    lambda d: d["states"]["spent"].update(initial=0),
    lambda d: d["states"].update(unbounded={"type": "gross"}),
    lambda d: d["operations"]["Charge"]["effects"].update(missing=["const", 1]),
    lambda d: d["operations"]["Charge"]["effects"].update(spent=["field", "missing"]),
    lambda d: d["operations"]["Charge"]["effects"].update(spent=["field", "target_protected"]),
    lambda d: d["operations"]["Charge"]["effects"].update(spent=["const", -1]),
    lambda d: d["operations"]["Charge"]["effects"].update(spent=["const", True]),
    lambda d: d["operations"]["Charge"]["effects"].update(spent=["const", 2**63]),
    lambda d: d["operations"]["Charge"]["effects"].update(spent=["add", 1, 2]),
    lambda d: d["invariants"][0].update(max=True),
    lambda d: d["invariants"][0].update(state="missing"),
    lambda d: d["invariants"][0].update(id="charge"),
    lambda d: d["invariants"].append(copy.deepcopy(d["invariants"][0])),
    lambda d: d["types"].pop("operation"),
    lambda d: d.update(extra="unknown"),
    lambda d: d.update(states={}),
])
def test_invalid_declarations(patch):
    doc = document()
    patch(doc)
    with pytest.raises(CompileError):
        compiled(doc)


def test_canonical_order_and_duplicate_keys():
    doc = document()
    policy = compiled(doc)
    doc["invariants"].reverse()
    doc["rules"].reverse()
    assert compiled(doc).policy_hash == policy.policy_hash
    with pytest.raises(CompileError):
        compile_state_policy('{"version":"a","version":"b"}')


def test_candidate_vector_and_hard_precedence():
    policy = compiled()
    result = admit(policy, "Charge", facts("", {"amount_minor": 50}),
                   {"spent": 60, "charge_count": 1})
    assert result.decision == "REJECT"
    assert result.violated == ("budget",)
    assert result.candidate == {"spent": 110, "charge_count": 2}
    result = admit(policy, "Charge", facts("", {"amount_minor": 1}),
                   {"spent": 0, "charge_count": 2})
    assert result.violated == ("charge_limit",)
    assert admit(policy, "Delete", {"amount_minor": 0, "target_protected": True},
                 {"spent": 0, "charge_count": 0}).reason == "HARD_REJECT"


@pytest.mark.parametrize("value", [-1, True, 1.5, "60", 2**63])
def test_invalid_effect_or_fact_rejects(value):
    result = admit(compiled(), "Charge", facts("", {"amount_minor": value}),
                   {"spent": 0, "charge_count": 0})
    assert result.decision == "REJECT"


def test_spoofed_or_missing_state_and_operation():
    policy = compiled()
    valid = facts("", {"amount_minor": 1})
    for held in ({}, {"spent": 0}, {"spent": -1, "charge_count": 0},
                 {"spent": 0, "charge_count": True}):
        assert admit(policy, "Charge", valid, held).decision == "REJECT"
    assert admit(policy, "Charge", {**valid, "operation": "Delete"},
                 {"spent": 0, "charge_count": 0}).decision == "REJECT"
    assert admit(policy, "Unknown", valid, {"spent": 0, "charge_count": 0}).decision == "REJECT"
    assert admit(policy, "Charge", {"amount_minor": 1},
                 {"spent": 0, "charge_count": 0}).reason == "INVALID_CONTEXT"


def test_overflow_rejected():
    assert admit(compiled(), "Charge", facts("", {"amount_minor": 1}),
                 {"spent": MAX_INT, "charge_count": 0}).reason == "STATE_OVERFLOW"


def test_change_effect_declaration_without_adapter_changes(tmp_path):
    doc = document()
    doc["operations"]["Charge"]["effects"]["charge_count"] = ["const", 2]
    engine = executor(tmp_path, doc=doc)
    assert engine.submit("a", "host", "Charge", {"amount_minor": 1}).outcome == "SUCCEEDED"
    assert engine.snapshot()["held"] == {"spent": 1, "charge_count": 2}
    assert engine.submit("b", "host", "Charge", {"amount_minor": 1}).outcome == "REJECT"
    assert len(engine.snapshot()["requests"]) == 1


def test_development_cli_persists_state_between_processes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = root / "examples/policy_v03_demo.py"
    base = [sys.executable, str(script), "--state", str(tmp_path / "scope")]

    def run(*arguments):
        result = subprocess.run([*base, *arguments], capture_output=True,
                                text=True, encoding="utf-8", timeout=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert run("--id", "a", "--amount", "60")["result"]["outcome"] == "SUCCEEDED"
    assert run("--id", "b", "--amount", "50")["result"]["outcome"] == "REJECT"
    retry = run("--id", "a", "--amount", "60")
    assert retry["result"]["outcome"] == "SUCCEEDED"
    assert retry["payment_total_minor"] == 60
    assert run()["held"] == {"spent": 60, "charge_count": 1}


def test_actual_payment_atomic_vector_and_restart(tmp_path):
    ledger = LocalPaymentLedger(tmp_path / "payments.db")
    calls = []

    def invoke(identifier, args):
        calls.append(identifier)
        ledger.charge(identifier, args["amount_minor"])

    engine = executor(tmp_path, invoke)
    assert engine.submit("a", "host", "Charge", {"amount_minor": 60}).outcome == "SUCCEEDED"
    assert engine.submit("b", "host", "Charge", {"amount_minor": 50}).outcome == "REJECT"
    assert engine.snapshot()["held"] == {"spent": 60, "charge_count": 1}
    restarted = executor(tmp_path, invoke)
    assert restarted.submit("a", "host", "Charge", {"amount_minor": 60}).outcome == "SUCCEEDED"
    assert restarted.submit("c", "host", "Charge", {"amount_minor": 1}).outcome == "SUCCEEDED"
    assert restarted.submit("d", "host", "Charge", {"amount_minor": 1}).outcome == "REJECT"
    assert ledger.total == 61
    assert calls == ["a", "c"]
    assert restarted.snapshot()["held"] == {"spent": 61, "charge_count": 2}
    assert restarted.submit("a", "other", "Charge", {"amount_minor": 60}).outcome == "REJECT"


@pytest.mark.parametrize("unknown", [True, False])
def test_unknown_retains_all_dimensions_and_no_effect_releases(tmp_path, unknown):
    invocations = []

    def invoke(identifier, args):
        invocations.append(identifier)
        if unknown:
            raise TimeoutError()
        raise NoEffectError("nothing happened")

    engine = executor(tmp_path, invoke)
    outcome = "UNKNOWN" if unknown else "FAILED"
    assert engine.submit("a", "host", "Charge", {"amount_minor": 60}).outcome == outcome
    assert executor(tmp_path, invoke).submit(
        "a", "host", "Charge", {"amount_minor": 60}).outcome == outcome
    assert invocations == ["a"]
    assert engine.snapshot()["held"] == ({"spent": 60, "charge_count": 1} if unknown
                                         else {"spent": 0, "charge_count": 0})
    assert engine.submit("b", "host", "Charge", {"amount_minor": 50}).outcome == (
        "REJECT" if unknown else "FAILED")


def test_distinct_id_concurrent_capacity(tmp_path):
    barrier = threading.Barrier(2)
    calls = []
    engines = [executor(tmp_path, lambda identifier, args: calls.append(identifier))
               for _ in range(2)]

    def run(index):
        barrier.wait(timeout=5)
        return engines[index].submit(str(index), "host", "Charge", {"amount_minor": 60}).outcome

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(run, range(2))) == ["REJECT", "SUCCEEDED"]
    assert len(calls) == 1
    assert engines[0].snapshot()["held"] == {"spent": 60, "charge_count": 1}


def test_same_id_concurrent_invokes_once(tmp_path):
    calls = []
    engine = executor(tmp_path, lambda identifier, args: calls.append(identifier))
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: engine.submit(
            "a", "host", "Charge", {"amount_minor": 60}), range(4)))
    assert all(r.outcome in ("SUCCEEDED", "UNKNOWN") for r in results)
    assert calls == ["a"]


def test_binding_and_legacy_scope_rejected(tmp_path):
    executor(tmp_path)
    changed = document()
    changed["invariants"][0]["max"] = 200
    with pytest.raises(ValueError, match="binding"):
        executor(tmp_path, doc=changed)
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE operations (id TEXT)")
    with pytest.raises(ValueError, match="migration"):
        StateEffectExecutor(compiled(), legacy,
                            {"Charge": StateEffectAdapter(facts, lambda *args: None, "legacy")})


def test_mutating_provider_rejected_before_invocation(tmp_path):
    calls = []

    def mutated(subject, args):
        args["amount_minor"] = 1
        return facts(subject, args)

    engine = StateEffectExecutor(compiled(), tmp_path / "requests.db",
                                 {"Charge": StateEffectAdapter(mutated,
                                  lambda *args: calls.append(args), "mutating")})
    assert engine.submit("a", "host", "Charge", {"amount_minor": 60}).outcome == "REJECT"
    assert not calls
    assert not engine.snapshot()["requests"]


def test_process_exit_retains_vector_and_does_not_reinvoke(tmp_path):
    script = """
import os
from pathlib import Path
import sqlite3
from romp_asr.policy_v03 import compile_state_policy
from romp_asr.state_effect_tools import StateEffectAdapter, StateEffectExecutor
base = Path(os.environ["TEST_SCOPE"])
policy = compile_state_policy(Path(os.environ["TEST_POLICY"]).read_bytes())
def facts(subject, args):
    return {"amount_minor": args["amount_minor"], "target_protected": False}
def invoke(identifier, args):
    with sqlite3.connect(base / "effects.db") as db:
        db.execute("CREATE TABLE effects (amount INTEGER)")
        db.execute("INSERT INTO effects VALUES (?)", (args["amount_minor"],))
    os._exit(17)
StateEffectExecutor(policy, base / "requests.db",
    {"Charge": StateEffectAdapter(facts, invoke, "charge-bounds-v3")}).submit(
    "a", "host", "Charge", {"amount_minor": 60})
"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root / "src"),
           "TEST_SCOPE": str(tmp_path), "TEST_POLICY": str(root / "examples/policy_v03.json")}
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, timeout=15)
    assert result.returncode == 17, result.stderr
    calls = []
    engine = executor(tmp_path, lambda *args: calls.append(args))
    assert engine.submit("a", "host", "Charge", {"amount_minor": 60}).outcome == "UNKNOWN"
    assert engine.submit("b", "host", "Charge", {"amount_minor": 50}).outcome == "REJECT"
    assert engine.snapshot()["held"] == {"spent": 60, "charge_count": 1}
    assert not calls
    with sqlite3.connect(tmp_path / "effects.db") as db:
        assert db.execute("SELECT amount FROM effects").fetchall() == [(60,)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file backend")
def test_actual_deletion_protected_by_hard_rule(tmp_path):
    roots = {name: tmp_path / name for name in ("workspace", "system")}
    for root in roots.values():
        root.mkdir()
        (root / "target").write_text("synthetic", encoding="utf-8")
    files = PosixFileTools(roots)

    def file_facts(subject, args):
        files.delete(args["root"], args["path"], validate_only=True)
        return {"amount_minor": 0, "target_protected": args["root"] == "system"}

    try:
        engine = StateEffectExecutor(compiled(), tmp_path / "requests.db",
            {"Delete": StateEffectAdapter(file_facts, lambda identifier, args:
             files.delete(args["root"], args["path"]), canonical_json(files.roots))})
        for root, expected in (("workspace", "SUCCEEDED"), ("system", "REJECT")):
            assert engine.submit(root, "host", "Delete",
                                 {"root": root, "path": "target"}).outcome == expected
        assert not (roots["workspace"] / "target").exists()
        assert (roots["system"] / "target").exists()
        assert engine.snapshot()["held"] == {"spent": 0, "charge_count": 0}
    finally:
        files.close()
