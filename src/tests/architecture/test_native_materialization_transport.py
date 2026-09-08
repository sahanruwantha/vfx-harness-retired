"""The production materializer cannot regain Claude transport or retry hooks."""

import ast
from pathlib import Path

import pytest


@pytest.mark.parametrize("relative", [
    "agents/materialization_runtime.py", "agents/materialization_session.py",
    "agents/planner/rematerialize.py", "agents/planner/kickoff.py",
])
def test_materialization_owns_no_claude_transport_import(relative):
    root = Path(__file__).parents[2] / "vfx_harness"
    tree = ast.parse((root / relative).read_text())
    forbidden = {
        "claude_agent_sdk", "vfx_harness.agents.sdk_options",
        "vfx_harness.agents.resilience", "vfx_harness.agents.model_stream",
        "vfx_harness.agents.plan_guardrails",
    }
    for node in ast.walk(tree):
        names = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                 else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
        assert not any(name == item or name.startswith(item + ".") for name in names for item in forbidden)
