"""Write the per-shot CLAUDE.md that survives compaction.

Everything task-specific currently arrives in the KICKOFF message — the oldest message in
the conversation and the first thing compaction summarises away. Gate G ran 121 turns
with 28KB of run_bpy payloads and megabytes of render output; it compacted, and its
instructions were in the message least likely to survive.

The SDK docs are explicit about the remedy: "Persistent rules belong in CLAUDE.md (loaded
via settingSources) rather than in the initial prompt, because CLAUDE.md content is
re-injected on every request." The compactor also reads CLAUDE.md, so a summary-
instructions section steers what it keeps.

This writes a short, durable file per gate run: the gate's contract (owned axes, every
frame it answers for, measured targets) plus the rules that must never be summarised out.
"""

from __future__ import annotations

from pathlib import Path

from .brief import Shot
from .escalate import answers_block, open_block

_HEADER = "<!-- generated per gate run by pipeline.shot_context — safe to overwrite -->"


def write_gate_context(shot: Shot, gate, axes: list[tuple[str, str]],
                       fingerprints: dict[int, str] | None = None) -> Path:
    """Write shots/<id>/CLAUDE.md for this gate. Returns the path."""
    fingerprints = fingerprints or {}
    owned = list(gate.owns) or ["(not declared — judge on the gate's scope)"]
    judge_rows = "\n".join(
        f"- **f{f}** vs `{r}`" + (f" — target: {fingerprints[f]}" if f in fingerprints else "")
        for f, r in gate.judges)
    axis_rows = "\n".join(f"- `{k}` — {d}" for k, d in axes if k in gate.owns) or \
                "\n".join(f"- `{k}` — {d}" for k, d in axes)

    sup = "\n\n".join(x for x in (answers_block(shot.folder), open_block(shot.folder)) if x)
    supervisor = (sup + "\n\n") if sup else ""
    body = f"""{_HEADER}
# Gate {gate.id} — {gate.title}

You are building ONE gate of shot `{shot.id}` ({shot.frames}f @ {shot.fps}fps).
This file is re-injected on every request: if the conversation is summarised, THESE
facts remain true and authoritative.

## What this gate must deliver
{gate.reads}

Delta script: `{gate.script}` — reproduce only THIS gate's changes; earlier gate scripts
run before yours and their objects already exist.

## Frames you answer for
The finished script is rendered and scored at EVERY frame below and passes only if all
of them clear. A change that fixes one and breaks another is not a fix.

{judge_rows}

## Axes you are scored on
Only these. Every other axis is marked "n/a" — including elements that are correctly
ABSENT at your frames because another gate adds or removes them.

{axis_rows}

## Rules that do not change
- Use the `bvfx_*` helpers over raw bpy where one exists. Hand-rolled equivalents use
  Blender-4 APIs that fail on 5.2, and a PreToolUse guardrail will block the known ones.
- `find_recipe` before inventing a technique; the cookbook has verified, measured code.
- Build LIVE in the session and verify with renders. Write the delta script at the end
  from your run_bpy transcript, not from memory.
- To change your build script: script_map -> find_in_script -> Read that span -> Edit.
  Write only creates it the first time; never rewrite a whole script for one value.
- Objective metrics beat opinion. If a render's measured gap to the reference is reported
  to you, treat it as fact and fix the number.

{supervisor}## Summary instructions
When summarising this conversation, ALWAYS preserve:
- the gate id, its owned axes, and every judge frame listed above
- object and material NAMES created so far (later gates reference them by name)
- measured values already converged on, and values already ruled out with their measurement
- which of the judge frames currently pass and which do not
"""
    path = shot.folder / "CLAUDE.md"
    path.write_text(body, encoding="utf-8")
    return path


def clear_gate_context(shot: Shot) -> None:
    """Remove a generated CLAUDE.md (never delete a hand-written one)."""
    p = shot.folder / "CLAUDE.md"
    if p.is_file() and p.read_text(encoding="utf-8").startswith(_HEADER):
        p.unlink()
