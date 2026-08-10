"""Leaf type aliases — the contract a realizer/critic satisfies.

The 3D pipeline only needs the *type* of a leaf — a ``BeatEntry -> Clip`` renderer, an image-plate
generator, a footage-style critic — so it can be injected and composed. The implementations live in
:mod:`scene` (e.g. :func:`scene.shot_realizer.make_pipeline_realizer`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from contracts.ledger import BeatEntry, Clip
from contracts.verdict import Verdict

Render3D = Callable[[BeatEntry], Awaitable[Clip]]  # the 3D leaf — builds and renders a Clip
GeneratePlate = Callable[[BeatEntry], Awaitable[Clip]]  # generates an AI image plate
CritiqueFootage = Callable[[BeatEntry], Awaitable[list[Verdict]]]  # judges a produced Clip
