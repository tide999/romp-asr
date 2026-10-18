"""Policy-driven real local payments and temporary-file deletion.

Default mode prints the bare proposal sequence. ``--scripted`` replays the
annotated policy-core walkthrough -- cumulative overrun, same-ID retry, and
controlled deletion -- including the policy-authoring step, and is
deterministic: no path, timestamp, or random value reaches stdout.

This exercises the durable-admission core only. The reference agent loop lives
in ``romp_asr.reference_agent`` and is not reachable from this demo.
"""

import argparse
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from romp_asr.tool_backends import LocalPaymentLedger, PosixFileTools
from romp_asr.policy_v02 import canonical_json, compile_policy
from romp_asr.policy_tools import PolicyExecutor, charge_adapter, delete_adapter

POLICY = Path(__file__).with_name("policy_v02.json")
ROOT_NAMES = ("workspace", "system")
PROTECTED_ROOTS = ("system",)
CHARGE_SEQUENCE = (("a", 60), ("b", 50), ("a", 60), ("c", 40))
DELETE_SEQUENCE = (("system", "core"), ("workspace", "draft"))
EDITED_THRESHOLD = 200
COMPARISONS = {"=": "==", ">": ">", "<": "<", ">=": ">=", "<=": "<="}


def _narrator(pace):
    """Return an emitter that flushes every line and optionally paces output."""
    def say(line=""):
        print(line, flush=True)
        if pace > 0:
            time.sleep(pace)
    return say


def _make_fixtures(base):
    for name in ROOT_NAMES:
        (base / name).mkdir()
    (base / "system" / "core").write_text("protected", encoding="utf-8")
    (base / "workspace" / "draft").write_text("ordinary", encoding="utf-8")


def _make_adapters(base, ledger):
    adapters = {"Charge": charge_adapter(ledger)}
    if os.name != "posix":
        return adapters, None
    files = PosixFileTools({name: base / name for name in ROOT_NAMES})
    return {**adapters, "Delete": delete_adapter(files, PROTECTED_ROOTS)}, files


def run_scope(policy, base, say):
    """Submit the fixed proposal sequence in one fresh scope and report effects."""
    if os.name == "posix":
        _make_fixtures(base)
    ledger = LocalPaymentLedger(base / "payments.db")
    adapters, files = _make_adapters(base, ledger)
    try:
        executor = PolicyExecutor(policy, base / "requests.db", adapters)
        for identifier, amount in CHARGE_SEQUENCE:
            result = executor.submit(identifier, "alice", "Charge",
                                     {"amount_minor": amount})
            say(f"Charge {amount}: {result.outcome}; settled={ledger.total}")
        if files:
            for root, relative in DELETE_SEQUENCE:
                result = executor.submit(root, "alice", "Delete",
                                         {"root": root, "path": relative})
                exists = (base / root / relative).exists()
                say(f"Delete {root}/{relative}: {result.outcome}; exists={exists}")
    finally:
        if files:
            files.close()


def _render_predicate(node):
    """Readable form for a simple comparison; exact canonical JSON otherwise."""
    if (type(node) is list and len(node) == 3 and type(node[0]) is str
            and node[0] in COMPARISONS and type(node[1]) is str):
        return f"{node[1]} {COMPARISONS[node[0]]} {json.dumps(node[2])}"
    return canonical_json(node)


def _render_rule(rule):
    line = (f"  {rule['kind']:<4} {rule['id']:<9} "
            f"{_render_predicate(rule['if'])} -> {rule['then']}")
    if rule["kind"] == "soft":
        line += f"  (weight={rule['weight']}, priority={rule['priority']})"
    return line


def _ledger_summary(database):
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute("SELECT id, amount FROM payments ORDER BY id").fetchall()
    entries = " ".join(f"{identifier}={amount}" for identifier, amount in rows)
    return f"{entries} total={sum(amount for _, amount in rows)}"


def _say_inspection(say, base):
    say(f"  payments    : {_ledger_summary(base / 'payments.db')}")
    for root, relative in DELETE_SEQUENCE:
        if (base / root).is_dir():
            present = (base / root / relative).exists()
            say(f"  filesystem  : {root}/{relative} present={present}")


