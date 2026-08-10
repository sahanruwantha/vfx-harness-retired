"""Screen-time estimation — how long a beat's narration runs on screen.

Slimmed for bambi-vfx: the documentary back-half's lock also held the footage-acquire and storyboard
leaves; the 3D pipeline only needs :func:`estimate_seconds` (the realizer uses it to scale how much
to build to a beat's screen time).
"""

from __future__ import annotations

WORDS_PER_SECOND = 2.5  # voice-over pace, for the running clock
MIN_BEAT_SECONDS = 10


def estimate_seconds(vo: str) -> int:
    """Rough on-screen duration of a beat's narration, floored so a beat is never trivially short."""
    words = len(vo.split())
    return max(MIN_BEAT_SECONDS, round(words / WORDS_PER_SECOND))
