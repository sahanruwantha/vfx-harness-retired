"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vfx_harness.domain.brief import Shot
from vfx_harness.observability.console import log
from vfx_harness.orchestration.ledger import load_axes, load_layers
from vfx_harness.orchestration.plan_authority import selected_artifact_path

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority


async def ensure_axes(
    shot: Shot,
    verbose: bool = True,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[tuple[str, str]]:
    """The critic rubric for this shot.

    Written by the PLAN stage (critic_axes.json): the planner has the deepest scene read
    AND knows the layer breakdown, so it is the only stage that can guarantee every axis
    has an owning layer. Missing axes are a migration failure, not an invitation for the
    builder to invent a different rubric.
    """

    if selected_authority is None:
        path = selected_artifact_path(shot.folder, "critic_axes.json")
    elif selected_authority.plan is None:
        path = shot.folder / "critic_axes.json"
    else:
        try:
            path = selected_authority.artifact_paths["critic_axes.json"]
        except KeyError as exc:
            raise ValueError("selected authority omits critic_axes.json") from exc
    if path.is_file():
        return load_axes(shot, selected_authority)
    raise FileNotFoundError(
        f"{path} missing — legacy builder-side rubric derivation has been removed; "
        "generate and gate the strict global plan"
    )


def _warn_unowned_axes(
    shot: Shot,
    axes: list[tuple[str, str]],
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> None:
    """Audit the axis↔layer mapping in BOTH directions — each catches a real bug we hit.

    axis with no layer  → unearnable: nobody can ever score it.
    layer with no axis  → unjudgeable: the layer is scored purely on OTHER layers' work,
                         so its own contribution is invisible and its revisions polish
                         someone else's layer (SH G60 "ENVIRONMENT" scored only on
                         typography and palette until `environment_depth` was added).

    An axis owned only by the LAST layer is fine and deliberately NOT flagged: with
    `owns` in force the earlier layers simply aren't judged on it, which is the point.
    """
    layers = load_layers(shot, selected_authority=selected_authority)
    if not any(g.owns for g in layers.values()):
        raise ValueError("strict layers.json contract requires owned axes; legacy unscoped judging is not supported")
    keys = {k for k, _ in axes}
    orphan = sorted(keys - {a for g in layers.values() for a in g.owns})
    if orphan:
        log(f"! axes owned by NO layer — unearnable: {orphan}")
    mute = sorted(g.id for g in layers.values() if not g.owns)
    if mute:
        log(f"! layers owning NO axis — judged only on other layers' work: {mute}")
    unknown = sorted({a for g in layers.values() for a in g.owns} - keys)
    if unknown:
        log(f"! layers claim axes not in the rubric: {unknown}")


def _owned_axes(axes: list[tuple[str, str]], layer) -> list[tuple[str, str]]:
    """Deterministically scope a layer before prompting or schema construction."""
    owned = set(getattr(layer, "owns", ()) or ())
    return [row for row in axes if row[0] in owned]


_MOTION_AXIS_WORDS = (
    "motion",
    "animation",
    "continuity",
    "timing",
    "trajectory",
    "interpolation",
    "velocity",
    "monotonic",
    "easing",
)


def _axes_need_motion(axes: list[tuple[str, str]]) -> bool:
    """Legacy-free helper for unlayered acceptance/eval calls only.

    Staged layers use their explicit ``temporal_evidence`` policy.  This heuristic remains
    for acceptance milestones, which do not have work-unit manifests.
    """
    text = " ".join(f"{key} {description}" for key, description in axes).lower()
    return any(word in text for word in _MOTION_AXIS_WORDS)


def _layer_needs_motion(layer) -> bool:
    return getattr(layer, "temporal_evidence", None) == "motion"


def _evidence_convergence_stop(layer, verdict: dict) -> bool:
    """Stop revisions when executable owned evidence has nothing left to repair.

    This does not manufacture a PASS: canonical verification may still record a judge
    conflict.  It prevents an evidence-free low score from sending the builder into more
    geometry edits after every authoritative contract is green.
    """
    if layer is None or verdict.get("pass"):
        return False
    authoritative = [row for row in (verdict.get("evidence") or []) if row.get("authoritative")]
    layer_id = str(getattr(layer, "id", ""))
    owned = [
        row for row in authoritative if str(row.get("fault_owner") or row.get("owner_layer") or layer_id) == layer_id
    ]
    return bool(
        owned
        and all(row.get("pass") for row in authoritative)
        and not verdict.get("issues")
        and not verdict.get("reference_unusable")
    )


def _builder_ticket_context(plan_excerpt: str, scope: str | None, axes: list[tuple[str, str]], layer=None) -> str:
    """Local retrieval query for prompt modules; never sent as extra prompt text.

    The full excerpt contains gotchas and regression notes about other departments. Feeding
    all of that to retrieval makes a finish ticket look like camera+city+asset work again.
    Headings, scope/done clauses, approach lines and explicit recipe calls express what this
    layer can actually change; the builder still receives the complete excerpt at kickoff.
    """
    ticket_lines = []
    for line in plan_excerpt.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith(("### ", "**Scope", "**Done", "**G"))
            or "build/approach:" in stripped
            or "find_recipe(" in stripped
        ):
            ticket_lines.append(stripped)
    layer_bits = [str(getattr(layer, key, "") or "") for key in ("title", "reads", "script")]
    axis_bits = [f"{name}: {description}" for name, description in axes]
    return "\n".join(x for x in ("\n".join(ticket_lines), scope or "", *layer_bits, *axis_bits) if x)
