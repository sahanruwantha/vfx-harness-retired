"""The supervised realizer — decide a shot's methodology, then route it to the right pipeline.

:func:`agents.scene_supervisor.decide_methodology` is the supe's call (3D / plate / hybrid + motion);
this wires that call to the realizers that carry it out. It is itself a ``render_3d`` leaf
(``BeatEntry -> Clip``), so develop can use one supervised leaf in place of hardcoding a single
methodology — the thing that would have sent the Silk Road atmospheric shot to a *plate* instead of
grinding the department pipeline to the geometry ceiling.

Realizers are injected, keyed by routing key: ``3d_still``, ``3d_motion``, ``plate``, ``hybrid``.
A missing key falls back down a sensible chain (hybrid → plate → 3d_still; plate → 3d_still;
3d_motion → 3d_still) so a decision is never a dead end. The chosen methodology + rationale are
stamped into the Clip's ``render_meta`` for the trace.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Awaitable, Callable

from agents.scene_supervisor import Methodology, ReferenceImage, decide_methodology
from develop.agents import Render3D
from develop.ledger import BeatEntry, Clip

Decider = Callable[..., Awaitable[Methodology]]
Realizer = Callable[[BeatEntry], "Clip | Awaitable[Clip]"]
RefLoader = Callable[[BeatEntry], list[ReferenceImage]]

# If the chosen key has no realizer, try these in order — never a dead end.
_FALLBACK: dict[str, tuple[str, ...]] = {
    "hybrid": ("hybrid", "plate", "3d_still"),
    "plate": ("plate", "3d_still", "3d_motion"),
    "3d_motion": ("3d_motion", "3d_still"),
    "3d_still": ("3d_still", "3d_motion"),
}


def _resolve(realizers: dict[str, Realizer], key: str) -> tuple[str, Realizer] | None:
    for candidate in _FALLBACK.get(key, (key,)):
        if realizers.get(candidate) is not None:
            return candidate, realizers[candidate]
    return None


def _invoke(realizer: Realizer, entry: BeatEntry, methodology: Methodology):
    """Call the realizer, handing it the supe's decision if it wants it. A realizer that declares a
    ``methodology`` parameter (or **kwargs) receives the per-element breakdown — that is how a 3D
    realizer learns it must insert the FX department (``methodology.needs_fx``). Realizers that don't
    care keep the plain ``BeatEntry -> Clip`` signature untouched."""
    try:
        params = inspect.signature(realizer).parameters
    except (TypeError, ValueError):
        return realizer(entry)
    wants = "methodology" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())
    return realizer(entry, methodology=methodology) if wants else realizer(entry)


def make_supervised_realizer(
    *,
    realizers: dict[str, Realizer],
    decide: Decider = decide_methodology,
    load_refs: RefLoader | None = None,
    default_motion: bool = True,
    on_message: Callable[[object], None] | None = None,
) -> Render3D:
    """A ``render_3d`` leaf that decides methodology, then dispatches to ``realizers[routing_key]``.

    ``realizers`` maps ``3d_still``/``3d_motion``/``plate``/``hybrid`` to a leaf (sync or async
    ``BeatEntry -> Clip``). ``load_refs`` (optional) supplies the reference images the supe looks at;
    without it the decision is made from the beat's intent text alone.
    """

    async def render(entry: BeatEntry) -> Clip:
        subject = entry.intent.subject or entry.intent.heading
        vo = entry.realization.vo if entry.realization else ""
        refs = list(load_refs(entry)) if load_refs else []
        methodology = await decide(
            subject=subject, vo=vo, evidence=entry.intent.evidence,
            reference_images=refs, default_motion=default_motion, on_message=on_message,
        )
        resolved = _resolve(realizers, methodology.routing_key)
        if resolved is None:
            return Clip(licence="KNOWN",
                        acquisition_gap=f"no realizer for methodology {methodology.kind!r} (key {methodology.routing_key!r})")
        chosen_key, realizer = resolved
        result = _invoke(realizer, entry, methodology)
        clip = await result if inspect.isawaitable(result) else result
        return _stamp(clip, methodology, chosen_key)

    return render


def _stamp(clip: Clip, methodology: Methodology, chosen_key: str) -> Clip:
    """Record the supe's decision on the Clip for the trace (routed_via differs from kind on a fallback).
    Clip is frozen, so return a copy with the merged render_meta."""
    meta = dict(clip.render_meta or {})
    meta.update({
        "methodology": methodology.kind,
        "motion": str(methodology.motion),
        "routed_via": chosen_key,
        "supe_rationale": methodology.rationale,
    })
    if methodology.elements:  # the per-element breakdown, for the trace + turnover
        meta["elements"] = "; ".join(f"{el.name}[{el.discipline}]" for el in methodology.elements)
        meta["needs_fx"] = str(methodology.needs_fx)
    return dataclasses.replace(clip, render_meta=meta)
