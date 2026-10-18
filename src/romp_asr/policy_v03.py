"""Bounded declarations of monotone state, effect bounds, and invariants."""

from dataclasses import dataclass
import hashlib
import json
import re

from .policy_v02 import (
    CompileError, CompiledPolicy, _keys, _load, canonical_json,
    compile_policy as compile_rules, evaluate,
)

VERSION = "romp-mu++/0.3"
MAX_INT = 2**63 - 1


def _name(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value)


def _amount(value):
    return type(value) is int and 0 <= value <= MAX_INT


@dataclass(frozen=True)
class StatePolicy:
    canonical: str
    policy_hash: str
    rules: CompiledPolicy


@dataclass(frozen=True)
class Admission:
    decision: str
    reason: str
    effects: dict
    candidate: dict
    violated: tuple = ()


def compile_state_policy(text, format="json"):
    try:
        return _compile(text, format)
    except CompileError:
        raise
    except (TypeError, ValueError, KeyError, RecursionError) as exc:
        raise CompileError("invalid state-effect policy") from exc


def _compile(text, format):
    doc = _load(text, format)
    _keys(doc, ("version", "types", "states", "operations", "invariants",
                "default", "rules", "order", "limits"))
    if doc["version"] != VERSION:
        raise CompileError("unsupported state-effect version")
    rule_doc = {k: doc[k] for k in ("types", "default", "rules", "order", "limits")}
    rule_doc["version"] = "romp-mu++/0.2"
    rules = compile_rules(canonical_json(rule_doc))
    normalized = json.loads(rules.canonical)
    if normalized["types"].get("operation") != "string":
        raise CompileError("operation must be a declared string")
    states = doc["states"]
    if type(states) is not dict or not 1 <= len(states) <= 16:
        raise CompileError("state count")
    for name, definition in states.items():
        if not _name(name):
            raise CompileError("invalid state name")
        _keys(definition, ("type",))
        if definition["type"] != "gross":
            raise CompileError("only monotone gross state is supported")
    operations = doc["operations"]
    if type(operations) is not dict or not 1 <= len(operations) <= 32:
        raise CompileError("operation count")
    for name, definition in operations.items():
        if not _name(name):
            raise CompileError("invalid operation name")
        _keys(definition, ("effects",))
        effects = definition["effects"]
        if type(effects) is not dict or not effects.keys() <= states.keys():
            raise CompileError("undeclared effect state")
        for expression in effects.values():
            if type(expression) is not list or len(expression) != 2:
                raise CompileError("invalid effect expression")
            op, value = expression
            if op == "const" and _amount(value):
                continue
            if (op == "field" and type(value) is str
                    and normalized["types"].get(value) == "int"):
                continue
            raise CompileError("effect requires nonnegative constant or integer fact")
    invariants = doc["invariants"]
    if type(invariants) is not list or not 1 <= len(invariants) <= 64:
        raise CompileError("invariant count")
    ids = {r["id"] for r in normalized["rules"]}
    covered = set()
    for invariant in invariants:
        _keys(invariant, ("id", "state", "max"))
        if not _name(invariant["id"]) or invariant["id"] in ids:
            raise CompileError("duplicate or invalid invariant ID")
        ids.add(invariant["id"])
        if (type(invariant["state"]) is not str or invariant["state"] not in states
                or not _amount(invariant["max"])):
            raise CompileError("invalid invariant bound")
        covered.add(invariant["state"])
    if covered != set(states):
        raise CompileError("every state needs an upper-bound invariant")
    doc.update({k: normalized[k] for k in rule_doc if k != "version"})
    doc["invariants"] = sorted(invariants, key=lambda inv: inv["id"])
    encoded = canonical_json(doc)
    return StatePolicy(encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), rules)


def admit(policy, operation, facts, held):
    """Pure admission; held state comes exclusively from the trusted runtime."""
    doc = json.loads(policy.canonical)
    if (type(operation) is not str or operation not in doc["operations"]
            or type(facts) is not dict or "operation" in facts
            or type(held) is not dict or held.keys() != doc["states"].keys()
            or not all(_amount(value) for value in held.values())):
        return Admission("REJECT", "INVALID_CONTEXT", {}, {})
    context = {**facts, "operation": operation}
    decision = evaluate(policy.rules, context)
    # Invalid facts must never enter arithmetic, including underneath NOT.
    if decision.reason == "INVALID_CONTEXT":
        return Admission("REJECT", "INVALID_CONTEXT", {}, {})
    effects = {name: 0 for name in doc["states"]}
    for name, (op, value) in doc["operations"][operation]["effects"].items():
        amount = value if op == "const" else facts[value]
        if not _amount(amount):
            return Admission("REJECT", "INVALID_EFFECT", {}, {})
        effects[name] = amount
    candidate = {name: held[name] + effects[name] for name in held}
    if not all(_amount(value) for value in candidate.values()):
        return Admission("REJECT", "STATE_OVERFLOW", effects, candidate)
    violated = tuple(inv["id"] for inv in doc["invariants"]
                     if candidate[inv["state"]] > inv["max"])
    if violated:
        return Admission("REJECT", "STATE_INVARIANT", effects, candidate, violated)
    return Admission(decision.decision, decision.reason, effects, candidate,
                     decision.hard_ids)
