import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from romp_asr.cli import main
from romp_asr import compile_policy


POLICY = Path(__file__).resolve().parents[1] / "examples" / "policy_v02.json"


def test_public_api_does_not_load_old_research_modules():
    source = """
import os,sys
before=dict(os.environ)
import romp_asr
assert dict(os.environ)==before
assert not hasattr(romp_asr,'Runtime')
assert not hasattr(romp_asr,'ToolPolicy')
for name in ('romp_mu','controlled_tools','experiments','external_baselines','llm_adapter'):
    assert 'romp_asr.'+name not in sys.modules
    import importlib.util
    assert importlib.util.find_spec('romp_asr.'+name) is None
"""
    result = subprocess.run([sys.executable, "-S", "-c", source],
                            cwd=POLICY.parents[1], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(POLICY.parents[1] / "src")})
    assert result.returncode == 0, result.stderr


def test_check_and_evaluate_cli(tmp_path, capsys):
    assert main(["check", str(POLICY)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["policy_hash"] == compile_policy(POLICY.read_bytes()).policy_hash
    context = tmp_path / "context.json"
    for amount, decision, code in ((60, "ALLOW", 0), (110, "REJECT", 1)):
        context.write_text(json.dumps({"operation": "Charge", "amount_minor": 60,
                                      "candidate_total_minor": amount, "target_protected": False}))
        assert main(["eval", str(POLICY), str(context)]) == code
        assert json.loads(capsys.readouterr().out)["decision"] == decision


def test_duplicate_context_and_policy_errors(tmp_path, capsys):
    context = tmp_path / "context.json"
    context.write_text('{"operation":"Charge","operation":"Delete"}')
    assert main(["eval", str(POLICY), str(context)]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "INVALID_CONTEXT"
    assert main(["check", str(tmp_path / "missing.json")]) == 2
    capsys.readouterr()


@pytest.mark.parametrize("arguments", [
    ["bench", "run"], ["verify", "records.jsonl"], ["perf", "bench", "100"],
    ["dsl", "check", "old.yaml"], ["--evaluate"], ["--case", "split"],
])
def test_old_commands_fail_explicitly(arguments):
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2


def test_old_policy_version_rejected():
    with pytest.raises(ValueError):
        compile_policy('{"version":"controlled-tools/1","charge":{},"delete":{}}')


def test_demo_cleans_temporary_database_handles():
    result = subprocess.run([sys.executable, str(POLICY.with_name("policy_v02_demo.py"))],
                            cwd=POLICY.parents[1], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(POLICY.parents[1] / "src")})
    assert result.returncode == 0, result.stderr
    assert "Charge 50: REJECT; settled=60" in result.stdout
    assert "Charge 40: SUCCEEDED; settled=100" in result.stdout
