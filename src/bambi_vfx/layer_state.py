"""The conclusions of a layer, kept outside the conversation.

A long layer accumulates every tool result verbatim — render metrics superseded three
edits ago, full scene listings, node dumps — and the builder re-reads that growing pile
each turn. Layer 1 of one run made ~10 compare_frame calls and dozens of run_bpy calls in
a single session.

The Python SDK (0.2.136) exposes no context-editing API, so stale tool results cannot be
cleared programmatically from here. What CAN be done is make the layer's conclusions
survive independently of the transcript: the current measured state per judge frame, what
has been tried, and what has been ruled out with the measurement that ruled it out. Then
a compaction — or a resume after a crash — costs the transcript, not the knowledge.

Written after every round, and re-injected into the layer contract that CLAUDE.md carries.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

NAME = "layer_state.json"
_MAX_TRIED = 24          # keep the file small; it is re-read on every request


def path_for(shot_folder: str | Path) -> Path:
    return Path(shot_folder) / "logs" / NAME


def load(shot_folder: str | Path) -> dict:
    p = path_for(shot_folder)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"! {NAME} unreadable, starting fresh ({e})", flush=True)
        return {}


def start(shot_folder: str | Path, layer_id: str, judges: list) -> dict:
    """Begin an attempt. Clears the measured scores, KEEPS what was ruled out.

    A rebuild starts from an empty scene, so last attempt's scores describe geometry that
    no longer exists and must go. But "this approach measurably made it worse" stays true
    across attempts — dropping it is how a repaired layer re-tries the very thing that
    failed. Carried over only within the same layer.
    """
    prev = load(shot_folder)
    carry = prev if str(prev.get("layer")) == str(layer_id) else {}
    st = {"layer": layer_id,
          "frames": {str(f): {"ref": r} for f, r in judges},
          "tried": list(carry.get("tried") or [])[-_MAX_TRIED:],
          "ruled_out": list(carry.get("ruled_out") or [])[-_MAX_TRIED:],
          "attempts": int(carry.get("attempts", 0)) + 1,
          "updated": _now()}
    _write(shot_folder, st)
    return st


def record_round(shot_folder: str | Path, *, frame: int, mean: float, passed: bool,
                 scores: dict | None = None, issues: list | None = None,
                 approach: str | None = None) -> dict:
    """What this round achieved at this frame — the durable version of the transcript."""
    st = load(shot_folder) or {"frames": {}, "tried": [], "ruled_out": []}
    slot = st.setdefault("frames", {}).setdefault(str(frame), {})
    prev = slot.get("mean")
    slot.update(mean=mean, passed=passed, scores=scores or {},
                open_issues=(issues or [])[:4])
    if prev is not None and prev != mean:
        slot["direction"] = "improved" if mean > prev else "regressed"
        slot["was"] = prev
    if approach:
        entry = {"approach": approach, "frame": frame, "mean": mean}
        st.setdefault("tried", []).append(entry)
        st["tried"] = st["tried"][-_MAX_TRIED:]
        # An approach that made things WORSE is the single most useful thing to carry
        # forward — it is exactly what a fresh context would otherwise try again.
        if prev is not None and mean < prev:
            st.setdefault("ruled_out", []).append(
                f"{approach} → {prev} dropped to {mean} at f{frame}")
            st["ruled_out"] = st["ruled_out"][-_MAX_TRIED:]
    st["updated"] = _now()
    _write(shot_folder, st)
    return st


def as_prompt_block(shot_folder: str | Path) -> str:
    """Compact text for the layer contract. Empty when there is nothing to say yet."""
    st = load(shot_folder)
    frames = st.get("frames") or {}
    scored = {f: d for f, d in frames.items() if d.get("mean") is not None}
    if not scored and not st.get("ruled_out"):
        return ""
    lines = ["## Where this layer already stands (survives compaction)"]
    for f, d in sorted(scored.items(), key=lambda kv: int(kv[0])):
        arrow = f" ({d['was']} → {d['mean']}, {d['direction']})" if d.get("direction") else ""
        lines.append(f"- f{f}: {d['mean']} {'PASS' if d.get('passed') else 'not yet'}{arrow}")
        for i in (d.get("open_issues") or [])[:2]:
            lines.append(f"    still open: {i}")
    if st.get("ruled_out"):
        lines.append("- ALREADY RULED OUT — do not retry these:")
        for r in st["ruled_out"][-6:]:
            lines.append(f"    {r}")
    return "\n".join(lines) + "\n"


def _write(shot_folder: str | Path, st: dict) -> None:
    p = path_for(shot_folder)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
