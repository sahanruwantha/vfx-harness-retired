"""Diagnostic applicability follows the unit's typed look authority (HIR-0174)."""

from __future__ import annotations

from vfx_harness.observability import log


def test_render_pass_is_not_required_of_an_executable_only_unit(monkeypatch) -> None:
    monkeypatch.setattr(log, "TOOL_USE", type(log.TOOL_USE)())
    log.TOOL_USE[log._MCP + "check_scene"] = 3
    log.TOOL_USE[log._MCP + "run_bpy"] = 4

    lookless = log.tool_use_summary(look_feedback_applicable=False)
    assert lookless["applicability"]["render_pass"] is False
    assert "render_pass" not in lookless["unused_required_tools"]
    assert "render_pass" in lookless["not_applicable_tools"]

    look_owning = log.tool_use_summary(look_feedback_applicable=True)
    assert look_owning["applicability"]["render_pass"] is True
    assert "render_pass" in look_owning["unused_required_tools"]

    revalidation = log.tool_use_summary(look_feedback_applicable=True, revalidation=True)
    assert revalidation["applicability"]["render_pass"] is False
