"""The durable record of what an agent was told, what it said, and what it did.

`log.log_message` already pretty-prints every SDK message — to STDOUT, which nobody
redirects. `run_shot` calls each stage with `subprocess.call` and inherited streams, so
the entire reasoning trace of a $6 layer lives in a terminal scrollback and dies with the
window. What survives is `logs/run_layerN.json`: real aggregates (rounds, cost, tool
counts) but no inputs, no outputs, no tool arguments. So every question of the form "what
did the builder actually see when it decided that?" has been answered by re-reading the
SDK's own session files outside the repo, or not at all.

That is the gap this closes. One JSONL line per event, appended and flushed as it
happens, so a crashed run leaves a complete record up to the crash.

WHAT IS DELIBERATELY NOT STORED: base64 image payloads. Renders reach the model as
~300-450KB base64 blobs and the critic attaches up to four per call; storing them would
put hundreds of MB of pixels into a file whose value is the TEXT. Every image becomes a
one-line placeholder recording its size and mime type, so the record still says "an image
of this size was attached here" — which is the part you need when a verdict looks wrong.
The renders themselves are already on disk under `renders/`.

WHAT IS DELIBERATELY STORED IN FULL: tool arguments, especially `run_bpy` scripts. The
console clips them to 120 lines because a terminal is unreadable otherwise; this file is
not read by a human top-to-bottom, it is grepped and diffed, and a clipped script cannot
be diffed against the next attempt. Clipping the console and clipping the record are
different decisions and this module makes the second one differently.

Reading it back:

    python -m pipeline.inspect_run <shot>            # the digest
    jq -r 'select(.kind=="tool_use") | .tool' logs/transcript/*.jsonl | sort | uniq -c
    jq -r 'select(.kind=="critic") | "\\(.frame) \\(.mean) \\(.verdict)"' logs/transcript/*.jsonl
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# One binding per process. Each stage is its own process (see run_shot), so a module-level
# destination is the honest shape here — there is never more than one agent transcript in
# flight, and threading a writer through five call sites would be ceremony for nothing.
_STATE: dict = {}

# A base64 JPEG of a 2048px render is ~450_000 chars. Anything remotely near that in a
# transcript is a payload, not prose.
_B64_HINT = 2048

# Non-code strings are capped generously: a tool result carrying a full scene dump is
# worth keeping, a runaway one is not.
_MAX_TEXT = 40_000
# Code (run_bpy scripts, file writes) is the artifact — the whole point of the record.
_MAX_CODE = 200_000
_CODE_KEYS = ("script", "code", "content", "new_string", "old_string")

# Past this, something is wrong with what we are recording rather than with the run.
_WARN_BYTES = 64 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def bind(shot_folder: str | Path, stage: str, label: str | None = None,
         run_id: str | None = None) -> Path | None:
    """Start recording. Returns the transcript path, or None if disabled.

    `BVFX_NO_TRANSCRIPT=1` turns it off — for the test suite and for anyone who does not
    want a build script's source mirrored into a second file.
    """
    if os.environ.get("BVFX_NO_TRANSCRIPT"):
        _STATE.clear()
        return None
    folder = Path(shot_folder) / "logs" / "transcript"
    folder.mkdir(parents=True, exist_ok=True)
    rid = run_id or os.environ.get("BVFX_RUN_ID") or "norun"
    name = f"{stage}{f'-{label}' if label else ''}-{rid}.jsonl"
    path = folder / name
    _STATE.clear()
    _STATE.update({"path": path, "stage": stage, "label": label, "run_id": rid,
                   "seq": 0, "t0": time.monotonic(), "warned": False})
    # Appended, never truncated: a resumed or retried layer is part of the same story,
    # and silently dropping the first attempt is how "it worked the first time" becomes
    # unfalsifiable.
    _emit("open", stage=stage, label=label, run_id=rid, at=_now(),
          argv_hint=os.environ.get("BVFX_STAGE_ARGV"))
    return path


def unbind() -> None:
    if _STATE:
        _emit("close", at=_now())
    _STATE.clear()


def path() -> Path | None:
    return _STATE.get("path")


def is_bound() -> bool:
    return bool(_STATE)


def _scrub(obj, *, code: bool = False):
    """Strip image payloads, cap everything else. Recursive, order-preserving."""
    if isinstance(obj, dict):
        # The two shapes the SDK uses for an attached image:
        #   {"type":"image","data":<b64>,"mimeType":...}                 (tool results)
        #   {"type":"image","source":{"type":"base64","data":<b64>,...}} (user messages)
        if obj.get("type") == "image":
            return {"type": "image", "omitted": "base64 payload",
                    "bytes": _image_bytes(obj),
                    "mime": obj.get("mimeType")
                            or (obj.get("source") or {}).get("media_type")}
        return {k: _scrub(v, code=code or k in _CODE_KEYS) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, code=code) for v in obj]
    if isinstance(obj, str):
        # A long opaque string in a non-code field is a payload that dodged the type tag.
        if not code and len(obj) > _B64_HINT and " " not in obj[:_B64_HINT]:
            return f"<omitted {len(obj)} chars of payload>"
        cap = _MAX_CODE if code else _MAX_TEXT
        return obj if len(obj) <= cap else obj[:cap] + f"…<clipped {len(obj) - cap}>"
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _scrub(str(obj), code=code)


def _image_bytes(block: dict) -> int:
    data = block.get("data") or (block.get("source") or {}).get("data") or ""
    return len(data) if isinstance(data, str) else 0


def _emit(kind: str, **fields) -> None:
    st = _STATE
    if not st:
        return
    st["seq"] += 1
    rec = {"seq": st["seq"], "dt": round(time.monotonic() - st["t0"], 2), "kind": kind}
    rec.update(_scrub(fields))
    line = json.dumps(rec, default=str, ensure_ascii=False)
    p: Path = st["path"]
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as e:
        # Never let recording break a run — but say so once, because a transcript that
        # silently stopped being written is worse than none: it looks like the run went
        # quiet exactly when it got interesting.
        if not st.get("warned"):
            st["warned"] = True
            print(f"! transcript write failed ({e}) — {p} is now INCOMPLETE", flush=True)
        return
    if not st.get("size_warned"):
        try:
            if p.stat().st_size > _WARN_BYTES:
                st["size_warned"] = True
                print(f"! transcript {p.name} exceeds {_WARN_BYTES // 1024 // 1024}MB — "
                      f"something is being recorded that should not be", flush=True)
        except OSError:
            pass


def event(kind: str, **fields) -> None:
    """Record a structured event the SDK does not produce.

    Critic verdicts, scene checks and layer boundaries are the highest-value lines in the
    file and none of them is an SDK message: `_critique` consumes its own stream without
    going through `log_message`, so the judge's answer — the thing every control decision
    hangs on — was the one output with no durable home.
    """
    _emit(kind, **fields)


def message(m) -> None:
    """Record one Agent SDK message. Called from `log.log_message`, which every drain
    path already funnels through — so binding once covers the builder, the planner, the
    approach reviewer, the asset agent and the distiller with no further wiring."""
    if not _STATE:
        return
    name = type(m).__name__

    content = getattr(m, "content", None)
    # A UserMessage's content is a LIST of blocks when it carries tool results and a bare
    # STRING when it is a plain turn — which is what the continuation nudges in `_drain`
    # are. Handling only the list shape dropped those silently, i.e. it lost part of the
    # INPUT, which is the half of the record that cannot be reconstructed from anywhere.
    if isinstance(content, str):
        if content.strip():
            _emit("user_text", text=content)
        return
    if isinstance(content, list):
        if getattr(m, "error", None):
            _emit("assistant_error", error=getattr(m, "error"))
        if getattr(m, "stop_reason", None) not in (None, "end_turn"):
            _emit("stop", reason=getattr(m, "stop_reason"), model=getattr(m, "model", None))
        for b in content:
            bname = type(b).__name__
            if bname == "ThinkingBlock":
                _emit("thinking", text=getattr(b, "thinking", ""))
            elif bname == "TextBlock":
                text = getattr(b, "text", "")
                if text.strip():
                    _emit("text", text=text)
            elif bname == "ToolUseBlock":
                _emit("tool_use", tool=getattr(b, "name", "?"),
                      id=getattr(b, "id", None), input=getattr(b, "input", None))
            elif bname == "ToolResultBlock":
                _emit("tool_result", id=getattr(b, "tool_use_id", None),
                      is_error=bool(getattr(b, "is_error", False)),
                      content=getattr(b, "content", None))
            else:
                _emit("block", block=bname, repr=str(b))
        return

    if name == "ResultMessage":
        _emit("result", subtype=getattr(m, "subtype", None),
              is_error=bool(getattr(m, "is_error", False)),
              turns=getattr(m, "num_turns", None),
              duration_ms=getattr(m, "duration_ms", None),
              cost_usd=getattr(m, "total_cost_usd", None),
              session_id=getattr(m, "session_id", None),
              stop_reason=getattr(m, "stop_reason", None),
              usage=getattr(m, "usage", None),
              # A DENIED tool call is a silent failure: the model asked for something, was
              # refused by a hook or permission rule, and carried on as if it had simply
              # chosen not to. Nothing else in the pipeline records it.
              permission_denials=getattr(m, "permission_denials", None) or None,
              api_error_status=getattr(m, "api_error_status", None),
              errors=getattr(m, "errors", None) or None)
        return

    if name == "SystemMessage":
        data = getattr(m, "data", {}) or {}
        sub = getattr(m, "subtype", None)
        # api_retry storms are the signature of a bad key (ten 401s with backoff, ~190s
        # per call) and of rate limiting. Both look like a hang from outside.
        if sub in ("init", "api_retry", "error"):
            _emit("system", subtype=sub,
                  session_id=data.get("session_id"), model=data.get("model"),
                  error=data.get("error"), error_status=data.get("error_status"),
                  attempt=data.get("attempt"))
        return


def prompt(text: str, *, role: str = "user", **fields) -> None:
    """The INPUT. Recorded separately because the SDK never echoes it back: a transcript
    of replies to an unrecorded question cannot be audited, and the system prompt and
    layer contract are exactly what changes between runs we are trying to compare."""
    _emit("prompt", role=role, text=text, **fields)


def read(p: str | Path) -> list[dict]:
    """Parse a transcript. Tolerates a truncated final line — a killed process leaves
    one, and refusing to read the file because of it would throw away the record of the
    very thing that killed it."""
    out = []
    for i, line in enumerate(Path(p).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"kind": "unparseable", "seq": None, "line_no": i,
                        "raw": line[:200]})
    return out


def find(shot_folder: str | Path, stage: str | None = None,
         run_id: str | None = None) -> list[Path]:
    folder = Path(shot_folder) / "logs" / "transcript"
    if not folder.is_dir():
        return []
    hits = sorted(folder.glob("*.jsonl"))
    if stage:
        hits = [p for p in hits if p.name.startswith(stage)]
    if run_id:
        hits = [p for p in hits if p.stem.endswith(run_id)]
    return hits
