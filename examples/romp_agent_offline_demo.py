"""Offline walkthrough of the reference Agent loop.

Only the model transport is replaced. Configuration loading, host construction,
StateEffectExecutor admission, the local backends, trace writing, and the
feedback loop all run as production code, reached through ``main()``. No network
call is made and no credential is required: the endpoint uses the reserved
``.invalid`` TLD and the key environment variable holds an obvious placeholder.

This mirrors the 27 scripted-model reference-agent checks of Section 4.2. It is
a didactic walkthrough, not a real-model measurement; Section 4.3 reports those.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from romp_asr.reference_agent import ModelClient, main

POLICY = Path(__file__).with_name("policy_v03.json")
KEY_ENV = "ROMP_OFFLINE_DEMO_KEY"
PLACEHOLDER_KEY = "offline-demo-placeholder-not-a-credential"
ENDPOINT = "https://demo.invalid/api/v1/chat/completions"
MODEL = "scripted/offline-demonstration"
SUBJECT = "offline-demo"
TASK = "Process both invoices, then clean up both targets."
ORDERS = {"invoice-001": ("demo-invoice-001", 60), "invoice-002": ("demo-invoice-002", 50)}
TARGETS = {"draft": ("demo-draft", "workspace", "draft.txt"),
           "core": ("demo-core", "system", "core.txt")}
ANSWER = "invoice-001 charged; invoice-002 denied by budget; draft removed; core denied."


def _narrator(pace, stream):
    """Return an emitter that flushes every line and optionally paces output.

    The stream is bound explicitly: main()'s own stdout is redirected while the
    agent loop runs, and the narration must stay outside that capture.
    """
    def say(line=""):
        print(line, file=stream, flush=True)
        if pace > 0:
            time.sleep(pace)
    return say


def _scripted_turns(files_enabled):
    """The sequence the transport replays: charge, both deletions, same-ID retry."""
    turns = [("charge", {"order": "invoice-001"}), ("charge", {"order": "invoice-002"})]
    if files_enabled:
        turns += [("delete", {"target": "draft"}), ("delete", {"target": "core"})]
    return turns + [("charge", {"order": "invoice-001"}), None]


class _ScriptedTransport:
    """Stands in for ModelClient.complete and narrates from the messages it is given."""

    def __init__(self, turns, say):
        self.turns, self.say, self.index, self.consumed = turns, say, 0, 0

    def complete(self, messages, tools):
        for message in messages[self.consumed:]:
            if message["role"] == "tool":
                self.say(f"           feedback <- {message['content']}")
        self.consumed = len(messages)
        turn = self.turns[self.index]
        if turn is None:
            self.say(f"  turn {self.index}  model -> final answer, no tool call")
            calls = []
        else:
            name, arguments = turn
            alias = next(iter(arguments.values()))
            self.say(f"  turn {self.index}  model -> {name}({alias})")
            calls = [{"id": f"call-{self.index}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments)}}]
        self.index += 1
        return {"id": f"resp-{self.index}", "model": MODEL,
                "choices": [{"index": 0,
                             "finish_reason": "tool_calls" if calls else "stop",
                             "message": {"role": "assistant",
                                         "content": None if calls else ANSWER,
                                         **({"tool_calls": calls} if calls else {})}}]}


def _write_host(base, files_enabled):
    policy = base / "policy.json"
    policy.write_bytes(POLICY.read_bytes())
    config = {"policy": str(policy), "state_dir": str(base / "state"), "subject": SUBJECT,
              "model": {"endpoint": ENDPOINT, "name": MODEL, "key_env": KEY_ENV},
              "orders": {alias: {"id": identifier, "amount_minor": amount}
                         for alias, (identifier, amount) in ORDERS.items()},
              "max_turns": 8, "max_tokens": 4096}
    if files_enabled:
        roots = base / "roots"
        (roots / "workspace").mkdir(parents=True)
        (roots / "system").mkdir(parents=True)
        (roots / "workspace" / "draft.txt").write_text("draft\n", encoding="utf-8")
        (roots / "system" / "core.txt").write_text("protected\n", encoding="utf-8")
        config["roots"] = {name: str(roots / name) for name in ("workspace", "system")}
        config["protected_roots"] = ["system"]
        config["targets"] = {alias: {"id": identifier, "root": root, "path": path}
                             for alias, (identifier, root, path) in TARGETS.items()}
    config_path = base / "host.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config_path, roots if files_enabled else None


def _say_state(say, state):
    say(f"  payment_total : {state['payment_total_minor']}")
    say("  held          : " + ", ".join(
        f"{name}={amount}" for name, amount in sorted(state["held"].items())))
    for request in state["requests"]:
        effects = ", ".join(f"{name}={amount}" for name, amount
                            in sorted(request["admission"]["effects"].items()))
        say(f"  request       : {request['id']:<18} {request['status']:<10}"
            f"{effects:<24}  {request['reason']}")
    say("  note          : rejected proposals create no request record")


def offline_walkthrough(pace):
    files_enabled = os.name == "posix"
    say = _narrator(pace, sys.stdout)
    say("ROMP reference agent -- offline walkthrough (scripted model transport)")
    say(f"model      : {MODEL}")
    say(f"endpoint   : {ENDPOINT}  (never contacted)")
    if not files_enabled:
        say("! POSIX file backend unavailable: both deletions are omitted.")
        say("  Record under WSL to capture the complete walkthrough.")
    say("")
    say("[1/4] host catalog -- aliases are the only tool arguments the model may use")
    say(f"  charge(order)  -> {', '.join(ORDERS)}")
    if files_enabled:
        say(f"  delete(target) -> {', '.join(TARGETS)}")
    say("  the model never sees an amount, a path, or a credential")

    with TemporaryDirectory(prefix="romp-agent-") as temporary:
        base = Path(temporary)
        config_path, roots = _write_host(base, files_enabled)
        os.environ[KEY_ENV] = PLACEHOLDER_KEY
        turns = _scripted_turns(files_enabled)
        transport = _ScriptedTransport(turns, say)
        say("")
        say(f"[2/4] romp-agent run --task {TASK!r}")
        with patch.object(ModelClient, "complete",
                          lambda self, messages, tools: transport.complete(messages, tools)):
            captured = io.StringIO()
            with redirect_stdout(captured):
                code = main(["run", "--config", str(config_path), "--task", TASK])
        result = json.loads(captured.getvalue())
        say("")
        say(f"  exit code {code}, termination {result['termination']}")
        _say_state(say, result["state"])

        say("")
        say("[3/4] separate process: romp-agent status --config host.json")
        completed = subprocess.run(
            [sys.executable, "-m", "romp_asr.reference_agent", "status",
             "--config", str(config_path)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", str(Path(__file__).parents[1] / "src"))})
        if completed.returncode == 0:
            _say_state(say, json.loads(completed.stdout))
        else:
            say(f"  status failed: {completed.stderr.strip()}")

        if roots:
            say("")
            say("[4/4] observed files")
            for alias, (_, root, path) in TARGETS.items():
                say(f"  delete({alias}) -> {root}/{path} present={(roots / root / path).exists()}")
    say("")
    say("end of offline walkthrough")


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pace", type=float, default=0.0, metavar="SECONDS",
                        help="sleep after each emitted line, for legible recording")
    arguments = parser.parse_args()
    if arguments.pace < 0:
        parser.error("--pace must not be negative")
    offline_walkthrough(arguments.pace)


if __name__ == "__main__":
    main_cli()