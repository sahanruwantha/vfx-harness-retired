"""Leaf type aliases — the contract a realizer/critic satisfies.

Slimmed for bambi-vfx: the full documentary back-half defined a scheduler and a dozen model-driven
leaves here. The 3D pipeline only needs the *type* of a leaf — a ``BeatEntry -> Clip`` renderer, an
image-plate generator, a footage critic — so it can be injected and composed. These aliases are that
contract; the implementations live in :mod:`scene` (e.g. :func:`scene.shot_realizer.make_pipeline_realizer`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from develop.ledger import BeatEntry, Clip, Coverage, Intent, Mode, Realization, Storyboard
from develop.verdict import Verdict

ProbeCoverage = Callable[[Intent], Awaitable[Coverage]]
DesignGrammar = Callable[[list[BeatEntry]], Awaitable[tuple[str, dict[str, Mode]]]]
ReviseScript = Callable[[BeatEntry, Realization | None, Intent | None], Awaitable[Realization]]
CritiqueScript = Callable[[BeatEntry, str], Awaitable[list[Verdict]]]
CritiqueDesign = Callable[[list[BeatEntry], str], Awaitable[list[Verdict]]]
AcquireFootage = Callable[[BeatEntry], Awaitable[Clip]]
Render3D = Callable[[BeatEntry], Awaitable[Clip]]  # the 3D leaf — builds and renders a Clip
GeneratePlate = Callable[[BeatEntry], Awaitable[Clip]]  # generates an AI image plate
CritiqueFootage = Callable[[BeatEntry], Awaitable[list[Verdict]]]
BuildStoryboard = Callable[[BeatEntry, str, Storyboard | None], Awaitable[Storyboard]]
