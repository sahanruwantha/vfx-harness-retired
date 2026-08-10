"""The beat ledger — the shared blackboard the back-half scheduler resolves.

The back half is constraint satisfaction, not a pipeline: ``script`` and ``footage``
depend on each other. The cycle is broken by splitting each beat into *layers* of
different stability. Intent (what the beat must do) is frozen up front by the front-half
pipeline; realization (wording, mode, clip) relaxes toward what is actually acquirable.

Every field belongs to exactly one :class:`Layer`. Layers freeze in ladder order
(``INTENT → COVERAGE → MODE → REALIZATION → CLIP → STORYBOARD``); a critic may *reopen* a
layer, which invalidates it and everything downstream. Reopens only ever move upstream and
are budgeted, so the loop terminates by construction. See ``docs/back-half-architecture.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Literal

from footage.candidate import FootageCandidate
from footage.inspect import FrameSample


class Layer(IntEnum):
    """The stability layers of a beat, in freeze (malleability) order.

    Lower = more upstream = frozen earlier and reopened last. ``INTENT`` is frozen on entry
    and never reopens; it is the anchor that breaks the script/footage cycle.
    """

    INTENT = 0
    COVERAGE = 1
    MODE = 2
    REALIZATION = 3
    CLIP = 4
    STORYBOARD = 5


# The mutable layers, in ladder order, and the entry attribute each one owns.
_LAYER_ATTR: dict[Layer, str] = {
    Layer.COVERAGE: "coverage",
    Layer.MODE: "mode",
    Layer.REALIZATION: "realization",
    Layer.CLIP: "clip",
    Layer.STORYBOARD: "storyboard",
}

CoverageVerdict = Literal["STRONG_ARCHIVAL", "WEAK", "NONE"]
VisualMode = Literal["ARCHIVAL", "MOTION_GRAPHICS", "3D"]
LicenceState = Literal["KNOWN", "UNKNOWN"]


@dataclass(frozen=True)
class Intent:
    """What a beat must accomplish. Produced by the spine; frozen for the whole back half."""

    function: str  # e.g. "cold open", "mechanism", "turn"
    subject: str  # the search anchor — what the beat is about
    evidence: str  # dossier finding(s) that anchor it, verbatim from the spine
    heading: str  # the full spine heading, for display
    index: int  # position in the spine, 0-based


@dataclass(frozen=True)
class Coverage:
    """What archival is actually acquirable for this beat's intent (probe, no fetch)."""

    verdict: CoverageVerdict
    candidates: tuple[FootageCandidate, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class Mode:
    """The visual mode this beat is realized in, decided from coverage + grammar."""

    assigned: VisualMode
    rationale: str = ""


@dataclass(frozen=True)
class Realization:
    """The beat's actual voice-over, drafted to the available assets."""

    vo: str
    citations: tuple[str, ...] = ()
    verify_tags: tuple[str, ...] = ()
    handoff_ok: bool = True


@dataclass(frozen=True)
class Clip:
    """A produced visual for a beat: a fetched archival clip, or a 3D render, or a named
    acquisition gap when nothing could be produced. The sighted critic judges ``frames`` either
    way, so the layer is mode-agnostic; ``blend_path``/``render_meta`` carry the 3D-only artifacts.
    """

    fetched_path: Path | None = None
    in_out: tuple[float, float] | None = None
    licence: LicenceState = "UNKNOWN"
    frames: tuple[FrameSample, ...] = ()
    acquisition_gap: str | None = None
    # 3D-only, additive (defaults preserve every footage/MG caller):
    blend_path: Path | None = None  # the .blend behind the render, for re-open/refine + final render
    render_meta: Mapping[str, str] = field(default_factory=dict)  # engine, camera, asset provenance


@dataclass(frozen=True)
class Storyboard:
    """The committed micro-beat rows for this beat's slice of the film."""

    rows: str  # the rendered ``Time | Visual action | Narration | Production gap`` table


@dataclass
class BeatEntry:
    """One beat on the blackboard: its frozen intent plus whatever has been resolved.

    ``frozen_through`` is the highest contiguously-frozen layer; layers above it are still
    fluid. Payloads for unfrozen mutable layers may be present (a pass produced them) but
    are not yet committed. ``reopen_counts`` enforces the per-layer reopen budget.
    """

    id: str
    intent: Intent
    prev_id: str | None = None
    next_id: str | None = None
    chapter: str | None = None

    coverage: Coverage | None = None
    mode: Mode | None = None
    realization: Realization | None = None
    clip: Clip | None = None
    storyboard: Storyboard | None = None

    frozen_through: Layer = Layer.INTENT
    reopen_counts: dict[Layer, int] = field(default_factory=dict)
    # The last critic reason that reopened this beat's realization, fed to the next rewrite so it
    # addresses the specific complaint instead of re-rolling blind. Set on reopen, read by the reviser.
    revision_note: str | None = None

    def payload(self, layer: Layer) -> object | None:
        if layer is Layer.INTENT:
            return self.intent
        return getattr(self, _LAYER_ATTR[layer])

    def set_payload(self, layer: Layer, value: object) -> None:
        if layer is Layer.INTENT:
            raise ValueError("intent is immutable")
        setattr(self, _LAYER_ATTR[layer], value)

    def is_frozen(self, layer: Layer) -> bool:
        return layer <= self.frozen_through

    def frontier(self) -> Layer:
        """The lowest (most upstream) layer not yet frozen — where work is owed next.

        ``STORYBOARD`` frozen means the beat is fully resolved; we report it as its own
        frontier so callers can test ``frontier() == STORYBOARD and is_frozen(STORYBOARD)``.
        """
        return Layer(min(self.frozen_through + 1, Layer.STORYBOARD))

    def is_resolved(self) -> bool:
        return self.frozen_through >= Layer.STORYBOARD

    def freeze_through(self, layer: Layer) -> None:
        """Commit every layer up to *layer*. Requires their payloads to be present."""
        for lyr in _LAYER_ATTR:
            if lyr <= layer and self.payload(lyr) is None:
                raise ValueError(f"cannot freeze {self.id} through {layer.name}: {lyr.name} is empty")
        self.frozen_through = Layer(max(self.frozen_through, layer))

    def reopen(self, layer: Layer) -> None:
        """Invalidate *layer* and everything downstream, and count the reopen.

        ``INTENT`` never reopens. Reopening clears the invalidated payloads and drops
        ``frozen_through`` to the layer just upstream, so the scheduler re-earns them.
        """
        if layer is Layer.INTENT:
            raise ValueError("intent never reopens")
        for lyr, attr in _LAYER_ATTR.items():
            if lyr >= layer:
                setattr(self, attr, None)
        self.frozen_through = Layer(layer - 1)
        self.reopen_counts[layer] = self.reopen_counts.get(layer, 0) + 1

    def reopens(self, layer: Layer) -> int:
        return self.reopen_counts.get(layer, 0)
