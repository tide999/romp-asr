"""Run a v0.3 payment through language-declared state/effect constraints."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from romp_asr.policy_v03 import compile_state_policy
from romp_asr.state_effect_tools import StateEffectAdapter, StateEffectExecutor
from romp_asr.tool_backends import LocalPaymentLedger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("policy_v03.json"))
    parser.add_argument("--id")
    parser.add_argument("--amount", type=int)
    parser.add_argument("--outcome", choices=("ok", "unknown"), default="ok",
                        help="simulate an after-effect timeout for Charge")
    args = parser.parse_args()
    if (args.id is None) != (args.amount is None):
        parser.error("--id and --amount must be supplied together")
    policy = compile_state_policy(args.policy.read_bytes())
    requests, payments = args.state / "requests.db", args.state / "payments.db"
    if requests.exists() != payments.exists():
        parser.error("incomplete persistent scope")
    args.state.mkdir(parents=True, exist_ok=True)
    ledger = LocalPaymentLedger(payments)

    def facts(subject, parameters):
        return {"amount_minor": parameters["amount_minor"], "target_protected": False}

    def invoke(identifier, parameters):
        ledger.charge(identifier, parameters["amount_minor"])
        if args.outcome == "unknown":
            raise TimeoutError("simulated after-effect timeout")

    engine = StateEffectExecutor(policy, requests, {
        "Charge": StateEffectAdapter(facts, invoke, "local-payment-v3")
    })
    result = engine.submit(args.id, "demo-host", "Charge",
                           {"amount_minor": args.amount}) if args.id is not None else None
    print(json.dumps({"result": asdict(result) if result else None,
                      "payment_total_minor": ledger.total,
                      **engine.snapshot()}, indent=2))


if __name__ == "__main__":
    main()
