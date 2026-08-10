"""The one definition of what this channel can actually produce — the faceless constraint.

Every stage that reasons about *how the film is made* (the treatment's voice and signature
move, the visual grammar, the per-beat mode assignment, the storyboard) must build within the
same production model, or the pipeline proposes things it cannot make — e.g. a treatment that
calls for on-camera expert talking heads a faceless channel cannot shoot. Defining it once here
keeps every stage honest to the same limits.
"""

from __future__ import annotations

# A compact one-line statement of the same constraint, for prompts that only need to
# name it (e.g. the footage sourcer/critic openings) rather than the full mode rules.
FACELESS_ONELINE = (
    "a FACELESS documentary — made only of motion graphics, 3D, and archival/stock "
    "footage, with no host, no talking-head, and no filmed actor"
)

FACELESS_PRODUCTION = (
    "This is a FACELESS channel: no host, no talking-head, no on-camera narrator, no filmed "
    "expert or interview, and no live-action reenactment with actors. The ONLY producible visual "
    "modes are: "
    "(1) MOTION GRAPHICS — 2D animation, kinetic typography, infographics, animated diagrams "
    "and data-viz, animated documents, maps, and charts, AND any conceptual, symbolic, or "
    "metaphorical space that illustrates an idea rather than depicting a real place (a 'vault' of "
    "data, a rendered 'stack' of laws, an abstract environment) — these are motion graphics, not 3D; "
    "(2) 3D — used ONLY to reconstruct a SPECIFIC, REAL physical scene, place, object, or moment the "
    "story actually depicts as a concrete event, of the kind a documentary would otherwise show with "
    "archival footage or a live reenactment (the actual room a raid happened in, a real vehicle, a "
    "specific building, a device). If the place did not physically exist as shown — if it is a "
    "metaphor, a symbol, or a generic stand-in for a process or an idea — it is NOT 3D; it is motion "
    "graphics. 3D is the reconstructive mode of an investigation/heist, not decoration for an "
    "explainer; "
    "(3) ARCHIVAL / STOCK — real news and TV clips, stock b-roll, document and photo scans, "
    "and screen / web captures. "
    "Prescribe ONLY these. Never specify a host, an interview, an on-camera expert, a filmed "
    "actor, or an original location shoot. An expert's point may appear only as a cited quote "
    "rendered on screen or read in voice-over, never as a person filmed to camera."
)
