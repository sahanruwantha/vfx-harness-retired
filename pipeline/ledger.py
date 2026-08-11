"""shot.json — the milestone ledger.

No git history: a deterministic `build/<milestone>.py` per milestone is the artifact,
and this file is the *state* — which milestones passed, the critic verdict that
gated them, and the script + render that produced each. Downstream stages and
resumed sessions read the ledger to know what's done.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .brief import Shot


@dataclass(frozen=True)
class Milestone:
    """One non-negotiable state-change frame the critic gates on (from brief.md)."""

    id: str
    frame: int
    ref: str  # path relative to the shot folder, e.g. "refs/M1_green.jpg"
    reads: str  # the state that MUST read at this frame


# Fallback critic axes. Per shot, the real axes are DERIVED from the brief + refs and
# cached at shot/critic_axes.json (see build_agent.ensure_axes) — that's what makes the
# critic generalise to any scene/style. These defaults are only used if none exist.
DEFAULT_AXES: list[tuple[str, str]] = [
    ("composition", "Framing, silhouette, camera angle and subject placement match the ref."),
    ("atmosphere", "Volumetric depth / haze / lighting mood match the ref — not a flat CG void."),
    ("subject_detail", "The hero subject reads with the ref's level of form and detail."),
    ("environment", "The surrounding environment/ground reads as in the ref."),
    ("palette", "Colours, contrast and tone match the ref."),
    ("finish", "Post/grade/bloom/motion cues match the ref's finish."),
]


def load_axes(shot: Shot) -> list[tuple[str, str]]:
    """The critic rubric for this shot: shot/critic_axes.json if present, else defaults.
    Stored as a list of {"key","desc"} objects."""
    path = shot.folder / "critic_axes.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            axes = [(a["key"], a["desc"]) for a in data if a.get("key") and a.get("desc")]
            if axes:
                return axes
        except Exception:
            pass
    return DEFAULT_AXES

def load_milestones(shot: Shot) -> dict[str, Milestone]:
    """Per-shot milestones from shots/<id>/milestones.json — written by the PLAN stage
    (the brief gives approval moments; mapping them to frames is the plan's job).
    Format: [{"id","frame","ref","reads"}, …] in shot order."""
    path = shot.folder / "milestones.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} missing — run the plan agent first (it maps the brief's approval "
            f"moments to frames and writes milestones.json)")
    data = json.loads(path.read_text())
    out: dict[str, Milestone] = {}
    for m in data:
        out[m["id"]] = Milestone(m["id"], int(m["frame"]), m["ref"], m.get("reads", ""))
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    """Read/modify/write `shot.json` for one shot."""

    def __init__(self, shot: Shot):
        self.shot = shot
        self.path = shot.folder / "shot.json"
        if self.path.is_file():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"shot": shot.id, "milestones": {}}

    # -- accessors -----------------------------------------------------------
    def _slot(self, m: Milestone) -> dict:
        return self.data.setdefault("milestones", {}).setdefault(m.id, {})

    def status(self, m: Milestone) -> str:
        return self._slot(m).get("status", "pending")

    # -- mutations -----------------------------------------------------------
    def begin(self, m: Milestone) -> None:
        slot = self._slot(m)
        slot.update(frame=m.frame, ref=m.ref, status="in_progress",
                    script=f"build/{m.id.lower()}.py", rounds=slot.get("rounds", []))
        self.save()

    def record_round(self, m: Milestone, *, kind: str, index: int,
                     render: str, verdict: dict) -> None:
        """Append one critic round (kind='iter' during the loop, 'canonical' for the
        deterministic re-run of the build script)."""
        self._slot(m).setdefault("rounds", []).append({
            "round": index,
            "kind": kind,
            "render": render,
            "scores": verdict.get("scores", {}),
            "mean": verdict.get("mean"),
            "pass": verdict.get("pass", False),
            "round_s": verdict.get("round_s"),  # wall-time telemetry (for eval)
            "issues": verdict.get("issues", []),
            "at": _now(),
        })
        self.save()

    def mark(self, m: Milestone, status: str, best: dict | None = None) -> None:
        slot = self._slot(m)
        slot["status"] = status
        slot["updated"] = _now()
        if best is not None:
            slot["best"] = {"round": best.get("round"), "mean": best.get("mean"),
                            "render": best.get("render")}
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
