"""Which layers must reach a light before their image judgments can be paid.

A layer declaring `image` evidence is not thereby a lit layer. Silhouette, extent,
occlusion and framing are image evidence judged perfectly well in Workbench solid, which
shades from the viewport's own lighting model and needs no lamp and no world. EEVEE
renders that same scene black.

So the question a plan must answer before it is paid for is not "does this layer judge
pixels" but "does it judge them in a medium that needs the scene lit" -- and that is a
declaration, because at sparse publication there are no units to derive it from and
inferring it from layer titles or axis names is the role-name heuristic HIR-0098 retired
(ADR-0011).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from vfx_harness.domain.judgment_debt_models import (
    LIT_OBSERVATION_MEDIA,
    OBSERVATION_MEDIA,
    medium_requires_illumination,
)

#: The layer field carrying that declaration.
IMAGE_MEDIA_FIELD = "image_observation_media"

LIT_JUDGMENT_RULE = (
    "a layer declaring the image evidence domain must declare "
    f"{IMAGE_MEDIA_FIELD} as a non-empty subset of {sorted(OBSERVATION_MEDIA)}, and a "
    f"layer naming any of {sorted(LIT_OBSERVATION_MEDIA)} must reach an illumination "
    "provider in its dependency closure or be one. Workbench solid needs no light and is "
    "never blocked by this rule"
)


def parse_image_media(value: Any, where: str) -> frozenset[str]:
    """The declared media, or a typed refusal naming the accepted set."""
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise ValueError(
            f"{where} must be a non-empty list drawn from {sorted(OBSERVATION_MEDIA)}"
        )
    media = {str(item) for item in value}
    unknown = sorted(media - OBSERVATION_MEDIA)
    if unknown:
        raise ValueError(
            f"{where} names unknown observation media: {', '.join(unknown)}; "
            f"accepted: {sorted(OBSERVATION_MEDIA)}"
        )
    return frozenset(media)


def requires_illumination(media: Sequence[str] | frozenset[str]) -> bool:
    """Whether any declared medium needs the scene lit."""
    return any(medium_requires_illumination(str(medium)) for medium in media)


def unreachable_illumination(
    *,
    layer_id: str,
    declared_media: Sequence[str] | frozenset[str],
    capability_closure: Mapping[str, set[str]] | Mapping[str, frozenset[str]],
) -> str | None:
    """The refusal for a lit layer with no reachable illumination, or None.

    Returns a sentence rather than a bool because the caller has no better information
    about which media forced the requirement, and a refusal that names the layer and the
    medium is the difference between an actionable plan edit and a rerun.
    """
    lit = sorted(
        str(medium)
        for medium in declared_media
        if medium_requires_illumination(str(medium))
    )
    if not lit:
        return None
    if "illumination" in set(capability_closure.get(str(layer_id), set()) or set()):
        return None
    return (
        f"declares {IMAGE_MEDIA_FIELD} {lit} but no illumination capability is reachable; "
        'declare provides: {"illumination": ["<reserved role>"]} on this layer or depend '
        "on an earlier layer whose capability closure provides it. A judgment in "
        f"{lit[0]!r} on an unlit scene renders black and cannot be paid in either "
        "direction. Declare only [\"workbench_solid\"] if this layer judges form rather "
        "than appearance"
    )


__all__ = [
    "IMAGE_MEDIA_FIELD",
    "LIT_JUDGMENT_RULE",
    "parse_image_media",
    "requires_illumination",
    "unreachable_illumination",
]
