"""When two composed groups are looking at the same picture.

A layer finalization runs one group per judgment debt, plus one per medium no debt
covers. Each group replays the layer and renders its own plate. Groups that differ only
in *which debt they pay* replay the same scripts and render the same frame in the same
mode, so their plates are byte-identical -- hansa_silk_road produced three identical
Workbench-solid captures in one finalization.

The key here is deliberately narrow: it holds everything that determines the pixels and
nothing that determines the judgment. Which debt a group pays, which claims it evaluates,
and what verdict it reaches are all outside it, because sharing a plate must never share
an obligation. Two groups may look at one picture and owe two separate answers about it.

Anything that could move a pixel belongs in the key. The replay inputs are in it by their
script digests rather than their paths, so a same-named script with changed bytes misses;
the dependencies are in it because a script's effect depends on what ran before it.

**This keys on the inputs, and the obvious refactor to keying on the produced file would
silently never fire.** Measured on hansa_silk_road's attempt-10 finalization: groups 0 and
1 rendered pixel-identical plates at all three judge frames -- diff bbox `None`, max delta
`0` -- and no two of the nine plates shared a SHA-256, because PNG output carries metadata
that differs per write. A cache keyed on the rendered bytes would miss every time and look
like it was working.

`scale` is in the key defensively. Every plate on that shot rendered at 0.5 and nothing
currently varies it, so this field is the one whose omission would cost nothing today and
return the wrong picture the day something does. It costs one digest input; keep it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from vfx_harness.domain.stop_envelope_primitives import canonical_digest

CAPTURE_EQUIVALENCE_SCHEMA = "vfx-harness.capture-equivalence-key/v1"


def _dependency_identity(dependency: Any) -> dict[str, Any]:
    """An `ExecutedReplayDependency`'s identity, read by its real field names.

    Direct attribute access, never `getattr(..., default)`. The first version of this read
    `script_path`/`script_sha256` -- the *input* type's field names -- off a dependency,
    which carries `kind`/`path`/`sha256`. Every dependency collapsed to empty strings, so
    changing a promoted asset's digest produced an identical key and would have served one
    scene's plate for another's. A wrong shape must raise here and become a cache miss, not
    quietly weaken the key.
    """
    return {
        "kind": str(dependency.kind),
        "path": str(dependency.path),
        "sha256": str(dependency.sha256),
    }


def replay_scene_identity(replay_inputs: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """The ordered scripts and dependencies whose execution produced the live scene."""
    identity: list[dict[str, Any]] = []
    for item in replay_inputs:
        identity.append(
            {
                "script_path": str(item.script_path),
                "script_sha256": str(item.script_sha256),
                "dependencies": [
                    _dependency_identity(dependency)
                    for dependency in (item.dependencies or ())
                ],
            }
        )
    return tuple(identity)


def capture_equivalence_key(
    *,
    replay_inputs: Sequence[Any],
    frame: int,
    mode: str,
    scale: float,
) -> str:
    """One digest over everything that decides what the render will contain.

    A miss is always safe -- it renders again. A false hit would hand one group another
    group's picture, so every component is required and none is defaulted.
    """
    if not replay_inputs:
        raise ValueError(
            "capture equivalence needs the executed replay inputs; an empty prefix "
            "cannot identify the scene that was rendered"
        )
    identity = replay_scene_identity(replay_inputs)
    if any(not row["script_sha256"] for row in identity):
        raise ValueError(
            "capture equivalence needs every replay input's script digest; a replay "
            "input with no digest cannot prove the scene is unchanged"
        )
    if any(
        not dependency["sha256"]
        for row in identity
        for dependency in row["dependencies"]
    ):
        raise ValueError(
            "capture equivalence needs every replay dependency's digest; a dependency "
            "read as blank is indistinguishable from one that did not change"
        )
    return canonical_digest(
        {
            "schema": CAPTURE_EQUIVALENCE_SCHEMA,
            "replay_scene": [dict(row) for row in identity],
            "frame": int(frame),
            "mode": str(mode),
            "scale": float(scale),
        }
    )


def capture_matches_key(receipt: Mapping[str, Any], *, frame: int, mode: str, scale: float) -> bool:
    """Whether a stored capture receipt really describes the requested render.

    The key alone says two requests are equivalent. This says the thing actually on disk
    is what that key promised, so a cache hit is checked against the receipt rather than
    trusted because a dictionary lookup succeeded.
    """
    return (
        int(receipt.get("frame", -1)) == int(frame)
        and str(receipt.get("mode") or "") == str(mode)
        and float(receipt.get("scale", -1.0)) == float(scale)
    )


__all__ = [
    "CAPTURE_EQUIVALENCE_SCHEMA",
    "capture_equivalence_key",
    "capture_matches_key",
    "replay_scene_identity",
]
