"""The plan gate — the deterministic bar a plan must clear before anything is built on it.

The plan is the specification every later stage is judged against, and it was the only
artifact nothing checked. `ledger.load_layers` reads `layers.json` for shape; past that, a
plan could claim anything and the first thing to notice would be a layer thrashing against
a target that was never reachable.

The organising finding, measured across both shots and all four plan documents:

    spike citations      PRECISE and TRUE. `spike_05.out` line 9 is cited for "the default
                         is 100" and line 9 reads `volumetric_start/end: ... 100.0`.
    web research         0 URLs. Step 6 of the planner prompt requires "source links in
                         the ticket". Not one plan contains a link.
    ask_supervisor       never fired across two shots.
    [unknown] tags       0, in every document, and research is gated on that tag.

The difference is not diligence. `spike` WRITES A FILE to the lab directory; `WebSearch`
leaves nothing behind unless the model chooses to type a URL. Evidence a tool physically
deposits survives and stays checkable. Evidence the model is merely asked to record does
not. Every check here is therefore a check on an ARTIFACT, never on a claim about one.

The checks, each free and each with a known-bad fixture in the test suite:

    grounded      every stated fingerprint number re-derives from the plate it describes
                  (eval.grounding — 153/160 on the shipped plans; the 7 failures were one
                  broken metric, not an inventive planner)
    citations     every file a plan cites RESOLVES, and a cited line number exists in it.
                  Not fabrication — link rot: `server_to_hansa/plan.md` cites
                  `../barrel_roll/logs/plan_lab_draft/spike_05.out` five times and that
                  path stopped existing when the shot was archived. The claims are true and
                  a build agent reading them today gets nothing.
    evidence      a ticket claiming `✓spiked` must cite a lab file; a ticket claiming
                  research must leave a source link. A confidence tag is a self-assessment
                  and worth exactly the artifact under it.
    done-checks   do the plan's own checks WORK — is each satisfiable by the reference it
                  names, and does it REJECT a known-bad render? Grounding asks whether the
                  numbers are real; this asks whether the checks can fail anything. Both are
                  needed: this plan scored 95/95 on grounding while carrying a check its own
                  plate cannot pass. See checks.py — the contract, re-run by the gate.
    contracts     plans/global.md, the next just-in-time work-unit plan, layers.json,
                  acceptance.json, critic_axes.json and live-scene
                  contracts must agree: an axis a layer OWNS must exist in the rubric,
                  every ref must be real, and a scene fact must belong to a frame/axis its
                  layer actually judges. A layer owning an axis the critic does not score
                  cannot be judged on it, and nothing else in the pipeline notices.

Severity is either `blocking` (a build on this plan is aiming at something that is not
there) or `warn` (the plan is weaker than it claims, but buildable).

Exit codes: 0 clean · 3 at least one blocking finding
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from vfx_harness.domain.work_units import read_document
from vfx_harness.evaluation.plan_gate.types import (
    Finding,
)
from vfx_harness.evidence.scene_checks import BBOX_KINDS


def _check_unit_dependencies(folder: Path) -> list[Finding]:
    """Report every layer-local dependency that names no unit in its own layer.

    This intentionally runs before the typed loader. It does not replace schema or cycle
    validation; it makes one common structural failure exhaustive instead of allowing the
    loader's first exception to hide identical failures in later layers.
    """
    try:
        layers = read_document(folder / "layers.json")
    except (OSError, ValueError):
        return []

    findings: list[Finding] = []
    for layer_index, layer in enumerate(layers):
        stages = layer.get("stages")
        if not isinstance(stages, list):
            continue
        known = {
            str(stage.get("id"))
            for stage in stages
            if isinstance(stage, dict) and str(stage.get("id") or "").strip()
        }
        for stage_index, stage in enumerate(stages):
            if not isinstance(stage, dict) or not isinstance(stage.get("depends_on", []), list):
                continue
            unit_id = str(stage.get("id") or f"stages[{stage_index}]")
            missing = sorted(
                {
                    str(dep)
                    for dep in stage.get("depends_on", [])
                    if isinstance(dep, str) and dep not in known
                }
            )
            if not missing:
                continue
            findings.append(
                Finding(
                    "hierarchy",
                    True,
                    f"layers.json.layers[{layer_index}].stages.{unit_id}",
                    f"depends on unknown units: {', '.join(missing)}",
                    "depends_on is layer-local; remove cross-layer ids because accepted "
                    "layer order and protected interfaces already carry that dependency",
                )
            )
    return findings


def _camera_only_host_roles(stages: dict[str, dict]) -> frozenset[str]:
    """Roles mutated by a camera provider that does not also provide geometry."""
    roles: set[str] = set()
    for unit in stages.values():
        provides = {str(item) for item in (unit.get("provides") or [])}
        if "camera" not in provides or "geometry" in provides:
            continue
        mutates = unit.get("mutates") or {}
        roles.update(str(item) for item in mutates.get("roles") or [])
        roles.update(str(item) for item in mutates.get("controls") or [])
    return frozenset(roles)


def _role_matches_any(role: str, selectors: frozenset[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(role, selector) or fnmatch.fnmatchcase(selector, role)
        for selector in selectors
    )


def _is_subject_framing_row(row: dict, camera_only_roles: frozenset[str]) -> bool:
    """True when a row can certify subject composition (HIR-0127).

    `projected_origin` of a camera-only host is alignment, not framing. `bbox_*` of a
    rendered subject is framing. A bbox whose every role is a camera-only host is not.
    """

    if str(row.get("kind") or "") not in BBOX_KINDS:
        return False
    roles = [str(item) for item in row.get("roles") or [] if str(item)]
    if not roles or not camera_only_roles:
        return True
    return not all(_role_matches_any(role, camera_only_roles) for role in roles)


def _deferred_subject_framing_covers(
    scene_rows: list,
    layer_id: str,
    frame: int,
    camera_only_roles: frozenset[str],
) -> bool:
    for row in scene_rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("owner_layer") or "") != layer_id:
            continue
        try:
            owner = int(row.get("owner_layer"))
            active = int(row.get("activates_at") or owner)
        except (TypeError, ValueError):
            continue
        if active <= owner:
            continue
        if row.get("frame") != frame:
            continue
        if _is_subject_framing_row(row, camera_only_roles):
            return True
    return False
