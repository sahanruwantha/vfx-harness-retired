"""Tiny flushing, timestamped logger + a rich pretty-printer for SDK messages.

Everything flushes immediately (default `print` block-buffers when redirected to a
file, which hid all our output). `log_message` unpacks an Agent SDK message into
readable lines: reasoning/thinking, agent text, tool calls with inputs, tool
results, and the final cost/duration.

It also forwards every message to `transcript`, which writes the DURABLE record. The two
have different jobs and therefore different clipping: the console is for a human watching
a run and must stay readable, so scripts stop at 120 lines; the transcript is grepped and
diffed after the fact, so it keeps them whole. Both are driven from here because this is
the one function all five drain paths already call.
"""

from __future__ import annotations

import json
from collections import Counter
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
_OK_CHARS = 700
_ERR_CHARS = 2400
_ARG_CHARS = 400
_SCRIPT_LINES = 120
_SCRIPT_CHARS = 6000

# Tool inputs whose value is code the operator needs verbatim to read the log at all.
_CODE_KEYS = ("script", "code")


def _clip(s: str, n: int, keep: str = "head") -> str:
    """One-line, length-capped rendering.

    keep='tail' drops the FRONT. Tracebacks are head-heavy boilerplate ("Traceback…", the
    serve() frame, the exec frame) and tail-light where it matters: the innermost frame,
    the exception, and any HINT we attached. Clipping those from the head threw away the
    only diagnostic part.

    keep='ends' elides the MIDDLE, because a compare_frame result is informative at both
    ends and dull in between: the caption and exposure line open it, and the signed gap
    plus any scale-change warning close it. Head-clipping at 300 cut off exactly the
    feedback added to make the builder converge — so the log showed a comparison happening
    and hid what it said. Same failure as the traceback clip, one layer along.
    """
    s = s.replace("\n", " ⏎ ")
    if len(s) <= n:
        return s
    if keep == "tail":
        return "…" + s[-n:]
    if keep == "ends":
        head, tail = n * 2 // 5, n - (n * 2 // 5)
        return f"{s[:head]}…[{len(s) - n} chars]…{s[-tail:]}"
    return s[:n] + "…"


# Which tools an agent reached for, counted as they go past. Every drain path already
# routes through log_message, so this is the one place that sees all of them.
#
# WHY THIS IS WORTH RECORDING. The four layers that passed barrel_roll called
# compare_frame — the tool that puts a render NEXT TO its reference — 7 to 41 times each.
# The one that failed three times called it 3 to 5 times and called measure_regions 17 to
# 44 times instead. It is the only layer where measuring outnumbered looking, and it
# optimised its way onto the target numbers with a render that still did not match the
# picture. The ratio looks like a leading indicator of a layer in trouble, and until now
# it was recoverable only by grepping a console log that does not survive the session.
TOOL_USE: Counter = Counter()

_MCP = "mcp__blender__"

# LOOKING: tools that put pixels in front of the model. `render_pass` belongs here and
# not in a category of its own — a diffuse-direct or clay render is still the model
# looking at the frame, just at the channel its axis is about.
_LOOK = ("compare_frame", "render_frame", "render_frames", "render_pass", "diff_frames")
# MEASURING: reducing the frame to numbers the model then optimises against.
_MEASURE = ("measure_regions", "measure_ref")
# VERIFYING: judgment-free facts about the SCENE rather than the image — visibility,
# framing, motion, mesh, scale. Deliberately a THIRD category, not folded into
# `measured`, because the failure mode the look/measure ratio detects is optimising
# against self-chosen image statistics, and `check_scene` is the opposite of that: it
# answers a question with one right answer that the builder did not get to pick.
_VERIFY = ("check_scene",)
# The Phase 1/2 additions, tracked by name so "did the builder ever reach for them" is
# answerable from the ledger instead of by grepping a transcript.
_NEW_TOOLS = ("render_pass", "check_scene", "diff_frames")


def reset_tool_use() -> None:
    TOOL_USE.clear()


def tool_use_summary() -> dict:
    """Per-tool counts, the look-vs-measure ratio layer outcomes correlate with, and
    whether the newer diagnostic tools were used at all."""
    if not TOOL_USE:
        return {}

    def n(*names):
        return sum(TOOL_USE.get(_MCP + x, 0) for x in names)

    looked, measured, verified = n(*_LOOK), n(*_MEASURE), n(*_VERIFY)
    out = {"calls": dict(TOOL_USE.most_common()), "total": sum(TOOL_USE.values()),
           "looked": looked, "measured": measured, "verified": verified,
           "compared": n("compare_frame"),
           # Adoption, per tool. A zero here is not neutral: it means a capability that
           # was built, verified against Blender and documented in the builder prompt is
           # being ignored, and the prompt is the thing to fix — not the tool.
           "adoption": {t: TOOL_USE.get(_MCP + t, 0) for t in _NEW_TOOLS},
           "unused_new_tools": [t for t in _NEW_TOOLS if not TOOL_USE.get(_MCP + t)]}
    if measured:
        out["look_per_measure"] = round(looked / measured, 2)
    return out


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
    """Pretty-print one SDK message: reasoning, text, tool calls, results, result.

    Also appends it to the durable transcript when one is bound (see `transcript.bind`).
    """
    from . import transcript
    transcript.message(m)

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
                TOOL_USE[b.name] += 1
                _log_tool_use(b)
        return

    if UserMessage is not None and isinstance(m, UserMessage):
        for b in getattr(m, "content", []) or []:
            if ToolResultBlock is not None and isinstance(b, ToolResultBlock):
                is_err = bool(getattr(b, "is_error", False))
                err = " (error)" if is_err else ""
                text = (_clip(_result_text(b), _ERR_CHARS, keep="tail") if is_err
                        else _clip(_result_text(b), _OK_CHARS, keep="ends"))
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
