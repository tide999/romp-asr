import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from romp_asr.policy_v02 import CompileError, compile_policy, evaluate
from romp_asr.policy_tools import (
    OperationAdapter, PolicyExecutor, charge_adapter, delete_adapter,
)
from romp_asr.tool_backends import LocalPaymentLedger, NoEffectError, PosixFileTools


def policy_document():
    return {
        "version": "romp-mu++/0.2",
        "types": {"operation": "string", "amount_minor": "int",
                  "candidate_total_minor": "int", "target_protected": "bool"},
        "default": "REJECT",
        "rules": [
            {"id": "budget", "kind": "hard", "if": [">", "candidate_total_minor", 100],
             "then": "REJECT"},
            {"id": "protected", "kind": "hard", "if": ["=", "target_protected", True],
             "then": "REJECT"},
            {"id": "charge", "kind": "soft", "if": ["=", "operation", "Charge"],
             "then": "ALLOW", "weight": 1, "priority": 0},
            {"id": "delete", "kind": "soft", "if": ["=", "operation", "Delete"],
             "then": "ALLOW", "weight": 1, "priority": 0},
        ],
        "order": {"precedence": [["REJECT", "ESCALATE"], ["ESCALATE", "ALLOW"]]},
        "limits": {"max_rules": 64, "max_depth": 6, "max_nodes": 1024},
    }


def compiled(doc=None):
    return compile_policy(json.dumps(doc if doc is not None else policy_document()))


def context(**kwargs):
    return {"operation": "Charge", "amount_minor": 60,
            "candidate_total_minor": 60, "target_protected": False, **kwargs}


def test_hard_reject_overrides_soft_votes_and_priority():
    doc = policy_document()
    for i in range(20):
        doc["rules"].append({"id": f"vote{i}", "kind": "soft",
                             "if": ["=", "operation", "Charge"], "then": "ALLOW",
                             "weight": 1000000, "priority": 2**31 - 1})
    result = evaluate(compiled(doc), context(candidate_total_minor=101))
    assert result.decision == "REJECT"
    assert result.hard_ids == ("budget",)
    assert result.scores == ()


@pytest.mark.parametrize("changes", [{"amount_minor": True}, {"amount_minor": 2**63},
                                   {"operation": 1}, {"target_protected": "false"},
                                   {"operation": "x" * 1025}])
def test_invalid_context_rejected(changes):
    assert evaluate(compiled(), context(**changes)).reason == "INVALID_CONTEXT"


def test_missing_context_under_not_does_not_allow():
    doc = policy_document()
    doc["rules"][2]["if"] = ["NOT", ["=", "operation", "Delete"]]
    value = context()
    del value["operation"]
    assert evaluate(compiled(doc), value).decision == "REJECT"


@pytest.mark.parametrize("value", [None, [], {"extra": 1}, context(operation="\ud800")])
def test_arbitrary_invalid_context(value):
    assert evaluate(compiled(), value).reason == "INVALID_CONTEXT"


def test_large_context_and_structured_policy_rejected():
    doc = policy_document()
    doc["types"] = {f"field{i}": "string" for i in range(64)}
    doc["rules"] = []
    assert evaluate(compiled(doc), {k: "x" * 1024 for k in doc["types"]}).reason == "INVALID_CONTEXT"
    for value in ({"version": []}, {"types": []}):
        with pytest.raises(CompileError):
            compiled(value)


def test_normalization_is_idempotent_and_preserves_decision():
    doc = policy_document()
    atom = ["=", "operation", "Charge"]
    doc["rules"][2]["if"] = ["AND", atom, ["AND", atom, [">", "amount_minor", 0]]]
    first = compiled(doc)
    assert compile_policy(first.canonical) == first
    reordered = copy.deepcopy(doc)
    reordered["rules"].reverse()
    reordered["rules"][1]["if"] = ["AND", [">", "amount_minor", 0], atom]
    assert compiled(reordered) == first
    assert evaluate(first, context()) == evaluate(first, context())
    assert evaluate(first, context()).decision == "ALLOW"


def test_in_normalization_and_duplicate_votes_preserved():
    doc = policy_document()
    doc["rules"][2]["if"] = ["IN", "operation", ["Charge", "Delete", "Charge"]]
    doc["rules"].append({**doc["rules"][2], "id": "second"})
    policy = compiled(doc)
    assert compile_policy(policy.canonical) == policy
    assert dict(evaluate(policy, context()).scores)["ALLOW"] == 2


