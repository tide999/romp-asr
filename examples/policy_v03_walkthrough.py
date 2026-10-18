"""Scripted walkthrough of the v0.3 effect-accounting kernel.

Replays an annotated, deterministic walkthrough of the declarations and the
admission they drive: cumulative budget rejection, a second resource dimension
that rejects on its own, an unresolved outcome that keeps its capacity,
controlled deletion, and durable state under a changed declaration.

No path, timestamp, or random value reaches stdout, so a recording of this
walkthrough is a deterministic replay. Each section opens its own scope, so a
section can be read without tracing the ones before it. The reference-agent
loop is a separate walkthrough and is not reachable from here.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from romp_asr.policy_v02 import canonical_json
from romp_asr.policy_v03 import admit, compile_state_policy
from romp_asr.state_effect_tools import StateEffectAdapter, StateEffectExecutor
from romp_asr.tool_backends import LocalPaymentLedger, PosixFileTools

POLICY = Path(__file__).with_name("policy_v03.json")
ROOT_NAMES = ("workspace", "system")
PROTECTED_ROOTS = ("system",)
DELETIONS = (("system", "core.txt", True), ("workspace", "draft.txt", False))
BUDGET_SEQUENCE = (("a", 60), ("b", 50), ("a", 60), ("c", 40))
DIMENSION_SEQUENCE = (("a", 10), ("b", 10), ("c", 10))
EDITED_BUDGET = 200

STATUS_SCRIPT = """
import os
from pathlib import Path
from romp_asr.policy_v03 import compile_state_policy
from romp_asr.state_effect_tools import StateEffectAdapter, StateEffectExecutor

base = Path(os.environ["ROMP_SCOPE"])
policy = compile_state_policy(Path(os.environ["ROMP_POLICY"]).read_bytes())
adapter = StateEffectAdapter(lambda *arguments: {}, lambda *arguments: None,
                             "local-payment-v3")
