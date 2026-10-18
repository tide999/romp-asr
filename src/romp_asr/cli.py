"""CLI for v0.2 compilation and pure evaluation (never invokes tools)."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .policy_v02 import CompileError, canonical_json, compile_policy, evaluate


def _read(path, limit):
    with Path(path).open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("input byte limit")
    return content


def main(argv=None):
    parser = argparse.ArgumentParser(description="ROMP-mu++/0.2 policy compiler and evaluator")
    parser.add_argument("--version", action="version", version="romp-asr 0.2.0")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "eval"):
        command = commands.add_parser(name)
        command.add_argument("policy")
        command.add_argument("--format", choices=("json", "yaml"), default="json")
        if name == "eval":
            command.add_argument("context", help="typed JSON Context; no tool execution")
    args = parser.parse_args(argv)
    try:
        policy = compile_policy(_read(args.policy, 65536), args.format)
        if args.command == "check":
            print(canonical_json({"version": "romp-mu++/0.2", "policy_hash": policy.policy_hash,
                                  "canonical": json.loads(policy.canonical)}))
            return 0
        raw = _read(args.context, 16384)
        # Use the same bounded decoder; duplicate keys are invalid contexts.
        from .policy_v02 import _load
        try:
            context = _load(raw, "json")
        except CompileError:
            context = None
        result = evaluate(policy, context)
        print(canonical_json(asdict(result)))
        return 0 if result.decision == "ALLOW" else 1
    except (OSError, CompileError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
