"""The shot contracts — the data types the 3D pipeline builds against.

Slimmed for bambi-vfx: the documentary back-half modelled a beat as a stack of freezable *layers*
(coverage, mode, storyboard, a scheduler that reopens them). The 3D pipeline needs none of that — it
needs a beat's INTENT (what to build), its optional REALIZATION (the voiceover it illustrates), and the
CLIP it produces. So this is just those plain records plus the ``Layer`` tag a ``Verdict`` carries and
the ``FrameSample`` a ``Clip``/critic passes around.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Literal

LicenceState = Literal["KNOWN", "UNKNOWN"]


class Layer(IntEnum):
    """The layer a verdict attributes a result to — a small tag on :class:`~contracts.verdict.Verdict`.
    The 3D pipeline judges the rendered CLIP; the other members remain for verdict compatibility."""

    INTENT = 0
    COVERAGE = 1
    MODE = 2
    REALIZATION = 3
    CLIP = 4
    STORYBOARD = 5


@dataclass(frozen=True)
class FrameSample:
    """One sampled frame — the unit the sighted critics judge (a render, or a reference image)."""

    timecode: str  # mm:ss
    seconds: float
    jpeg_b64: str  # base64-encoded JPEG, for an image content block


@dataclass(frozen=True)
class Intent:
    """What a beat must accomplish — the anchor the whole shot is built to."""

    function: str  # e.g. "cold open", "mechanism", "turn"
    subject: str  # what the beat is about — the build anchor
    evidence: str  # factual constraints, verbatim
    heading: str  # the full heading, for display
    index: int  # position, 0-based


@dataclass(frozen=True)
class Realization:
    """The beat's voice-over — what the shot illustrates on screen."""

    vo: str
    citations: tuple[str, ...] = ()
    verify_tags: tuple[str, ...] = ()
    handoff_ok: bool = True


@dataclass(frozen=True)
class Clip:
    """A produced visual for a beat: a 3D render (or a named acquisition gap when nothing built).
    The sighted critic judges ``frames``; ``blend_path``/``render_meta`` carry the 3D artifacts."""

    fetched_path: Path | None = None
    in_out: tuple[float, float] | None = None
    licence: LicenceState = "UNKNOWN"
    frames: tuple[FrameSample, ...] = ()
    acquisition_gap: str | None = None
    blend_path: Path | None = None  # the .blend behind the render, for re-open/refine + final render
    render_meta: Mapping[str, str] = field(default_factory=dict)  # engine, camera, asset provenance


@dataclass
class BeatEntry:
    """One shot to build: its frozen intent, the voiceover it illustrates, and the clip it produces.
    ``prev_id``/``next_id`` give a realizer cut-from/cut-to continuity."""

    id: str
    intent: Intent
    prev_id: str | None = None
    next_id: str | None = None
    chapter: str | None = None

    realization: Realization | None = None
    clip: Clip | None = None
