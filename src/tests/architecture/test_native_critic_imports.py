"""Production critic modules cannot restore the superseded Claude transport."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / 'vfx_harness'


@pytest.mark.parametrize('path', [
    'agents/critic_session.py', 'agents/critic_transport.py', 'agents/builder/critic.py',
    'agents/builder/critic_focus.py', 'agents/builder/axes.py', 'domain/critic_verdict.py',
])
def test_native_critic_has_no_legacy_transport_import(path):
    tree = ast.parse((ROOT / path).read_text())
    forbidden = ('claude_agent_sdk', 'vfx_harness.agents.sdk_options',
                 'vfx_harness.agents.builder.drain', 'vfx_harness.agents.model_stream')
    imports = [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports.extend(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert not [name for name in imports if any(name == prefix or name.startswith(prefix + '.')
                                              for prefix in forbidden)]
