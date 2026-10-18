"""Reference Agent tests use scripted model responses, not LLM measurements."""

import json
import os
from pathlib import Path
import urllib.error

import pytest

from romp_asr.reference_agent import (
    AgentError, AgentHost, ModelClient, ReferenceAgent, load_config, main,
)


def response(name=None, args=None, *, finish=None):
    message = {"role": "assistant", "content": "Recorded tool outcomes." if name is None else None}
    if name:
        message["tool_calls"] = [{"id": "call-1", "type": "function",
                                 "function": {"name": name, "arguments": json.dumps(args)}}]
    return {"choices": [{"message": message,
                         "finish_reason": finish or ("tool_calls" if name else "stop")}]}


class ScriptedClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.feedback = []

    def complete(self, messages, tools):
        self.feedback.extend(m["content"] for m in messages if m["role"] == "tool")
        assert tools
        return next(self.responses)


@pytest.fixture
def configured(tmp_path):
    policy = Path(__file__).parents[1] / "examples/policy_v03.json"
    (tmp_path / "policy.json").write_bytes(policy.read_bytes())
    config = {
        "policy": "policy.json", "state_dir": "state", "subject": "operator",
        "model": {"endpoint": "https://example.test/v1/chat/completions",
                  "name": "test-model", "key_env": "ROMP_TEST_KEY"},
        "orders": {"first": {"id": "business-1", "amount_minor": 60},
                   "second": {"id": "business-2", "amount_minor": 50}},
    }
    filename = tmp_path / "host.json"
    filename.write_text(json.dumps(config), encoding="utf-8")
    return filename, config


def save(configured):
    filename, config = configured
    filename.write_text(json.dumps(config), encoding="utf-8")
    return load_config(filename)


def test_loop_budget_feedback_and_restart(configured):
    config = save(configured)
    host = AgentHost(config)
    client = ScriptedClient([response("charge", {"order": "first"}),
                             response("charge", {"order": "second"}), response()])
    result = ReferenceAgent(host, client).run("Process configured invoices.")
    assert result["termination"] == "completed"
    assert result["state"]["payment_total_minor"] == 60
    assert result["state"]["held"] == {"spent": 60, "charge_count": 1}
    assert any('"outcome": "REJECT"' in feedback for feedback in client.feedback)
    trace = [json.loads(line) for line in
             (config["state_dir"] / "agent-trace.jsonl").read_text().splitlines()]
    # The second charge is refused on the persisted state invariant, not on a rule score.
    assert [(r["result"]["outcome"], r["result"]["reason"])
            for r in trace if r["kind"] == "tool"] == [
                ("SUCCEEDED", "tool completed"), ("REJECT", "STATE_INVARIANT")]
    host.close()
    restarted = AgentHost(config)
    assert restarted.submit("charge", {"order": "first"}).outcome == "SUCCEEDED"
    assert restarted.submit("charge", {"order": "second"}).outcome == "REJECT"
    assert restarted.status()["payment_total_minor"] == 60
    restarted.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file backend")
def test_loop_actual_deletion_and_protected_target(configured):
    filename, config = configured
    for root in ("workspace", "system"):
        folder = filename.parent / root
        folder.mkdir()
        (folder / "file.txt").write_text("test")
    config.update(roots={"work": "workspace", "protected": "system"},
                  protected_roots=["protected"],
                  targets={"draft": {"id": "delete-1", "root": "work", "path": "file.txt"},
                           "core": {"id": "delete-2", "root": "protected", "path": "file.txt"}})
    host = AgentHost(save(configured))
    try:
        result = ReferenceAgent(host, ScriptedClient([
            response("delete", {"target": "draft"}),
            response("delete", {"target": "core"}), response(),
        ])).run("Clean configured targets.")
        assert result["termination"] == "completed"
        assert not (filename.parent / "workspace/file.txt").exists()
        assert (filename.parent / "system/file.txt").exists()
        assert len(result["state"]["requests"]) == 1
    finally:
        host.close()


def test_unknown_feedback_and_retry_suppression(configured):
    host = AgentHost(save(configured))
    invoke = host.payments.charge
    calls = []
    def timeout(identifier, amount):
        calls.append(identifier)
        invoke(identifier, amount)
        raise TimeoutError()
    host.payments.charge = timeout
    client = ScriptedClient([response("charge", {"order": "first"}),
                             response("charge", {"order": "first"}), response()])
    result = ReferenceAgent(host, client).run("Pay and check uncertain status.")
    assert calls == ["business-1"]
    assert result["state"]["payment_total_minor"] == 60
    # An uncertain outcome keeps the whole declared effect vector reserved.
    assert result["state"]["held"] == {"spent": 60, "charge_count": 1}
    assert result["state"]["requests"][0]["status"] == "UNKNOWN"
    assert any('"outcome": "UNKNOWN"' in value for value in client.feedback)
    host.close()


@pytest.mark.parametrize("name,args", [
    ("shell", {"command": "delete"}),
    ("charge", {"order": "unregistered"}),
    ("charge", {"order": "first", "amount_minor": 1}),
    ("charge", {"order": []}),
    ("charge", []),
])
def test_out_of_catalog_calls_have_no_effect(configured, name, args):
    host = AgentHost(save(configured))
    assert host.submit(name, args).outcome == "REJECT"
    assert host.status()["payment_total_minor"] == 0
    host.close()


