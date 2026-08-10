"""The desk transcript logger — serialization, base64 scrubbing, and file output (no SDK run)."""

from __future__ import annotations

import json

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from agents.desk_log import _render_result_content, _scrub, make_message_logger


def test_scrub_replaces_base64_image_data_with_a_size_note():
    scrubbed = _scrub({"type": "image", "data": "A" * 500, "mimeType": "image/jpeg"})
    assert scrubbed["data"].startswith("<image:") and "500 b64" in scrubbed["data"]
    # anthropic source-shaped image too
    s = _scrub({"type": "image", "source": {"type": "base64", "data": "B" * 500}})
    assert "b64 chars" in s["source"]["data"]


def test_render_result_content_notes_images_and_keeps_text():
    rendered = _render_result_content([{"type": "text", "text": "luma=0.4"}, {"type": "image", "data": "Z" * 300}])
    assert "luma=0.4" in rendered and "[image: 300 b64 chars]" in rendered


def test_logger_writes_thinking_calls_results_and_end(tmp_path):
    tpath, jpath = tmp_path / "r.md", tmp_path / "r.jsonl"
    log = make_message_logger(tpath, jpath, title="test session")

    log(AssistantMessage(content=[
        TextBlock(text="I'll build the green tower first."),
        ToolUseBlock(id="t1", name="mcp__blender__run_bpy", input={"code": "import bpy\nbpy.ops.mesh.primitive_cube_add()"}),
    ], model="claude"))
    log(UserMessage(content=[
        ToolResultBlock(tool_use_id="t1", content=[{"type": "text", "text": "ran OK"}], is_error=False),
    ]))
    log(AssistantMessage(content=[
        ToolUseBlock(id="t2", name="mcp__blender__render", input={"label": "look"}),
    ], model="claude"))
    log(UserMessage(content=[
        ToolResultBlock(tool_use_id="t2", content=[{"type": "text", "text": "luma_mean=0.4"}, {"type": "image", "data": "Q" * 400}], is_error=False),
    ]))
    log(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                      num_turns=5, session_id="s1", total_cost_usd=0.12, result="SUBMIT: done"))

    md = tpath.read_text()
    # decision text + the actual code the agent ran are captured
    assert "build the green tower first" in md
    assert "```python\nimport bpy" in md and "primitive_cube_add" in md
    # results captured, image scrubbed (no raw base64 blob in the readable log)
    assert "luma_mean=0.4" in md and "[image: 400 b64 chars]" in md
    assert "Q" * 400 not in md
    assert "END" in md and "SUBMIT: done" in md

    # jsonl has one object per event, parseable, with the tool_call code preserved
    rows = [json.loads(line) for line in jpath.read_text().splitlines()]
    kinds = [r["kind"] for r in rows]
    assert "text" in kinds and "tool_call" in kinds and "tool_result" in kinds and "result" in kinds
    call = next(r for r in rows if r["kind"] == "tool_call" and r["name"].endswith("run_bpy"))
    assert "primitive_cube_add" in call["input"]["code"]


def test_logger_never_raises_on_a_weird_message(tmp_path):
    log = make_message_logger(tmp_path / "r.md", None)
    log(object())  # not an SDK message — must be swallowed
    log(None)
    assert (tmp_path / "r.md").exists()