@pytest.mark.parametrize("kind", ["score", "priority", "precedence", "unresolved", "default"])
def test_soft_resolution(kind):
    doc = policy_document()
    doc["rules"] = [
        {"id": "a", "kind": "soft", "if": ["=", "operation", "Charge"],
         "then": "ALLOW", "weight": 1, "priority": 0},
        {"id": "r", "kind": "soft", "if": ["=", "operation", "Charge"],
         "then": "REJECT", "weight": 1, "priority": 0},
    ]
    expected = "REJECT"
    if kind == "score":
        doc["rules"][0]["weight"] = 2
        expected = "ALLOW"
    if kind == "priority":
        doc["rules"][0]["priority"] = 1
        expected = "ALLOW"
    if kind == "unresolved":
        doc["order"]["precedence"] = []
        expected = "ESCALATE"
    if kind == "default":
        doc["rules"] = []
    assert evaluate(compiled(doc), context()).decision == expected


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(default="ALLOW"),
    lambda d: d["rules"][0].update(then="ALLOW"),
    lambda d: d["rules"][0].update(weight=1),
    lambda d: d["rules"][2].update(weight=True),
    lambda d: d["rules"][1].update(id="budget"),
    lambda d: d["rules"][0].update(**{"if": [">", "amount_minor", True]}),
    lambda d: d["rules"][0].update(**{"if": ["=", "missing", 1]}),
    lambda d: d["order"].update(precedence=[["ALLOW", "REJECT"], ["REJECT", "ALLOW"]]),
    lambda d: d["limits"].update(max_rules=65),
    lambda d: d["limits"].update(max_depth=1),
    lambda d: d["limits"].update(max_nodes=1),
])
def test_compile_validation(mutation):
    doc = policy_document()
    mutation(doc)
    if doc["limits"]["max_depth"] == 1:
        doc["rules"][0]["if"] = ["NOT", doc["rules"][0]["if"]]
    with pytest.raises(CompileError):
        compiled(doc)


@pytest.mark.parametrize("source", [
    '{"version":1,"version":2}', "[" * 100 + "]" * 100,
    " " * 65537, '{"x":NaN}', '{"x":"\\ud800"}',
], ids=["duplicate", "deep", "large", "nan", "surrogate"])
def test_invalid_encoding(source):
    with pytest.raises(CompileError):
        compile_policy(source)


def test_yaml_same_semantics_and_duplicate_alias_rejection():
    yaml = pytest.importorskip("yaml")
    doc = policy_document()
    assert compile_policy(yaml.safe_dump(doc), "yaml") == compiled(doc)
    for source in ("x: 1\nx: 2", "x: &a [1]\ny: *a", "x: !!python/object:os.system {}"):
        with pytest.raises(CompileError):
            compile_policy(source, "yaml")


def test_generic_charge_restart_concurrency_and_dsl_change(tmp_path):
    ledger = LocalPaymentLedger(tmp_path / "payment.db")
    adapters = {"Charge": charge_adapter(ledger)}
    first = PolicyExecutor(compiled(), tmp_path / "requests.db", adapters)
    second = PolicyExecutor(compiled(), tmp_path / "requests.db", adapters)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(e.submit, name, name, "Charge", {"amount_minor": 60})
                   for e, name in ((first, "a"), (second, "b"))]
        results = [f.result().outcome for f in futures]
    assert sorted(results) == ["REJECT", "SUCCEEDED"]
    assert ledger.total == 60
    winner = "a" if results[0] == "SUCCEEDED" else "b"
    assert second.submit(winner, winner, "Charge", {"amount_minor": 60}).outcome == "SUCCEEDED"
    assert ledger.total == 60
    assert second.submit("c", "c", "Charge", {"amount_minor": 40}).outcome == "SUCCEEDED"
    assert ledger.total == 100
    doc = policy_document()
    doc["rules"][0]["if"][2] = 50
    with pytest.raises(ValueError, match="binding"):
        PolicyExecutor(compiled(doc), tmp_path / "requests.db", adapters)
    other = PolicyExecutor(compiled(doc), tmp_path / "other.db", adapters)
    assert other.submit("d", "d", "Charge", {"amount_minor": 60}).outcome == "REJECT"


