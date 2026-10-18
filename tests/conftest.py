"""Classify regression coverage without changing test locations or assertions."""

from pathlib import Path

import pytest


GROUPS = {
    "dsl_current": {"test_policy_v02.py"},
    "runtime_support": {"test_tool_backends.py"},
    "cli_current": {"test_v02_migration.py"},
    "agent_current": {"test_reference_agent.py"},
}


def pytest_configure(config):
    for group in GROUPS:
        config.addinivalue_line("markers", f"{group}: regression category")


def pytest_collection_modifyitems(items):
    for item in items:
        filename = Path(str(item.path)).name
        for group, files in GROUPS.items():
            if filename in files:
                item.add_marker(getattr(pytest.mark, group))
