"""Transactional vector reservations driven by a v0.3 policy."""

from dataclasses import asdict, dataclass
import json
from typing import Callable

from .policy_tools import PolicyExecutor
from .policy_v02 import canonical_json
from .policy_v03 import admit
from .tool_backends import NoEffectError, ToolResult


@dataclass(frozen=True)
class StateEffectAdapter:
    facts: Callable
    invoke: Callable
    binding: str


class StateEffectExecutor(PolicyExecutor):
    """Uses the existing DELETE/FULL connection discipline, not v0.2 tables."""

    def __init__(self, policy, database, adapters):
        self.policy, self.database = policy, str(database)
        self.adapters = dict(adapters)
        self.states = tuple(json.loads(policy.canonical)["states"])
        operations = json.loads(policy.canonical)["operations"]
        if (not self.adapters or not self.adapters.keys() <= operations.keys()
                or any(not isinstance(v, StateEffectAdapter)
                       or type(v.binding) is not str or not v.binding
                       for v in self.adapters.values())):
            raise ValueError("invalid state-effect adapters")
        binding = canonical_json({"policy": policy.policy_hash,
                                  "adapters": {k: v.binding for k, v in self.adapters.items()}})
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='operations'").fetchone():
                raise ValueError("v0.2 scope requires migration, not reinitialization")
            db.execute("CREATE TABLE IF NOT EXISTS state_binding (value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS state_requests "
                       "(id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,"
                       "reason TEXT NOT NULL,decision TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS state_reservations "
                       "(request TEXT NOT NULL,state TEXT NOT NULL,amount INTEGER NOT NULL,"
                       "PRIMARY KEY(request,state))")
            bindings = db.execute("SELECT value FROM state_binding").fetchall()
            if bindings and bindings != [(binding,)]:
                raise ValueError("policy or adapter binding changed")
            if not bindings:
                if (db.execute("SELECT 1 FROM state_requests LIMIT 1").fetchone()
                        or db.execute("SELECT 1 FROM state_reservations LIMIT 1").fetchone()):
                    raise ValueError("unbound existing scope")
                db.execute("INSERT INTO state_binding VALUES (?)", (binding,))

    def _held(self, db):
        held = dict.fromkeys(self.states, 0)
        rows = db.execute(
            "SELECT r.state,r.amount FROM state_reservations r "
            "JOIN state_requests q ON q.id=r.request "
            "WHERE q.status IN ('PENDING','UNKNOWN','SUCCEEDED')").fetchall()
        for state, amount in rows:
            if state not in held or type(amount) is not int or amount < 0:
                raise ValueError("invalid persistent reservation")
            held[state] += amount
        return held

    def snapshot(self):
        with self._connect() as db:
            db.execute("BEGIN")
            return {"held": self._held(db), "requests": [
                {"id": identifier, "status": status, "reason": reason,
                 "admission": json.loads(decision)}
                for identifier, status, reason, decision in db.execute(
                    "SELECT id,status,reason,decision FROM state_requests ORDER BY id")
            ]}

    def submit(self, identifier, subject, operation, arguments):
        if (not all(type(v) is str and 0 < len(v) <= 1024
                    for v in (identifier, subject, operation))
                or type(arguments) is not dict or operation not in self.adapters):
            return ToolResult("REJECT", "invalid submission")
        try:
            payload = canonical_json([subject, operation, arguments])
            if len(payload.encode("utf-8")) > 16384:
                raise ValueError()
            args = json.loads(payload)[2]
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return ToolResult("REJECT", "invalid arguments")
        adapter = self.adapters[operation]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT payload,status,reason FROM state_requests WHERE id=?",
                               (identifier,)).fetchone()
            if prior:
                if prior[0] != payload:
                    return ToolResult("REJECT", "identifier conflict")
                return ToolResult("UNKNOWN" if prior[1] == "PENDING" else prior[1], prior[2])
            try:
                facts = adapter.facts(subject, args)
                if canonical_json([subject, operation, args]) != payload:
                    raise ValueError("provider mutated arguments")
                admission = admit(self.policy, operation, facts, self._held(db))
            except Exception:
                return ToolResult("REJECT", "invalid trusted context")
            if admission.decision != "ALLOW":
                return ToolResult(admission.decision, admission.reason)
            record = {"policy_hash": self.policy.policy_hash, **asdict(admission)}
            db.execute("INSERT INTO state_requests VALUES (?,?,'PENDING',?,?)",
                       (identifier, payload, "awaiting result", canonical_json(record)))
            db.executemany("INSERT INTO state_reservations VALUES (?,?,?)",
                           [(identifier, state, amount)
                            for state, amount in admission.effects.items()])
        try:
            adapter.invoke(identifier, args)
            result = ToolResult("SUCCEEDED", "tool completed")
        except NoEffectError as exc:
            result = ToolResult("FAILED", str(exc))
        except Exception:
            result = ToolResult("UNKNOWN", "uncertain tool result")
        with self._connect() as db:
            db.execute("UPDATE state_requests SET status=?,reason=? WHERE id=?",
                       (result.outcome, result.reason, identifier))
        return result