@pytest.mark.parametrize("mutation", [
    lambda c: c["orders"]["first"].update(amount_minor=True),
    lambda c: c["orders"]["second"].update(id="business-1"),
    lambda c: c["model"].update(endpoint="http://example.test/chat/completions"),
    lambda c: c.update(max_turns=0),
    lambda c: c.update(unrecognized=True),
])
def test_invalid_configuration(configured, mutation):
    mutation(configured[1])
    with pytest.raises(AgentError):
        save(configured)


def test_binding_change_and_missing_database_rejected(configured):
    host = AgentHost(save(configured))
    host.submit("charge", {"order": "first"})
    host.close()
    configured[1]["orders"]["first"]["amount_minor"] = 1
    with pytest.raises(ValueError, match="binding changed"):
        AgentHost(save(configured))
    configured[1]["orders"]["first"]["amount_minor"] = 60
    config = save(configured)
    (config["state_dir"] / "payments.db").unlink()
    with pytest.raises(AgentError, match="incomplete"):
        AgentHost(config)


def test_cli_status_without_model_key(configured, capsys):
    host = AgentHost(save(configured))
    host.submit("charge", {"order": "first"})
    host.close()
    assert main(["status", "--config", str(configured[0])]) == 0
    assert json.loads(capsys.readouterr().out)["payment_total_minor"] == 60


def test_cli_run_with_injected_transport(configured, monkeypatch, capsys):
    monkeypatch.setenv("ROMP_TEST_KEY", "test-secret")
    scripted = ScriptedClient([response("charge", {"order": "first"}), response()])
    monkeypatch.setattr(ModelClient, "complete",
                        lambda self, messages, tools: scripted.complete(messages, tools))
    assert main(["run", "--config", str(configured[0]), "--task", "Pay first."]) == 0
    assert json.loads(capsys.readouterr().out)["state"]["payment_total_minor"] == 60


@pytest.mark.parametrize("payload", [
    {}, response(finish="length"),
    {"choices": [{"message": {"role": "assistant", "tool_calls": "invalid"}}]},
])
def test_invalid_response_stops_without_effect(configured, payload):
    host = AgentHost(save(configured))
    with pytest.raises(AgentError):
        ReferenceAgent(host, ScriptedClient([payload])).run("A task")
    assert host.status()["payment_total_minor"] == 0
    host.close()


def test_turn_limit_not_success(configured):
    configured[1]["max_turns"] = 1
    host = AgentHost(save(configured))
    result = ReferenceAgent(host, ScriptedClient([
        response("charge", {"order": "first"})])).run("Pay first.")
    assert result["termination"] == "turn_limit"
    assert result["state"]["payment_total_minor"] == 60
    host.close()


def test_call_limit_rejects_whole_batch(configured):
    host = AgentHost(save(configured))
    payload = response("charge", {"order": "first"})
    calls = payload["choices"][0]["message"]["tool_calls"]
    calls[:] = [dict(calls[0], id=f"call-{i}") for i in range(9)]
    result = ReferenceAgent(host, ScriptedClient([payload])).run("Pay.")
    assert result["termination"] == "tool_call_limit"
    assert result["state"]["payment_total_minor"] == 0
    host.close()


def test_http_error_does_not_disclose_credentials(configured, monkeypatch):
    monkeypatch.setenv("ROMP_TEST_KEY", "test-secret")
    def fail(request, **kwargs):
        assert request.get_header("Authorization") == "Bearer test-secret"
        raise urllib.error.HTTPError(request.full_url, 401, "test-secret", {}, None)
    class Opener:
        open = staticmethod(fail)
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    with pytest.raises(AgentError, match="model HTTP status 401") as error:
        ModelClient(save(configured)).complete([], [])
    assert "test-secret" not in str(error.value)


def test_duplicate_tool_ids_reject_batch_before_effect(configured):
    host = AgentHost(save(configured))
    payload = response("charge", {"order": "first"})
    calls = payload["choices"][0]["message"]["tool_calls"]
    calls.append(dict(calls[0]))
    with pytest.raises(AgentError, match="native tool-call"):
        ReferenceAgent(host, ScriptedClient([payload])).run("Pay.")
    assert host.status()["payment_total_minor"] == 0
    host.close()


def test_duplicate_json_arguments_rejected(configured):
    host = AgentHost(save(configured))
    payload = response("charge", {"order": "first"})
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
        '{"order":"first","order":"second"}')
    result = ReferenceAgent(host, ScriptedClient([payload, response()])).run("Pay.")
    assert result["state"]["payment_total_minor"] == 0
    host.close()


def test_redirect_handler_withholds_authorization():
    from romp_asr.reference_agent import _NoRedirect
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.test") is None


def test_model_transport_redacts_echoed_key(configured, monkeypatch):
    monkeypatch.setenv("ROMP_TEST_KEY", "test-secret")
    class Reply:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            payload = response()
            payload["choices"][0]["message"]["content"] = "test-secret"
            return json.dumps(payload).encode()
    class Opener:
        def open(self, request, **kwargs):
            payload = json.loads(request.data)
            assert payload["tool_choice"] == "auto"
            return Reply()
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    result = ModelClient(save(configured)).complete([], [])
    assert result["choices"][0]["message"]["content"] == "[REDACTED]"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink")
def test_state_symlink_rejected(configured):
    config = save(configured)
    other = configured[0].parent / "other"
    other.mkdir()
    config["state_dir"].symlink_to(other, target_is_directory=True)
    with pytest.raises(AgentError, match="symbolic links"):
        AgentHost(config)
