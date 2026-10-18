"""Generic policy admission inside durable accounting transactions."""

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Callable

from .tool_backends import NoEffectError, ToolResult
from .policy_v02 import canonical_json, evaluate


@dataclass(frozen=True)
class OperationAdapter:
    context: Callable
    invoke: Callable
    binding: str


def charge_adapter(payments):
    def context(subject, args, used):
        if (set(args) != {"amount_minor"} or type(args["amount_minor"]) is not int
                or not 0 < args["amount_minor"] < 2**63
                or used + args["amount_minor"] >= 2**63):
            raise NoEffectError("invalid amount or overflow")
        amount = args["amount_minor"]
        return {"operation": "Charge", "amount_minor": amount,
                "candidate_total_minor": used + amount, "target_protected": False}, amount

    return OperationAdapter(context, lambda identifier, args: payments.charge(
        identifier, args["amount_minor"]), "charge-v1")


def delete_adapter(files, protected_roots):
    protected = frozenset(protected_roots)
    if not protected <= set(files.roots):
        raise ValueError("protected root must be mapped")

    def context(subject, args, used):
        if (set(args) != {"root", "path"} or
                not all(type(v) is str and v for v in args.values()) or
                args["root"] not in files.roots):
            raise NoEffectError("invalid target")
        files.delete(args["root"], args["path"], validate_only=True)
        return {"operation": "Delete", "amount_minor": 0,
                "candidate_total_minor": 0,
                "target_protected": args["root"] in protected}, 0

    binding = canonical_json({"roots": files.roots, "protected": sorted(protected)})
    return OperationAdapter(context, lambda identifier, args: files.delete(
        args["root"], args["path"]), binding)


class PolicyExecutor:
    def __init__(self, policy, database: Path, adapters):
        self.policy, self.database = policy, str(database)
        self.adapters = dict(adapters)
        binding = canonical_json({
            "policy": policy.policy_hash,
            "adapters": {k: v.binding for k, v in self.adapters.items()},
        })
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS policy_binding (value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS operations "
                       "(id TEXT PRIMARY KEY,payload TEXT NOT NULL,amount INTEGER NOT NULL,"
                       "status TEXT NOT NULL,reason TEXT NOT NULL,decision TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT value FROM policy_binding").fetchone()
            if prior and prior[0] != binding:
                raise ValueError("policy or adapter binding changed")
            if not prior:
                db.execute("INSERT INTO policy_binding VALUES (?)", (binding,))

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            if (db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete"
                    or db.execute("PRAGMA synchronous").fetchone()[0] != 2):
                raise ValueError("request database requires DELETE journal and FULL synchronous")
            with db:
                yield db
        finally:
            db.close()

    def submit(self, identifier, subject, operation, arguments):
        if (not all(type(v) is str and 0 < len(v) <= 1024
                    for v in (identifier, subject, operation))
                or type(arguments) is not dict or operation not in self.adapters):
            return ToolResult("REJECT", "invalid submission")
        # Copy through serialization before allowing a provider to inspect arguments.
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
            prior = db.execute("SELECT payload,status,reason FROM operations WHERE id=?",
                               (identifier,)).fetchone()
            if prior:
                if prior[0] != payload:
                    return ToolResult("REJECT", "identifier conflict")
                return ToolResult("UNKNOWN" if prior[1] == "PENDING" else prior[1], prior[2])
            used = db.execute("SELECT COALESCE(SUM(amount),0) FROM operations "
                              "WHERE status IN ('PENDING','UNKNOWN','SUCCEEDED')").fetchone()[0]
            try:
                context, amount = adapter.context(subject, args, used)
                if type(amount) is not int or not 0 <= amount < 2**63 or used + amount >= 2**63:
                    raise NoEffectError("accounting overflow")
            except Exception:
                return ToolResult("REJECT", "invalid trusted context")
            decision = evaluate(self.policy, context)
            if decision.decision != "ALLOW":
                return ToolResult(decision.decision, decision.reason)
            db.execute("INSERT INTO operations VALUES (?,?,?,'PENDING','awaiting result',?)",
                       (identifier, payload, amount, canonical_json(asdict(decision))))
        try:
            adapter.invoke(identifier, args)
            result = ToolResult("SUCCEEDED", "tool completed")
        except NoEffectError as exc:
            result = ToolResult("FAILED", str(exc))
        except Exception:
            result = ToolResult("UNKNOWN", "uncertain tool result")
        with self._connect() as db:
            db.execute("UPDATE operations SET status=?,reason=? WHERE id=?",
                       (result.outcome, result.reason, identifier))
        return result
