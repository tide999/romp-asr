"""Audit saved real episodes without making API calls or replacing raw evidence."""

import hashlib
import json
from pathlib import Path

BASE = Path(__file__).parent
ROOT = BASE.parents[1]


def main():
    manifest = json.loads((BASE / "manifest.json").read_text(encoding="utf-8"))
    for name, fingerprint in manifest["sources"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == fingerprint, name
    records = [json.loads(line) for line in (BASE / "episodes.jsonl").read_text(
        encoding="utf-8").splitlines()]
    identities = [(r["scenario"], r["group"], r["repeat"]) for r in records]
    assert len(set(identities)) == len(identities), "duplicate episode identity"
    rows = []
    for scenario in manifest["scenarios"]:
        for group in ("prompt", "romp"):
            selected = [r for r in records if r["scenario"] == scenario and r["group"] == group]
            events = [e for r in selected for e in r["tool_events"]]
            responses = [p for r in selected for p in r["responses"]]
            rows.append({
                "scenario": scenario, "group": group, "episodes": len(selected),
                "completed": sum(r["termination"] == "completed" for r in selected),
                "terminations": {t: sum(r["termination"] == t for r in selected)
                                 for t in sorted({r["termination"] for r in selected})},
                "unsafe_fresh_proposals": sum(e["unsafe_fresh_invocation"] for e in events),
                "hard_rejected_unsafe": sum(e["unsafe_fresh_invocation"] and
                    e["result"]["outcome"] == "REJECT" for e in events),
                "unknown_returns": sum(e["result"]["outcome"] == "UNKNOWN" for e in events),
                "violating_episodes": sum(r["oracle"]["actual_violation"] for r in selected),
                "safe_subtasks_completed": sum(r["oracle"]["safe_subtask_completed"] for r in selected),
                "backend_charge_calls": sum(r["oracle"]["backend_charge_calls"] for r in selected),
                "effect_count": sum(r["oracle"]["effect_count"] for r in selected),
                "duplicate_effects": sum(r["oracle"]["duplicate_effects"] for r in selected),
                "api_responses": len(responses),
                "reported_models": sorted({p.get("model", "unreported") for p in responses}),
                "reported_total_tokens": sum(p.get("usage", {}).get("total_tokens", 0) for p in responses),
            })
    audit = {"episodes": len(records), "source_hashes_match": True,
             "rows": rows, "raw_sha256": hashlib.sha256((BASE / "episodes.jsonl").read_bytes()).hexdigest()}
    (BASE / "audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