state = StateEffectExecutor(policy, base / "requests.db", {"Charge": adapter}).snapshot()
held = " ".join(f"{name}={amount}" for name, amount in state["held"].items())
print(f"held {held} requests={len(state['requests'])}")
"""


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
    (base / "system" / "core.txt").write_text("protected", encoding="utf-8")
    (base / "workspace" / "draft.txt").write_text("ordinary", encoding="utf-8")


def _charge_adapter(ledger, invocations, unknown=False):
    """A real ledger, plus the declared facts the admission needs."""
    def facts(subject, arguments):
        return {"amount_minor": arguments["amount_minor"], "target_protected": False}

    def invoke(identifier, arguments):
        invocations.append(identifier)
        ledger.charge(identifier, arguments["amount_minor"])
        if unknown:
            raise TimeoutError("simulated after-effect timeout")

    return StateEffectAdapter(facts, invoke, "local-payment-v3")


def _delete_adapter(files):
    """Validate first so the host fact exists before admission decides."""
    def facts(subject, arguments):
        files.delete(arguments["root"], arguments["path"], validate_only=True)
        return {"amount_minor": 0,
                "target_protected": arguments["root"] in PROTECTED_ROOTS}

    def invoke(identifier, arguments):
        files.delete(arguments["root"], arguments["path"])

    return StateEffectAdapter(facts, invoke, canonical_json(files.roots))


def _render_effect(expression):
    _, value = expression
    return str(value)


def _held_text(engine):
    held = engine.snapshot()["held"]
    return " ".join(f"{name}={held[name]}" for name in engine.states)


def _rejection_label(policy, amount, held):
    """Name the violated invariants through the same entry point the runtime uses."""
    violated = admit(policy, "Charge",
                     {"amount_minor": amount, "target_protected": False}, held).violated
    return f" ({', '.join(violated)})" if violated else ""


def _play_charges(say, policy, label, sequence, base, unknown=False, retries=True):
    """Submit one charge sequence into one scope and report the held vector."""
    ledger = LocalPaymentLedger(base / "payments.db")
    invocations = []
    engine = StateEffectExecutor(policy, base / "requests.db",
                                 {"Charge": _charge_adapter(ledger, invocations, unknown)})
    say("")
    say(label)
    seen = set()
    for identifier, amount in sequence:
        held = engine.snapshot()["held"]
        result = engine.submit(identifier, "alice", "Charge", {"amount_minor": amount})
        if retries and identifier in seen:
            say(f"  Charge {amount:>3} id={identifier} : {result.outcome} (stored)"
                f"   invocations={invocations.count(identifier)}")
            continue
        seen.add(identifier)
        detail = _rejection_label(policy, amount, held) if result.outcome == "REJECT" else ""
        say(f"  Charge {amount:>3} id={identifier} : {result.outcome}{detail}"
            f"   held {_held_text(engine)}")
    if unknown:
        say(f"  invocations for id=a : {invocations.count('a')}")


def _charge_section(say, policy, label, sequence, scope, base=None, unknown=False, retries=True):
    """Play one charge sequence. Scope A passes its own base so it stays alive."""
    if base is not None:
        _play_charges(say, policy, label, sequence, Path(base), unknown, retries)
        return
    with TemporaryDirectory(prefix=f"romp-v03-{scope}-") as temporary:
        _play_charges(say, policy, label, sequence, Path(temporary), unknown, retries)


def _declaration_section(say, document):
    say("[1/7] declarations -- resources, effect bounds, invariants")
    say(f"  states     : {', '.join(document['states'])}   (monotone gross counters)")
    for operation, definition in document["operations"].items():
        effects = definition["effects"]
        rendered = ", ".join(f"{state} += {_render_effect(expression)}"
                             for state, expression in effects.items())
        say(f"  {operation:<10} : {rendered or 'no accounted effect on these resources'}")
    for invariant in document["invariants"]:
        say(f"  invariant  : {invariant['id']:<12} {invariant['state']:<12}"
            f" <= {invariant['max']}")
    say("  note       : an invariant is an upper bound; no rule can override it")


def _deletion_section(say, policy):
    if os.name != "posix":
        say("")
        say("[6/7] scope D -- controlled deletion (POSIX file backend unavailable)")
        return
    with TemporaryDirectory(prefix="romp-v03-d-") as temporary:
        base = Path(temporary)
        _make_fixtures(base)
        files = PosixFileTools({name: base / name for name in ROOT_NAMES})
        try:
            engine = StateEffectExecutor(policy, base / "requests.db",
                                         {"Delete": _delete_adapter(files)})
            say("")
            say("[6/7] scope D -- controlled deletion")
            for root, path, protected in DELETIONS:
                result = engine.submit(root, "alice", "Delete",
                                       {"root": root, "path": path})
                exists = (base / root / path).exists()
                label = " (protected)" if protected and result.outcome == "REJECT" else ""
                say(f"  Delete {root}/{path:<10} : {result.outcome}{label}"
                    f"   exists={exists}")
        finally:
            files.close()


def _durability_section(say, policy, source, base, ledger):
    say("")
    say("[7/7] scope A again -- durable state, and a changed declaration")
    environment = {**os.environ,
                   "ROMP_SCOPE": str(base), "ROMP_POLICY": str(POLICY),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    completed = subprocess.run([sys.executable, "-c", STATUS_SCRIPT], env=environment,
                               capture_output=True, text=True, check=False)
    reported = completed.stdout.strip() or completed.stderr.strip()
    say(f"  second process           : {reported}")
    edited_document = json.loads(source)
    previous = None
    for invariant in edited_document["invariants"]:
        if invariant["id"] == "budget":
            previous, invariant["max"] = invariant["max"], EDITED_BUDGET
    if previous is None:
        raise SystemExit("policy_v03.json no longer declares the budget invariant")
    edited = compile_state_policy(canonical_json(edited_document))
    say(f"  edit invariant budget {previous} -> {EDITED_BUDGET} : the hash changes")
    try:
        StateEffectExecutor(edited, base / "requests.db",
                            {"Charge": _charge_adapter(ledger, [])})
    except ValueError as exc:
        say(f"  reopen scope A under the edited policy -> refused ({exc})")
        return
    raise SystemExit("the binding guard accepted a changed policy")


def scripted_walkthrough(pace):
    say = _narrator(pace)
    source = POLICY.read_bytes()
    policy = compile_state_policy(source)
    document = json.loads(policy.canonical)

    say("ROMP v0.3 -- scripted walkthrough (deterministic replay)")
    say(f"policy      : {POLICY.name}")
    say(f"policy hash : {policy.policy_hash}")
    _declaration_section(say, document)

    say("")
    say("[2/7] scope A -- fresh databases and the host root mapping")
    say(f"  roots      : {ROOT_NAMES[0]} (ordinary), {ROOT_NAMES[1]} (protected)")
    if os.name == "posix":
        say("  fixtures   : system/core.txt (protected), workspace/draft.txt (ordinary)")
    else:
        say("! POSIX file backend unavailable: the deletion section is skipped.")

    with TemporaryDirectory(prefix="romp-v03-a-") as temporary:
        base = Path(temporary)
        _charge_section(
            say, policy, "[3/7] scope A -- cumulative budget and identifier-bound retry",
            BUDGET_SEQUENCE, "a", base=base)
        say(f"  ledger     : total={LocalPaymentLedger(base / 'payments.db').total}")

        _charge_section(
            say, policy, "[4/7] scope B -- a second dimension rejects on its own",
            DIMENSION_SEQUENCE, "b", retries=False)
        say("  note       : spent is 20 of 100; the amount budget alone would admit this")

        _charge_section(
            say, policy, "[5/7] scope C -- an unresolved outcome keeps its capacity",
            (("a", 60), ("b", 50), ("a", 60)), "c", unknown=True)

        _deletion_section(say, policy)
        _durability_section(say, policy, source, base, LocalPaymentLedger(base / "payments.db"))

    say("")
    say("end of scripted walkthrough")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pace", type=float, default=0.0, metavar="SECONDS",
                        help="sleep after each emitted line, for legible recording")
    arguments = parser.parse_args()
    if arguments.pace < 0:
        parser.error("--pace must not be negative")
    scripted_walkthrough(arguments.pace)


if __name__ == "__main__":
    main()
