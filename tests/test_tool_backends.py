"""Carry useful old backend regressions forward through the generic executor."""

from concurrent.futures import ThreadPoolExecutor
import os
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from romp_asr import (
    compile_policy, PolicyExecutor, LocalPaymentLedger, PosixFileTools,
    charge_adapter, delete_adapter,
)


def policy():
    return compile_policy((Path(__file__).resolve().parents[1] /
                           "examples" / "policy_v02.json").read_bytes())


def test_same_request_concurrently_invokes_once(tmp_path):
    payments = LocalPaymentLedger(tmp_path / "payments.db")
    calls = []

    class Backend:
        def charge(self, identifier, amount):
            calls.append(identifier)
            payments.charge(identifier, amount)

    executor = PolicyExecutor(policy(), tmp_path / "requests.db",
                              {"Charge": charge_adapter(Backend())})
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: executor.submit(
            "same", "alice", "Charge", {"amount_minor": 60}), range(2)))
    assert {r.outcome for r in results} <= {"SUCCEEDED", "UNKNOWN"}
    assert calls == ["same"]
    assert payments.total == 60
    assert executor.submit("same", "bob", "Charge", {"amount_minor": 60}).outcome == "REJECT"


@pytest.mark.parametrize("amount", [True, 0, -1, 101, 1.5, "60", 2**63])
def test_invalid_or_excessive_amount_has_no_effect(tmp_path, amount):
    payments = LocalPaymentLedger(tmp_path / "payments.db")
    executor = PolicyExecutor(policy(), tmp_path / "requests.db",
                              {"Charge": charge_adapter(payments)})
    assert executor.submit("a", "alice", "Charge", {"amount_minor": amount}).outcome == "REJECT"
    assert payments.total == 0


@pytest.fixture
def filesystem(tmp_path):
    if os.name != "posix":
        pytest.skip("POSIX descriptor backend")
    workspace, system = tmp_path / "workspace", tmp_path / "system"
    workspace.mkdir()
    system.mkdir()
    (workspace / "draft").write_text("draft")
    (system / "core").write_text("protected")
    tools = PosixFileTools({"workspace": workspace, "system": system})
    try:
        yield tools, workspace, system
    finally:
        tools.close()


@pytest.mark.parametrize("path", ["../system/core", "/system/core", "./draft",
                                  "x/../draft", "x//draft", "x\\draft"])
def test_path_rejections_preserve_files(tmp_path, filesystem, path):
    tools, workspace, system = filesystem
    executor = PolicyExecutor(policy(), tmp_path / "requests.db",
                              {"Delete": delete_adapter(tools, ["system"])})
    assert executor.submit("a", "a", "Delete", {"root": "workspace", "path": path}).outcome == "REJECT"
    assert (workspace / "draft").exists() and (system / "core").exists()


def test_root_mapping_checks(filesystem):
    tools, workspace, _ = filesystem
    with pytest.raises(ValueError, match="overlap"):
        PosixFileTools({"a": workspace, "b": workspace})
    with pytest.raises(ValueError, match="mapped"):
        delete_adapter(tools, ["missing"])


def test_protected_root_has_no_unlink_and_retry_preserves_replacement(tmp_path, filesystem):
    tools, workspace, system = filesystem

    class Backend:
        roots = tools.roots
        calls = 0

        def delete(self, root, path, *, validate_only=False):
            if not validate_only:
                self.calls += 1
            tools.delete(root, path, validate_only=validate_only)
            if not validate_only:
                raise TimeoutError()

    backend = Backend()
    executor = PolicyExecutor(policy(), tmp_path / "requests.db",
                              {"Delete": delete_adapter(backend, ["system"])})
    assert executor.submit("protected", "a", "Delete", {"root": "system", "path": "core"}).outcome == "REJECT"
    assert backend.calls == 0 and (system / "core").exists()
    args = {"root": "workspace", "path": "draft"}
    assert executor.submit("delete", "a", "Delete", args).outcome == "UNKNOWN"
    (workspace / "draft").write_text("replacement")
    assert executor.submit("delete", "a", "Delete", args).outcome == "UNKNOWN"
    assert (workspace / "draft").read_text() == "replacement"
    assert backend.calls == 1


