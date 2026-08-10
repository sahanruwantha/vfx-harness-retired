"""The agent-driven desk (build_scene), with a fake query — no SDK server, no Blender.

We monkeypatch ``agents.scene_builder.query`` with a fake that records the options it was handed and
yields a canned ResultMessage, so we can assert the wiring (tools, mcp server, budget, resume, clean
slate) and the submit / wrap-up / raise branches without running a model or Blender.
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest

import agents.scene_builder as sb
from agents.scene_builder import (
    BLENDER_TOOLS,
    build_scene,
    desk_content,
    revision_content,
)


class FakeBridge:
    def __init__(self) -> None:
        self.render_dir = tempfile.mkdtemp(prefix="desk-bridge-")
        self.code_seen: list[str] = []

    def run_python(self, code: str) -> dict:
        self.code_seen.append(code)
        return {"ok": True, "result": {"clean_slate": True}}


class FakeResult:
    def __init__(
        self,
        *,
        is_error: bool = False,
        subtype: str = "success",
        result: str = "SUBMIT: a lit tower over a city-lights sea.",
        session_id: str = "sess-1",
        num_turns: int = 7,
        total_cost_usd: float = 0.2,
        terminal_reason: str | None = "completed",
        errors: list[str] | None = None,
    ) -> None:
        self.is_error = is_error
        self.subtype = subtype
        self.result = result
        self.session_id = session_id
        self.num_turns = num_turns
        self.total_cost_usd = total_cost_usd
        self.terminal_reason = terminal_reason
        self.errors = errors


def _fake_query(results, calls):
    """A query() stand-in: append each call's kwargs to `calls`, yield the next result in order."""
    state = {"i": 0}

    def query(*, prompt, options):  # noqa: A002 — mirror the SDK keyword signature
        calls.append({"prompt": prompt, "options": options})

        async def gen():
            idx = min(state["i"], len(results) - 1)
            state["i"] += 1
            yield results[idx]

        return gen()

    return query


def _refs():
    return [("image/jpeg", "UkVG")]


# --- content assembly (pure) ---------------------------------------------------------


def test_desk_content_has_brief_reference_and_submit_rule():
    blocks = desk_content(brief="a tower", reference_images=_refs())
    texts = " ".join(b.get("text", "") for b in blocks)
    assert "BRIEF" in texts and "SUBMIT" in texts
    assert sum(1 for b in blocks if b["type"] == "image") == 1


def test_desk_content_animation_mode():
    texts = " ".join(b.get("text", "") for b in desk_content(brief="roll", reference_images=_refs(), animation_frames=36))
    assert "ANIMATION MODE" in texts and "frame_end = 36" in texts


def test_revision_content_carries_the_dailies_notes():
    texts = " ".join(b.get("text", "") for b in revision_content("lighting=FIX, too washed out"))
    assert "DAILIES" in texts and "too washed out" in texts and "SUBMIT" in texts


# --- fresh build ---------------------------------------------------------------------


def test_fresh_build_clears_the_scene_and_wires_the_tools(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(sb, "ResultMessage", FakeResult)
    monkeypatch.setattr(sb, "query", _fake_query([FakeResult()], calls))
    bridge = FakeBridge()

    out = asyncio.run(build_scene(bridge=bridge, brief="a tower", reference_images=_refs()))

    assert out.submitted is True and out.outcome == "complete"
    assert out.note.startswith("SUBMIT:") and out.session_id == "sess-1"
    # fresh build prepares a clean, empty scene
    assert any("clean_slate" in c for c in bridge.code_seen)
    # exactly one desk turn ran; the tools + mcp server + budget are wired, no resume
    assert len(calls) == 1
    opts = calls[0]["options"]
    assert set(opts.allowed_tools) == set(BLENDER_TOOLS)
    assert "blender" in opts.mcp_servers
    assert opts.resume is None
    assert opts.task_budget == {"total": sb.DESK_BUDGET_TOKENS}


def test_bpy_log_starts_empty_when_the_agent_runs_no_tools(monkeypatch):
    # the fake query never invokes the tools, so nothing is recorded — the hook is wired, not firing
    monkeypatch.setattr(sb, "ResultMessage", FakeResult)
    monkeypatch.setattr(sb, "query", _fake_query([FakeResult()], []))
    out = asyncio.run(build_scene(bridge=FakeBridge(), brief="x", reference_images=_refs()))
    assert out.bpy_log == ()


# --- revision (resume) ---------------------------------------------------------------


def test_resume_revision_skips_clean_slate_and_the_budget(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(sb, "ResultMessage", FakeResult)
    monkeypatch.setattr(sb, "query", _fake_query([FakeResult(session_id="sess-1")], calls))
    bridge = FakeBridge()

    out = asyncio.run(build_scene(
        bridge=bridge, brief="a tower", reference_images=_refs(),
        resume="sess-1", feedback="lighting=FIX",
    ))

    assert out.submitted is True
    # a revision edits the existing scene — it must NOT clear it
    assert not any("clean_slate" in c for c in bridge.code_seen)
    opts = calls[0]["options"]
    assert opts.resume == "sess-1"
    assert opts.task_budget is None  # budget only bounds the fresh build


# --- limits ---------------------------------------------------------------------------


def test_recoverable_limit_wraps_up(monkeypatch):
    calls: list[dict] = []
    results = [
        FakeResult(is_error=True, subtype="error_max_turns", result="", terminal_reason="max_turns"),
        FakeResult(result="Built a tower and city lights; lighting still rough.", session_id="sess-1", num_turns=2),
    ]
    monkeypatch.setattr(sb, "ResultMessage", FakeResult)
    monkeypatch.setattr(sb, "query", _fake_query(results, calls))

    out = asyncio.run(build_scene(bridge=FakeBridge(), brief="a tower", reference_images=_refs()))

    assert out.submitted is False and out.outcome == "wrapped_up"
    assert "lighting still rough" in out.note
    assert out.turns == 7 + 2  # main + wrap-up turns summed
    assert len(calls) == 2
    assert calls[1]["options"].allowed_tools == []  # wrap-up has no tools


def test_non_recoverable_error_raises(monkeypatch):
    monkeypatch.setattr(sb, "ResultMessage", FakeResult)
    monkeypatch.setattr(sb, "query", _fake_query([FakeResult(is_error=True, subtype="error_other", errors=["boom"])], []))
    with pytest.raises(RuntimeError, match="scene desk failed"):
        asyncio.run(build_scene(bridge=FakeBridge(), brief="x", reference_images=_refs()))
