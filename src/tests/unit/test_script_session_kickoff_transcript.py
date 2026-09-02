"""Every script session journals its kickoff and continuation prompts (HIR-0174)."""

from __future__ import annotations

import inspect

from vfx_harness.agents.builder import script_agent


def test_script_agent_records_each_prompt_before_querying() -> None:
    source = inspect.getsource(script_agent._run_script_agent)
    kickoff = source.index("transcript.prompt(\n            prompt,")
    assert kickoff < source.index("await agent.query(prompt)")
    assert 'role="kickoff"' in source
    continuation = source.index("transcript.prompt(\n                continuation,")
    assert continuation < source.index("await agent.query(continuation)")
    assert 'role="continuation"' in source