def test_successful_delete_retry_does_not_delete_replacement(tmp_path, filesystem):
    tools, workspace, _ = filesystem
    executor = PolicyExecutor(policy(), tmp_path / "requests.db",
                              {"Delete": delete_adapter(tools, ["system"])})
    args = {"root": "workspace", "path": "draft"}
    assert executor.submit("a", "a", "Delete", args).outcome == "SUCCEEDED"
    (workspace / "draft").write_text("replacement")
    assert executor.submit("a", "a", "Delete", args).outcome == "SUCCEEDED"
    assert (workspace / "draft").read_text() == "replacement"


@pytest.mark.parametrize("timeout", [False, True])
def test_non_idempotent_backend_invocation_and_effects(tmp_path, timeout):
    class Backend:
        calls = 0
        effects = []

        def charge(self, identifier, amount):
            self.calls += 1
            self.effects.append(amount)  # Deliberately no identifier deduplication.
            if timeout:
                raise TimeoutError()

    backend = Backend()
    adapters = {"Charge": charge_adapter(backend)}
    executor = PolicyExecutor(policy(), tmp_path / "requests.db", adapters)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: executor.submit(
            "same", "alice", "Charge", {"amount_minor": 60}), range(4)))
    assert {r.outcome for r in results} <= {"SUCCEEDED", "UNKNOWN"}
    restarted = PolicyExecutor(policy(), tmp_path / "requests.db", adapters)
    assert restarted.submit("same", "alice", "Charge", {"amount_minor": 60}).outcome == (
        "UNKNOWN" if timeout else "SUCCEEDED")
    assert restarted.submit("over", "alice", "Charge", {"amount_minor": 50}).outcome == "REJECT"
    assert backend.calls == 1
    assert backend.effects == [60]


def test_sqlite_configuration_and_wal_rejection(tmp_path):
    executor = PolicyExecutor(policy(), tmp_path / "requests.db", {})
    with executor._connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    with sqlite3.connect(tmp_path / "wal.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
    with pytest.raises(ValueError, match="DELETE"):
        PolicyExecutor(policy(), tmp_path / "wal.db", {})


def test_process_exit_after_effect_retains_pending_and_suppresses_retry(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "policy_v02.json"
    source = """
import os,sys,sqlite3
from pathlib import Path
from romp_asr import compile_policy,PolicyExecutor,charge_adapter
base=Path(sys.argv[1])
class Backend:
    def charge(self, identifier, amount):
        with sqlite3.connect(base/'effects.db') as db:
            db.execute('CREATE TABLE IF NOT EXISTS effects (amount INTEGER)')
            db.execute('INSERT INTO effects VALUES (?)',(amount,))
        os._exit(17)
executor=PolicyExecutor(compile_policy(Path(sys.argv[2]).read_bytes()),
                       base/'requests.db',{'Charge':charge_adapter(Backend())})
executor.submit('same','alice','Charge',{'amount_minor':60})
"""
    result = subprocess.run([sys.executable, "-c", source, str(tmp_path), str(example)],
                            env={**os.environ, "PYTHONPATH": str(example.parents[1] / "src")})
    assert result.returncode == 17
    calls = []

    class Backend:
        def charge(self, identifier, amount):
            calls.append(amount)

    executor = PolicyExecutor(policy(), tmp_path / "requests.db",
                              {"Charge": charge_adapter(Backend())})
    assert executor.submit("same", "alice", "Charge", {"amount_minor": 60}).outcome == "UNKNOWN"
    assert executor.submit("over", "alice", "Charge", {"amount_minor": 50}).outcome == "REJECT"
    with sqlite3.connect(tmp_path / "effects.db") as db:
        assert db.execute("SELECT amount FROM effects").fetchall() == [(60,)]
    assert calls == []


def test_file_prohibition_is_policy_not_backend(tmp_path, filesystem):
    tools, _, system = filesystem
    document = json.loads(policy().canonical)
    document["rules"] = [r for r in document["rules"] if r["id"] != "protected"]
    executor = PolicyExecutor(compile_policy(json.dumps(document)), tmp_path / "requests.db",
                              {"Delete": delete_adapter(tools, ["system"])})
    assert executor.submit("a", "a", "Delete", {"root": "system", "path": "core"}).outcome == "SUCCEEDED"
    assert not (system / "core").exists()
