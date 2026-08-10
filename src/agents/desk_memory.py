"""Desk memory — what the harness carries between desk sessions so a fresh artist starts informed.

The .blend handoff carries the SCENE (what exists); this carries the KNOWLEDGE (why). A fresh
department desk otherwise walks in cold and re-derives — and can drift from the intent an earlier
stage set. Two scopes, both threaded into the desk's opening message and appended to afterwards:

* :class:`ShotContext` (per shot) — INTENT + DECISIONS + SCENE FACTS + OPEN ISSUES, the turnover
  notes that travel between rounds and departments. Curated (capped, saved to ``context.md``).
* :class:`LessonBook` (durable, cross-shot) — Blender 5.x gotchas the desk keeps rediscovering.
  Seeded with the known ones, harvested from ``LESSON:`` lines the desk emits, persisted, and
  re-injected so nobody wastes a pass rediscovering them.

Both are plain, testable data objects; the orchestrators own them and thread them through
``build_scene(context=…, lessons=…)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_MAX_DECISIONS = 15
_MAX_FACTS = 10
_MAX_ENTRY = 320  # a desk note can ramble; keep each context entry tight

_LESSON_RE = re.compile(r"(?im)^[^\n]*?\bLESSON:\s*(.+?)\s*$")


def default_lessons_path() -> Path:
    """The durable lessons file, under $HOME (persists across runs, like the bridge cache)."""
    return Path.home() / ".cache" / "bambi" / "blender5_lessons.md"


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _MAX_ENTRY else text[:_MAX_ENTRY] + "…"


# ======================================================================================
# Per-shot context
# ======================================================================================


@dataclass
class ShotContext:
    """The accumulating turnover notes for one shot, threaded into every desk session."""

    path: Path | None = None
    intent: str = ""
    decisions: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    open_issues: str = ""

    def set_intent(self, text: str) -> None:
        self.intent = _clip(text)
        self.save()

    def add_decision(self, stage: str, text: str) -> None:
        if text and text.strip():
            self.decisions.append(f"[{stage}] {_clip(text)}")
            self.decisions = self.decisions[-_MAX_DECISIONS:]
            self.save()

    def add_fact(self, stage: str, text: str) -> None:
        if text and text.strip():
            self.facts.append(f"[{stage}] {_clip(text)}")
            self.facts = self.facts[-_MAX_FACTS:]
            self.save()

    def set_open_issues(self, text: str) -> None:
        self.open_issues = _clip(text or "")
        self.save()

    def render(self) -> str:
        """The context block to inject — omits empty sections; empty overall → ''."""
        if not (self.intent or self.decisions or self.facts or self.open_issues):
            return ""
        parts = ["PROJECT CONTEXT (read before you build; do not contradict earlier decisions):"]
        if self.intent:
            parts.append(f"Intent: {self.intent}")
        if self.decisions:
            parts.append("Decisions so far:\n" + "\n".join(f"- {d}" for d in self.decisions))
        if self.facts:
            parts.append("Scene facts (names/values already in the scene):\n" + "\n".join(f"- {f}" for f in self.facts))
        if self.open_issues:
            parts.append(f"Open issues to address: {self.open_issues}")
        return "\n\n".join(parts)

    def save(self) -> None:
        if self.path is not None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.path).write_text("# shot context\n\n" + (self.render() or "(empty)") + "\n")


# ======================================================================================
# Durable cross-shot lessons
# ======================================================================================

SEED_LESSONS: tuple[str, ...] = (
    "EEVEE's engine id is 'BLENDER_EEVEE' (there is no '_NEXT').",
    "The compositor is a node group in 5.x (scene.compositing_node_group); a mis-wired one renders "
    "the frame BLACK — get glow from EMISSIVE materials, not a Glare/compositor node.",
    "Action.fcurves is gone (slotted actions); animate with obj.keyframe_insert(data_path, frame=f).",
    "scene.eevee has no 'use_bloom' in 5.x — bloom is not a scene toggle.",
    "Principled BSDF emission inputs are named 'Emission Color' and 'Emission Strength'.",
    "render.image_settings.file_format is gated by image_settings.media_type in 5.x — set "
    "media_type='MULTI_LAYER' BEFORE assigning 'OPEN_EXR_MULTILAYER' (='VIDEO' for FFMPEG); bl_rna "
    "enum_items lists the static superset, so check by trying to assign, not by reading the enum.",
    "Cycles quality is scene.cycles.samples + scene.cycles.use_denoising (NOT eevee.taa_render_samples).",
    # --- FX / simulation ---
    "A believable volumetric (storm/cloud/smoke/haze) for a STILL is a DOMAIN mesh (a cube) with a "
    "'Principled Volume' shader whose Density is driven by a Noise/Musgrave texture — NOT a sculpted "
    "lumpy mesh (that gives the tarry-blob look). Shape it via the texture's Scale/Detail and a "
    "ColorRamp/Map Range on density, not by deforming geometry.",
    "Blender 5.x Geometry Nodes simulation is a zone: a Simulation Input node wired to a Simulation "
    "Output node; state accumulates per frame. For a single still you usually do NOT bake — just "
    "scene.frame_set(f) to a representative moment after building the setup.",
    "Fluid/smoke sims (a modifier on a DOMAIN object) need bpy.ops.fluid.bake_all() and are slow and "
    "fragile headless; for one still frame prefer a volume-shader domain over a baked fluid sim.",
    "Volumes need light to read: give the effect some world/ambient or a light so its density and "
    "falloff are visible; Cycles resolves Principled Volume density/shadows more truthfully than EEVEE.",
)


def harvest_lessons(text: str) -> list[str]:
    """Pull ``LESSON: …`` lines out of a desk reply (tolerates a leading '-'/'*'/'**')."""
    out: list[str] = []
    for m in _LESSON_RE.findall(text or ""):
        lesson = m.strip().strip("*").strip()
        if lesson:
            out.append(lesson)
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


@dataclass
class LessonBook:
    """Seeded + harvested Blender gotchas. Seed lives in code (always present); harvested ones
    persist to ``path`` and accumulate across runs."""

    path: Path | None = None
    harvested: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.path is not None and Path(self.path).exists():
            for line in Path(self.path).read_text().splitlines():
                line = line.strip()
                if line.startswith("- "):
                    self.harvested.append(line[2:].strip())

    def all(self) -> list[str]:
        """Seed + harvested, de-duplicated (seed wins)."""
        seen = {_norm(s) for s in SEED_LESSONS}
        out = list(SEED_LESSONS)
        for h in self.harvested:
            if _norm(h) not in seen:
                seen.add(_norm(h))
                out.append(h)
        return out

    def add(self, lesson: str) -> bool:
        """Add a harvested lesson if novel. Novelty is substring-aware: a lesson that is contained in
        (or contains) a known one — e.g. a seed with extra parenthetical detail — is treated as a
        duplicate. Returns True if added."""
        lesson = lesson.strip()
        new = _norm(lesson)
        if not new:
            return False
        for known in (_norm(x) for x in (*SEED_LESSONS, *self.harvested)):
            if new == known or new in known or known in new:
                return False
        self.harvested.append(lesson)
        self.save()
        return True

    def harvest_and_add(self, text: str) -> list[str]:
        """Harvest LESSON: lines from *text* and add the novel ones. Returns the newly added."""
        added = [l for l in harvest_lessons(text) if self.add(l)]
        return added

    def render(self) -> str:
        return "LESSONS — known Blender 5.x gotchas (do not rediscover these):\n" + "\n".join(
            f"- {l}" for l in self.all()
        )

    def save(self) -> None:
        if self.path is not None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.path).write_text(
                "# Blender 5.x lessons (harvested across runs)\n\n" + "\n".join(f"- {l}" for l in self.harvested) + "\n"
            )
