"""Full transcript logging for a desk session — every decision and tool call, on disk.

The round-level trace tells you WHAT happened (score, revise/pass); this tells you WHY: the agent's
thinking, every ``introspect``/``run_bpy``/``render``/``scene_graph`` call with its inputs, and every
result. :func:`make_message_logger` returns an ``on_message`` callback to hand to
:func:`agents.scene_builder.build_scene`; it writes two files:

* ``<name>.transcript.md`` — human-readable, for eyeballing the flow and deciding what to tune;
* ``<name>.messages.jsonl`` — one JSON object per SDK message, for machine analysis.

Base64 image payloads (the render tool's frames) are replaced with a size note so the log stays
readable and small; long text/thinking is clipped. Logging never raises into the run — a failure to
serialize one message is swallowed so it can't kill a live build.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

_MAX_TEXT = 6000
_MAX_THINK = 6000
_MAX_RESULT = 3000
_MAX_STR = 2000  # any bare string longer than this is likely a blob → clip


def _clip(text: Any, limit: int) -> str:
    s = str(text)
    return s if len(s) <= limit else s[:limit] + f"… [+{len(s) - limit} chars]"


def _scrub(obj: Any) -> Any:
    """Recursively strip base64 image data (→ size note) and clip long strings, keep structure."""
    if isinstance(obj, dict):
        if obj.get("type") == "image" and isinstance(obj.get("data"), str):
            return {**{k: v for k, v in obj.items() if k != "data"}, "data": f"<image: {len(obj['data'])} b64 chars>"}
        # anthropic image block shape: {"source": {"type":"base64","data": ...}}
        src = obj.get("source")
        if isinstance(src, dict) and isinstance(src.get("data"), str) and len(src["data"]) > 256:
            src = {**src, "data": f"<image: {len(src['data'])} b64 chars>"}
            return {**obj, "source": src}
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, str) and len(obj) > _MAX_STR:
        return _clip(obj, _MAX_STR)
    return obj


def _render_result_content(content: Any) -> str:
    """A tool result's content → readable text (text parts joined, images noted)."""
    if isinstance(content, str):
        return _clip(content, _MAX_RESULT)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(_clip(item.get("text", ""), _MAX_RESULT))
                elif item.get("type") == "image":
                    data = item.get("data") or (item.get("source") or {}).get("data") or ""
                    parts.append(f"[image: {len(data)} b64 chars]")
                else:
                    parts.append(_clip(item, 400))
            else:
                parts.append(_clip(item, 400))
        return "\n".join(parts)
    return _clip(content, _MAX_RESULT)


def _events(msg: Any) -> list[dict[str, Any]]:
    """Flatten one SDK message into a list of structured events."""
    out: list[dict[str, Any]] = []
    if isinstance(msg, AssistantMessage):
        for b in msg.content or []:
            if isinstance(b, ThinkingBlock):
                out.append({"kind": "thinking", "text": _clip(b.thinking, _MAX_THINK)})
            elif isinstance(b, TextBlock):
                out.append({"kind": "text", "text": _clip(b.text, _MAX_TEXT)})
            elif isinstance(b, ToolUseBlock):
                out.append({"kind": "tool_call", "name": b.name, "id": b.id, "input": _scrub(b.input)})
            else:
                out.append({"kind": type(b).__name__, "data": _scrub(getattr(b, "__dict__", {}))})
    elif isinstance(msg, UserMessage):
        content = msg.content if isinstance(msg.content, list) else [msg.content]
        for b in content:
            if isinstance(b, ToolResultBlock):
                out.append({"kind": "tool_result", "id": b.tool_use_id,
                            "is_error": bool(b.is_error), "content": _render_result_content(b.content)})
            elif isinstance(b, dict) and b.get("type") == "tool_result":
                out.append({"kind": "tool_result", "id": b.get("tool_use_id"),
                            "is_error": bool(b.get("is_error")), "content": _render_result_content(b.get("content"))})
            # plain user text blocks (the initial brief) are already known; skip to keep the log about the desk
    elif isinstance(msg, ResultMessage):
        out.append({"kind": "result", "subtype": msg.subtype, "is_error": bool(msg.is_error),
                    "num_turns": msg.num_turns, "cost_usd": msg.total_cost_usd,
                    "result": _clip(msg.result or "", _MAX_RESULT)})
    elif isinstance(msg, SystemMessage):
        pass  # init/system chatter — noise for decision analysis, drop it from both logs
    else:
        out.append({"kind": type(msg).__name__})
    return out


def _to_markdown(ev: dict[str, Any], ts: str) -> str | None:
    k = ev["kind"]
    if k == "thinking":
        return f"\n### 🧠 thinking · {ts}\n{ev['text']}\n"
    if k == "text":
        return f"\n### 💬 assistant · {ts}\n{ev['text']}\n"
    if k == "tool_call":
        name, inp = ev["name"], ev.get("input") or {}
        short = name.split("__")[-1]
        if short == "run_bpy" and isinstance(inp, dict) and "code" in inp:
            return f"\n#### → run_bpy · {ts}\n```python\n{inp['code']}\n```\n"
        if short == "introspect" and isinstance(inp, dict):
            return f"\n#### → introspect · {ts}\n`{inp.get('expr', '')}`\n"
        return f"\n#### → {short} · {ts}\n`{_clip(inp, 600)}`\n"
    if k == "tool_result":
        flag = "ERR" if ev["is_error"] else "ok"
        return f"\n#### ← result [{flag}] · {ts}\n```\n{ev['content']}\n```\n"
    if k == "result":
        return (f"\n---\n**END** · {ts} · subtype={ev['subtype']} is_error={ev['is_error']} "
                f"turns={ev['num_turns']} cost=${ev['cost_usd']}\n\n> {ev['result']}\n")
    if k == "system":
        return None  # keep the readable log focused; system init is in the jsonl
    return None


def make_message_logger(
    transcript_path: str | Path,
    jsonl_path: str | Path | None = None,
    *,
    title: str = "desk session",
) -> Callable[[object], None]:
    """Return an ``on_message`` callback that appends a readable transcript (and optional JSONL).

    Hand it to ``build_scene(on_message=…)``. Safe to call many times; each call appends. Never
    raises into the run.
    """
    tpath = Path(transcript_path)
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text(f"# {title}\n")
    jpath = Path(jsonl_path) if jsonl_path else None

    def on_message(msg: object) -> None:
        try:
            ts = time.strftime("%H:%M:%S")
            events = _events(msg)
            lines = [md for md in (_to_markdown(ev, ts) for ev in events) if md]
            if lines:
                with tpath.open("a") as fh:
                    fh.write("".join(lines))
            if jpath is not None:
                with jpath.open("a") as fh:
                    for ev in events:
                        fh.write(json.dumps({"ts": ts, **ev}, ensure_ascii=False, default=str) + "\n")
        except Exception:  # logging must never break a live build
            pass

    return on_message