@pytest.mark.parametrize("effect", [False, True])
def test_generic_unknown_holds_capacity(tmp_path, effect):
    ledger = LocalPaymentLedger(tmp_path / "payment.db")

    class Backend:
        calls = 0

        def charge(self, identifier, amount):
            self.calls += 1
            if effect:
                ledger.charge(identifier, amount)
            raise TimeoutError()

    backend = Backend()
    adapters = {"Charge": charge_adapter(backend)}
    executor = PolicyExecutor(compiled(), tmp_path / "requests.db", adapters)
    assert executor.submit("a", "a", "Charge", {"amount_minor": 60}).outcome == "UNKNOWN"
    restarted = PolicyExecutor(compiled(), tmp_path / "requests.db", adapters)
    assert restarted.submit("a", "a", "Charge", {"amount_minor": 60}).outcome == "UNKNOWN"
    assert restarted.submit("b", "b", "Charge", {"amount_minor": 50}).outcome == "REJECT"
    assert backend.calls == 1
    assert ledger.total == (60 if effect else 0)


def test_generic_no_effect_failure_releases(tmp_path):
    class Backend:
        def charge(self, identifier, amount):
            if identifier == "a":
                raise NoEffectError("no effect")

    executor = PolicyExecutor(compiled(), tmp_path / "requests.db",
                              {"Charge": charge_adapter(Backend())})
    assert executor.submit("a", "a", "Charge", {"amount_minor": 60}).outcome == "FAILED"
    assert executor.submit("b", "b", "Charge", {"amount_minor": 100}).outcome == "SUCCEEDED"


def test_generic_pending_and_escalation_do_not_invoke(tmp_path):
    calls = []
    doc = policy_document()
    doc["rules"][2]["then"] = "ESCALATE"
    adapter = charge_adapter(type("Backend", (), {
        "charge": lambda self, identifier, amount: calls.append(identifier)})())
    executor = PolicyExecutor(compiled(doc), tmp_path / "requests.db", {"Charge": adapter})
    assert executor.submit("a", "a", "Charge", {"amount_minor": 60}).outcome == "ESCALATE"
    assert calls == []
    import sqlite3
    from romp_asr.policy_v02 import canonical_json

    payload = canonical_json(["a", "Charge", {"amount_minor": 60}])
    with sqlite3.connect(executor.database) as db:
        db.execute("INSERT INTO operations VALUES (?,?,60,'PENDING','interrupted','{}')",
                   ("a", payload))
    assert executor.submit("a", "a", "Charge", {"amount_minor": 60}).outcome == "UNKNOWN"
    assert calls == []


def test_third_operation_uses_same_engine_without_business_branches(tmp_path):
    doc = policy_document()
    doc["types"] = {"operation": "string", "domain": "string"}
    doc["rules"] = [
        {"id": "domain_guard", "kind": "hard", "if": ["NOT", ["IN", "domain", ["company.test"]]],
         "then": "REJECT"},
        {"id": "send", "kind": "soft", "if": ["=", "operation", "Send"],
         "then": "ALLOW", "weight": 1, "priority": 0},
    ]
    calls = []
    adapter = OperationAdapter(
        lambda subject, args, used: ({"operation": "Send", "domain": args["domain"]}, 0),
        lambda identifier, args: calls.append(identifier), "send-test-v1")
    executor = PolicyExecutor(compiled(doc), tmp_path / "requests.db", {"Send": adapter})
    assert executor.submit("a", "a", "Send", {"domain": "outside.test"}).outcome == "REJECT"
    assert executor.submit("b", "b", "Send", {"domain": "company.test"}).outcome == "SUCCEEDED"
    assert calls == ["b"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX backend")
@pytest.mark.parametrize("alias", ["symbolic", "hard", "parent", "spoof"])
def test_generic_file_protection_and_aliases(tmp_path, alias):
    workspace, system = tmp_path / "workspace", tmp_path / "system"
    workspace.mkdir()
    system.mkdir()
    (workspace / "draft").write_text("ordinary")
    (system / "core").write_text("protected")
    if alias == "hard":
        os.link(system / "core", workspace / "alias")
    else:
        (workspace / "alias").symlink_to(system / "core")
    files = PosixFileTools({"workspace": workspace, "system": system})
    try:
        executor = PolicyExecutor(compiled(), tmp_path / "requests.db",
                                  {"Delete": delete_adapter(files, ["system"])})
        assert executor.submit("a", "a", "Delete", {"root": "system", "path": "core"}).outcome == "REJECT"
        assert (system / "core").exists()
        args = {"root": "workspace", "path": "../system/core" if alias == "parent" else "alias"}
        if alias == "spoof":
            args["target_protected"] = False
        assert executor.submit("b", "b", "Delete", args).outcome == "REJECT"
        assert executor.submit("c", "c", "Delete", {"root": "workspace", "path": "draft"}).outcome == "SUCCEEDED"
        assert not (workspace / "draft").exists()
    finally:
        files.close()
