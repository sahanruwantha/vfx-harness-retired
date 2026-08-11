"""Tiny flushing, timestamped logger + a rich pretty-printer for SDK messages.

Everything flushes immediately (default `print` block-buffers when redirected to a
file, which hid all our output). `log_message` unpacks an Agent SDK message into
readable lines: reasoning/thinking, agent text, tool calls with inputs, tool
results, and the final cost/duration.
"""

from __future__ import annotations

import json
import time

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

# Optional block/message types — import defensively across SDK versions.
try:
    from claude_agent_sdk import ThinkingBlock
except Exception:  # noqa: BLE001
    ThinkingBlock = None
try:
    from claude_agent_sdk import UserMessage, ToolResultBlock
except Exception:  # noqa: BLE001
    UserMessage = ToolResultBlock = None
try:
    from claude_agent_sdk import SystemMessage
except Exception:  # noqa: BLE001
    SystemMessage = None

_t0: float | None = None


def _elapsed() -> float:
    global _t0
    now = time.monotonic()
    if _t0 is None:
        _t0 = now
    return now - _t0


def log(msg: str, indent: int = 0) -> None:
    """Timestamped, immediately-flushed log line."""
    print(f"[{_elapsed():6.1f}s] {'  ' * indent}{msg}", flush=True)


def _clip(s: str, n: int) -> str:
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[:n] + "…"


def _result_text(block) -> str:
    content = getattr(block, "content", "")
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                parts.append(c.get("text", "") if c.get("type") == "text"
                             else f"[{c.get('type', 'block')}]")
            else:
                parts.append(str(c))
        return " ".join(p for p in parts if p)
    return str(content)


def log_message(m) -> None:
    """Pretty-print one SDK message: reasoning, text, tool calls, results, result."""
    if SystemMessage is not None and isinstance(m, SystemMessage):
        if getattr(m, "subtype", None) == "init":
            data = getattr(m, "data", {}) or {}
            sid = str(data.get("session_id", "?"))[:8]
            log(f"● session {sid} started (model {data.get('model', '?')})")
        return

    if isinstance(m, AssistantMessage):
        for b in m.content:
            if ThinkingBlock is not None and isinstance(b, ThinkingBlock):
                for line in b.thinking.strip().splitlines():
                    if line.strip():
                        log(f"🧠 {line.strip()}", 1)
            elif isinstance(b, TextBlock) and b.text.strip():
                for line in b.text.strip().splitlines():
                    if line.strip():
                        log(f"💬 {line.strip()}", 1)
            elif isinstance(b, ToolUseBlock):
                log(f"→ {b.name}  {_clip(json.dumps(b.input, default=str), 400)}", 1)
        return

    if UserMessage is not None and isinstance(m, UserMessage):
        for b in getattr(m, "content", []) or []:
            if ToolResultBlock is not None and isinstance(b, ToolResultBlock):
                err = " (error)" if getattr(b, "is_error", False) else ""
                log(f"←{err} {_clip(_result_text(b), 300)}", 1)
        return

    if isinstance(m, ResultMessage):
        cost = getattr(m, "total_cost_usd", None)
        dur = getattr(m, "duration_ms", None)
        turns = getattr(m, "num_turns", None)
        bits = [f"subtype={getattr(m, 'subtype', '?')}"]
        if turns is not None:
            bits.append(f"turns={turns}")
        if dur is not None:
            bits.append(f"dur={dur / 1000:.1f}s")
        if cost is not None:
            bits.append(f"cost=${cost:.4f}")
        log(f"■ done: {'  '.join(bits)}")
