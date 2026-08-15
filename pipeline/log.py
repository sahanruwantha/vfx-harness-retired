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


# Display caps. Ordinary tool results stay short (they are mostly scene stats we can
# re-derive); errors get a far bigger budget because the log is the ONLY record of a
# failed run once the process is gone.
_OK_CHARS = 300
_ERR_CHARS = 2400
_ARG_CHARS = 400
_SCRIPT_LINES = 120
_SCRIPT_CHARS = 6000

# Tool inputs whose value is code the operator needs verbatim to read the log at all.
_CODE_KEYS = ("script", "code")


def _clip(s: str, n: int, keep: str = "head") -> str:
    """One-line, length-capped rendering. keep='tail' drops the FRONT instead.

    Tracebacks are head-heavy boilerplate ("Traceback…", the serve() frame, the exec
    frame) and tail-light where it matters: the innermost frame, the exception, and any
    HINT we attached. Clipping those from the head threw away the only diagnostic part,
    so error text is clipped from the front instead.
    """
    s = s.replace("\n", " ⏎ ")
    if len(s) <= n:
        return s
    return "…" + s[-n:] if keep == "tail" else s[:n] + "…"


def _log_tool_use(b) -> None:
    """Log a tool call. Code payloads print verbatim; everything else is clipped.

    run_bpy scripts are routinely thousands of characters, so a 400-char cap showed the
    imports and nothing else — the log recorded that a script ran but never what it did.
    Raising the cap for ALL tools would flood the log with file-write payloads, so only
    script/code arguments get the big, line-by-line treatment.
    """
    args = dict(b.input) if isinstance(b.input, dict) else {"input": b.input}
    code = next((args.pop(k) for k in _CODE_KEYS
                 if isinstance(args.get(k), str) and "\n" in args[k]), None)
    rest = _clip(json.dumps(args, default=str), _ARG_CHARS) if args else ""
    log(f"→ {b.name}  {rest}".rstrip(), 1)
    if code is None:
        return
    lines = code[:_SCRIPT_CHARS].splitlines()
    for line in lines[:_SCRIPT_LINES]:
        log(f"│ {line}", 2)
    dropped = len(code.splitlines()) - len(lines[:_SCRIPT_LINES])
    if dropped > 0:
        log(f"│ … {dropped} more line(s)", 2)


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
                _log_tool_use(b)
        return

    if UserMessage is not None and isinstance(m, UserMessage):
        for b in getattr(m, "content", []) or []:
            if ToolResultBlock is not None and isinstance(b, ToolResultBlock):
                is_err = bool(getattr(b, "is_error", False))
                err = " (error)" if is_err else ""
                text = (_clip(_result_text(b), _ERR_CHARS, keep="tail") if is_err
                        else _clip(_result_text(b), _OK_CHARS))
                log(f"←{err} {text}", 1)
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