def _edited_policy(source):
    """Raise the hard budget threshold in memory; the policy file on disk is untouched."""
    document = json.loads(source)
    budget = [rule for rule in document["rules"] if rule.get("id") == "budget"]
    if len(budget) != 1 or budget[0]["kind"] != "hard":
        raise SystemExit("policy_v02.json no longer has a single hard budget rule")
    predicate = budget[0]["if"]
    if predicate[:2] != [">", "candidate_total_minor"]:
        raise SystemExit("policy_v02.json budget predicate changed shape")
    threshold = predicate[2]
    predicate[2] = EDITED_THRESHOLD
    return threshold, compile_policy(json.dumps(document).encode("utf-8"))


def _show_binding_guard(base, original, edited, say):
    """A changed policy must be refused on an existing scope, not silently rebound."""
    ledger = LocalPaymentLedger(base / "payments.db")
    adapters, files = _make_adapters(base, ledger)
    try:
        PolicyExecutor(original, base / "requests.db", adapters)
        say("  reopen the old scope with the original policy -> accepted")
        try:
            PolicyExecutor(edited, base / "requests.db", adapters)
        except ValueError as exc:
            say(f"  reopen the old scope with the edited policy   -> refused: {exc}")
            return
        raise SystemExit("policy binding guard accepted a changed policy")
    finally:
        if files:
            files.close()


def scripted_walkthrough(pace):
    """Replay the annotated walkthrough; the result lines are identical to default mode."""
    say = _narrator(pace)
    source = POLICY.read_bytes()
    policy = compile_policy(source)
    document = json.loads(source)

    say("ROMP demo -- scripted walkthrough (deterministic replay)")
    say(f"policy file : {POLICY.name}")
    say(f"policy hash : {policy.policy_hash}")
    if os.name != "posix":
        say("! POSIX file backend unavailable: the deletion half is skipped.")
        say("  Record under WSL to capture all six result lines.")
    say("")
    say("[1/6] author edits rules -- no host code change is involved")
    say(f"  version {document['version']}   default {document['default']}")
    for rule in document["rules"]:
        say(_render_rule(rule))

    with TemporaryDirectory(prefix="romp-policy-") as first:
        base = Path(first)
        say("")
        say("[2/6] initialize a fresh scope and the host root mapping")
        say("  scope      : new requests.db + payments.db, no prior history")
        say(f"  roots      : {', '.join(ROOT_NAMES)}   protected: "
            f"{', '.join(PROTECTED_ROOTS)}")
        if os.name == "posix":
            say("  fixtures   : system/core (protected), workspace/draft (ordinary)")
        say("")
        say("[3/6] submit proposals")
        run_scope(policy, base, say)
        say("")
        say("[4/6] inspect durable effects")
        _say_inspection(say, base)

        threshold, edited = _edited_policy(source)
        say("")
        say(f"[5/6] author edits one rule: hard budget threshold "
            f"{threshold} -> {EDITED_THRESHOLD}")
        say(f"  hash before: {policy.policy_hash}")
        say(f"  hash after : {edited.policy_hash}")
        say("  a changed policy needs a new scope: the request database binds the")
        say("  policy hash and the adapter bindings at initialization")
        _show_binding_guard(base, policy, edited, say)

    with TemporaryDirectory(prefix="romp-policy-") as second:
        say("")
        say("[6/6] replay the same proposals in a new scope under the edited policy")
        run_scope(edited, Path(second), say)
        say("")
        say("  inspect durable effects")
        _say_inspection(say, Path(second))
    say("")
    say("end of scripted walkthrough")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scripted", action="store_true",
                        help="replay the annotated walkthrough including policy authoring")
    parser.add_argument("--pace", type=float, default=0.0, metavar="SECONDS",
                        help="sleep after each emitted line, for legible recording")
    arguments = parser.parse_args()
    if arguments.pace < 0:
        parser.error("--pace must not be negative")
    if arguments.scripted:
        scripted_walkthrough(arguments.pace)
        return
    say = _narrator(arguments.pace)
    with TemporaryDirectory(prefix="romp-policy-") as temporary:
        run_scope(compile_policy(POLICY.read_bytes()), Path(temporary), say)


if __name__ == "__main__":
    main()