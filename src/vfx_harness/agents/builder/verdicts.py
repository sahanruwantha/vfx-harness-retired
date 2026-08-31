"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

from PIL import Image

from vfx_harness.agents.builder.evidence import (
    _forecast_blocker_ids,
    _geometry_protected_vis_ids,
    _scene_contract_issue,
    _scene_ids_active_at_declared_frames,
    _scene_ids_active_on_layer,
    _unit_evidence_ids,
)
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.provisional_judgment import (
    _composition_judge_unit,
)
from vfx_harness.agents.builder.provisional_judgment import (
    _load_provisional_decisions as _load_provisional_decisions_impl,
)
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.image_debts import UNPAID_IMAGE_DEBT_RULE, image_contract_debt_cards, normalize_evidence_id
from vfx_harness.domain.work_units import (
    LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
    UNIT_JUDGE_CLAIM_COVERAGE_RULE,
    unearned_look_judge_frames,
)
from vfx_harness.evidence import scene_checks
from vfx_harness.observability import run_artifacts, transcript
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.observability.worklists import load_unit_worklist
from vfx_harness.orchestration.judgment_debt_state import current_judgment_debt_states
from vfx_harness.orchestration.ledger import Milestone
from vfx_harness.orchestration.unit_state import unit_digest

__all__ = ("_composition_judge_unit", "_load_provisional_decisions")


def _load_provisional_decisions(shot: Shot, layer_id: str) -> tuple[dict, ...]:
    """Compatibility seam for verdict-level callers and monkeypatched tests."""
    return _load_provisional_decisions_impl(
        shot,
        layer_id,
        state_loader=current_judgment_debt_states,
    )


def _executable_unit_verdict(
    unit,
    frame: int,
    axes: list[tuple[str, str]],
    evidence: list[dict],
    contract_frames: dict[str, int] | None = None,
    extra_required_ids: set[str] | None = None,
    inactive_ids: set[str] | None = None,
) -> dict | None:
    """Let exact executable claims decide an atomic unit without a vision call."""
    if unit is None:
        return None
    required = [
        claim
        for claim in unit.evaluation.claims
        if claim.required and int(frame) in claim.moments
    ]
    if not required or any(claim.authority != "executable_required" for claim in required):
        return None

    if int(frame) in unearned_look_judge_frames(unit):
        return _look_without_image_domain_verdict(unit, int(frame), axes)
    # A frame-scoped contract produces its reading at ITS declared frame only. A claim
    # judging [72, 150] that binds vis-f72 AND vis-f150 was faulted at each frame for
    # the OTHER frame's row (run 20260825: detail_instancing 'required bound evidence
    # was not produced' at both frames with both rows green at their own). A binding is
    # due here unless it is scoped to a different frame this claim also judges; a
    # binding scoped to a frame NO claim moment covers stays due — loudly missing beats
    # silently never-checked.
    binding_moments: dict[str, set[int]] = {}
    declared_moments: dict[str, set[int]] = {}
    for claim in required:
        for binding in claim.evidence:
            binding_moments.setdefault(binding.id, set()).update(
                int(moment) for moment in claim.moments
            )
            if getattr(binding, "moments", None) is not None:
                declared_moments.setdefault(binding.id, set()).update(
                    int(moment) for moment in binding.moments
                )
    def _due_here(binding_id: str) -> bool:
        # the author's declared moments are the model; frame-inference is only the
        # fallback for undeclared bindings on frame-carrying contracts
        declared_set = declared_moments.get(binding_id)
        if declared_set is not None:
            return int(frame) in declared_set
        declared = (contract_frames or {}).get(binding_id)
        if declared is None or int(declared) == int(frame):
            return True
        return int(declared) not in binding_moments.get(binding_id, set())
    required_ids = {
        binding.id
        for claim in required
        for binding in claim.evidence
        if _due_here(binding.id)
    }
    if extra_required_ids:
        required_ids |= {str(item) for item in extra_required_ids}
    if inactive_ids:
        required_ids -= {str(item) for item in inactive_ids}
    by_id = {str(row.get("id")): row for row in evidence if row.get("id")}
    missing = sorted(required_ids - set(by_id))
    failures = [
        by_id[eid]
        for eid in sorted(required_ids & set(by_id))
        if not by_id[eid].get("pass")
    ]
    worklist_failures = [
        row for row in evidence if row.get("source") == "builder_state" and not row.get("pass")
    ]
    passed = not missing and not failures and not worklist_failures
    issues = [_scene_contract_issue(row) for row in [*failures, *worklist_failures]]

    due_image = {
        card.id
        for card in image_contract_debt_cards(unit)
        if card.frame == int(frame)
    }
    for eid in missing:
        bare = normalize_evidence_id(eid)
        if bare in due_image:
            issues.append(
                f"{bare} unpaid image-contract debt: no checks.json or runtime_checks.json "
                "row. Call propose_checks with this exact id, frame, property kind, and axis "
                "on an existing run render. A role or control retag cannot produce it. "
                + UNPAID_IMAGE_DEBT_RULE
            )
        else:
            issues.append(f"{bare} required bound evidence was not produced")
    scored_axes = [key for key, _description in axes]
    score = 5 if passed else 1
    return {
        "scores": dict.fromkeys(scored_axes, score),
        "mean": float(score),
        "pass": passed,
        "issues": issues,
        "scored_axes": scored_axes,
        "na_axes": [],
        "observations": [],
        "evidence": evidence,
        "evidence_failures": [*failures, *worklist_failures],
        "missing_evidence": missing,
        "decided_by": "unit_executable_evidence",
        "judge_conflict": False,
        "contract_gap": bool(missing),
    }


def _provisional_decisions_for_layer(
    base_requirements: list[dict],
    selected_requirements: list[dict],
    layer_id: str,
    *,
    falsifications: tuple[dict, ...] | list[dict] = (),
    bundle_hash: str | None = None,
) -> tuple[dict, ...]:
    """Provisional owned decisions that still owe build-time judgment."""
    owned = {
        str(row.get("id")): {
            "evidence_domains": tuple(
                (row.get("resolution") or {}).get("evidence_domains") or ()
            ),
            "statement": str(row.get("statement") or "").strip(),
        }
        for row in base_requirements
        if isinstance(row, dict)
        and (row.get("resolution") or {}).get("kind") == "deferred_owner"
        and str((row.get("resolution") or {}).get("owner_layer") or "") == str(layer_id)
    }
    selected_by_id = {
        str(row.get("id")): row
        for row in selected_requirements
        if isinstance(row, dict) and row.get("id")
    }
    rows = []
    found: set[str] = set()
    for row in selected_requirements:
        requirement_id = str(row.get("id") or "")
        resolution = row.get("resolution") or {}
        if requirement_id not in owned:
            continue
        decisions = []
        if resolution.get("kind") == "decision":
            decisions.append({
                "statement": resolution.get("decision") or resolution.get("statement"),
                "decision_strength": resolution.get("decision_strength"),
            })
        decisions.extend(
            binding
            for binding in (resolution.get("domain_bindings") or ())
            if isinstance(binding, dict)
            and binding.get("kind") == "provisional_decision"
        )
        provisional = next(
            (
                decision
                for decision in decisions
                if str(decision.get("decision_strength") or "")
                in {"approved_start", "planner_start"}
            ),
            None,
        )
        if provisional is None:
            continue
        strength = str(provisional.get("decision_strength") or "")
        # The selected binding carries strength, never new authored intent. Always judge
        # the immutable global requirement proposition so meta-text such as "deferred to
        # lookdev" cannot replace "reads as the specific hotel" (HIR-0149).
        statement = owned[requirement_id]["statement"]
        if statement:
            rows.append(
                {
                    "id": requirement_id,
                    "statement": statement,
                    "decision_strength": strength,
                    "evidence_domains": owned[requirement_id]["evidence_domains"],
                }
            )
            found.add(requirement_id)

    # A rematerialization may replace the selected requirement's decision resolution
    # with executable contract ids. That closes mechanical producer debt, but cannot
    # erase an already-consumed current-bundle finding that says the approved/planner
    # start failed independent reference judgment. The typed finding is the lineage;
    # old-bundle records and confirmed outcomes remain inert.
    for finding in falsifications:
        if not isinstance(finding, dict):
            continue
        identities = finding.get("identities") or {}
        if bundle_hash and str(identities.get("bundle_hash") or "") != str(bundle_hash):
            continue
        for decision in finding.get("decisions") or ():
            if not isinstance(decision, dict):
                continue
            requirement_id = str(decision.get("id") or "")
            strength = str(decision.get("strength") or "")
            if (
                requirement_id in found
                or requirement_id not in owned
                or strength not in {"approved_start", "planner_start"}
            ):
                continue
            selected = selected_by_id.get(requirement_id) or {}
            resolution = selected.get("resolution") or {}
            if (
                resolution.get("kind") == "decision"
                and resolution.get("decision_strength") == "confirmed_outcome"
            ):
                continue
            statement = str(selected.get("statement") or "").strip()
            if not statement:
                statement = owned[requirement_id]["statement"]
            if not statement:
                continue
            rows.append(
                {
                    "id": requirement_id,
                    "statement": statement,
                    "decision_strength": strength,
                    "evidence_domains": owned[requirement_id]["evidence_domains"],
                }
            )
            found.add(requirement_id)
    return tuple(rows)


def _required_claims_at(unit, frame: int):
    """Required claims whose moments include this canonical frame."""
    claims = tuple(getattr(getattr(unit, "evaluation", None), "claims", ()) or ())
    return [
        claim
        for claim in claims
        if getattr(claim, "required", False) and int(frame) in (getattr(claim, "moments", ()) or ())
    ]


def _look_without_image_domain_verdict(
    unit, frame: int, axes: list[tuple[str, str]]
) -> dict:
    """Look ownership with only scene claims is not a 5.0 seal (HIR-0046).

    Canonical ``contract_gap`` skips repair only when ``issues`` is empty.
    """

    scored_axes = [key for key, _description in axes]
    axis = scored_axes[0] if scored_axes else "coverage"
    unit_id = str(getattr(unit, "id", "unit") or "unit")
    capabilities = tuple(getattr(unit, "look_capabilities", ()) or ())
    observation = {
        "id": "look-without-image-domain",
        "kind": "measurable",
        "axis": axis,
        "property": "look_image_domain",
        "observation": (
            f"unit {unit_id} declares look_capabilities {list(capabilities)} "
            f"but frame {int(frame)} has no required image-domain claim"
        ),
        "action": (
            "add a required claim that asserts image and binds image_contract "
            "(or qualification / human_decision), or declare look_capabilities []"
        ),
        "moment": int(frame),
        "roles": list(getattr(getattr(unit, "mutates", None), "roles", ()) or ()),
        "claim_id": None,
        "check_ids": [],
        "panel_ids": [],
    }
    gap = {
        "state": "contract_gap",
        "observation": observation,
        "reason": LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
        "check_ids": [],
    }
    return {
        "scores": dict.fromkeys(scored_axes, 1),
        "mean": 1.0,
        "pass": False,
        "issues": [],
        "scored_axes": scored_axes,
        "na_axes": [],
        "observations": [],
        "evidence": [],
        "evidence_failures": [],
        "missing_evidence": [],
        "decided_by": "look_without_image_domain",
        "judge_conflict": False,
        "contract_gap": True,
        "contract_gaps": [gap],
        "observation_reconciliation": [gap],
    }


def _uncovered_judge_frame_verdict(unit, frame: int, axes: list[tuple[str, str]]) -> dict:
    """A unit judge frame with no required claim is not a critic look vote (HIR-0045).

    Canonical ``contract_gap`` skips repair only when ``issues`` is empty, so the
    teaching text lives on the gap observation, not ``issues``.
    """

    scored_axes = [key for key, _description in axes]
    axis = scored_axes[0] if scored_axes else "coverage"
    unit_id = str(getattr(unit, "id", "unit") or "unit")
    roles = tuple(getattr(getattr(unit, "mutates", None), "roles", ()) or ())
    observation = {
        "id": "uncovered-judge-frame",
        "kind": "measurable",
        "axis": axis,
        "property": "required_claim_moment",
        "observation": (
            f"unit {unit_id} judges frame {int(frame)} but no required claim "
            "includes that moment"
        ),
        "action": (
            "add a required claim whose moments include this frame, or drop the "
            "frame from the unit judge set"
        ),
        "moment": int(frame),
        "roles": list(roles),
        "claim_id": None,
        "check_ids": [],
        "panel_ids": [],
    }
    gap = {
        "state": "contract_gap",
        "observation": observation,
        "reason": UNIT_JUDGE_CLAIM_COVERAGE_RULE,
        "check_ids": [],
    }
    return {
        "scores": dict.fromkeys(scored_axes, 1),
        "mean": 1.0,
        "pass": False,
        "issues": [],
        "scored_axes": scored_axes,
        "na_axes": [],
        "observations": [],
        "evidence": [],
        "evidence_failures": [],
        "missing_evidence": [],
        "decided_by": "uncovered_judge_frame",
        "judge_conflict": False,
        "contract_gap": True,
        "contract_gaps": [gap],
        "observation_reconciliation": [gap],
    }


def _lookless_without_executable_verdict(unit, frame: int, axes: list[tuple[str, str]]) -> dict:
    """Look-less evaluation must not fall through to ``_judge`` (HIR-0032, HIR-0039)."""
    scored_axes = [key for key, _description in axes]
    required = [
        claim
        for claim in unit.evaluation.claims
        if claim.required and int(frame) in claim.moments
    ]
    if not required:
        return _uncovered_judge_frame_verdict(unit, frame, axes)
    return {
        "scores": dict.fromkeys(scored_axes, 1),
        "mean": 1.0,
        "pass": False,
        "issues": [
            "look-less evaluation cannot be settled by a visual critic; required "
            "claims at this frame must be executable_required"
        ],
        "scored_axes": scored_axes,
        "na_axes": [],
        "observations": [],
        "evidence": [],
        "evidence_failures": [],
        "missing_evidence": [],
        "decided_by": "lookless_requires_executable_claims",
        "judge_conflict": False,
        "contract_gap": True,
    }


def _provisional_composition_contract_gap(
    judged: dict, active_unit, frame: int
) -> dict:
    """Route qualified composed criticism to replan without losing its evidence.

    The critic may identify a concrete defect under qualified qualitative authority,
    but the synthetic composition unit owns no executable script.  Preserve that exact
    observation as a contract-gap row instead of retaining a broad repair instruction or
    replacing it with an opaque score-only summary.
    """
    # HIR-0032's no-signal result is an inability to observe the proposition, not
    # independent qualitative evidence that the proposition is false.  Preserve the
    # typed unpaid attempt so the provisional debt remains unresolved; converting it
    # here would make an empty plate publish a false HIR-0137 falsification.
    if judged.get("decided_by") == "no_optical_signal":
        return judged

    provisional = tuple(
        getattr(active_unit, "provisional_requirement_ids", ()) or ()
    )
    debt_ids = tuple(getattr(active_unit, "provisional_debt_ids", ()) or ())
    prefixes = (
        *(f"requirement:{requirement_id}:" for requirement_id in provisional),
        *(f"judgment-debt:{debt_id}:" for debt_id in debt_ids),
    )
    rows = list(judged.get("observation_reconciliation") or [])
    converted = []
    retained = []
    for row in rows:
        observation = dict(row.get("observation") or {})
        claim_id = str(observation.get("claim_id") or "")
        if row.get("state") == "actionable" and claim_id.startswith(prefixes):
            converted.append(
                {
                    **row,
                    "state": "contract_gap",
                    "observation": observation,
                    "reason": (
                        "qualified canonical judgment falsified a provisional layer "
                        "decision, but composed judgment grants no cross-unit mutation "
                        "authority; transactionally replan the cited producer closure"
                    ),
                }
            )
        else:
            retained.append(row)
    if not converted:
        converted = [
            {
                "state": "contract_gap",
                "observation": {
                    "id": f"provisional-requirement-{requirement_id}",
                    "kind": "qualitative",
                    "observation": (
                        "canonical reference judgment falsified provisional requirement "
                        f"{requirement_id}"
                    ),
                    "action": (
                        "replan the bounded producer units; composed judgment grants no "
                        "cross-unit mutation authority"
                    ),
                    "axis": "reference_match",
                    "property": "reference_identity",
                    "moment": int(frame),
                    "roles": list(getattr(active_unit.mutates, "roles", ()) or ()),
                    "claim_id": (
                        f"judgment-debt:{debt_ids[index]}"
                        if index < len(debt_ids)
                        else f"requirement:{requirement_id}"
                    ),
                    "check_ids": [],
                    "panel_ids": [],
                },
                "reason": (
                    "an approved/planner start owned by this layer remains provisional "
                    "until independent canonical reference judgment passes"
                ),
                "check_ids": [],
            }
            for index, requirement_id in enumerate(provisional)
        ]
    judged["issues"] = []
    judged["contract_gap"] = True
    judged["contract_gaps"] = converted
    judged["observation_reconciliation"] = [*retained, *converted]
    judged["decided_by"] = "provisional_requirement_contract_gap"
    judged["judge_conflict"] = False
    return judged


async def _judge_unit_or_layer(
    shot: Shot,
    m: Milestone,
    candidate_rel: str,
    axes: list[tuple[str, str]],
    session: BlenderSession,
    verbose: bool,
    scope: str | None,
    evidence: list[dict],
    *,
    active_unit=None,
    selected_authority=None,
    **kwargs,
) -> dict:

    try:
        contract_frames = {
            str(r.get("id")): int(r.get("frame"))
            for r in scene_checks.load_rows(shot.folder, selected_authority)
            if isinstance(r, dict) and r.get("id") and r.get("frame") is not None
        }
    except (OSError, ValueError):
        contract_frames = {}
    if active_unit is not None and not _required_claims_at(active_unit, int(m.frame)):
        return _uncovered_judge_frame_verdict(active_unit, int(m.frame), axes)

    if active_unit is not None and int(m.frame) in unearned_look_judge_frames(active_unit):
        return _look_without_image_domain_verdict(active_unit, int(m.frame), axes)
    extra_required = set()
    layer = kwargs.pop("layer", None)
    if active_unit is not None and layer is not None:
        try:
            extra_required = _scene_ids_active_at_declared_frames(
                shot,
                str(layer.id),
                _geometry_protected_vis_ids(
                    shot,
                    layer,
                    active_unit,
                    selected_authority=selected_authority,
                ),
                [int(m.frame)],
                selected_authority=selected_authority,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            extra_required = set()
    extra_required.update(_forecast_blocker_ids(evidence))
    inactive_ids: set[str] = set()
    bound_ids = set(_unit_evidence_ids(active_unit, int(m.frame)) or set())
    if active_unit is not None and layer is not None:
        try:
            due = _scene_ids_active_on_layer(
                shot,
                str(layer.id),
                bound_ids,
                [int(m.frame)],
                selected_authority=selected_authority,
            )
            inactive_ids = bound_ids - due
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            inactive_ids = set()
    verdict = _executable_unit_verdict(
        active_unit,
        int(m.frame),
        axes,
        evidence,
        contract_frames=contract_frames,
        extra_required_ids=extra_required,
        inactive_ids=inactive_ids,
    )
    if verdict is None:
        if (
            active_unit is not None
            and not tuple(getattr(active_unit, "look_capabilities", ()) or ())
            and not tuple(getattr(active_unit, "provisional_requirement_ids", ()) or ())
        ):
            return _lookless_without_executable_verdict(active_unit, int(m.frame), axes)
        judged = await builder_package()._judge(
            shot,
            m,
            candidate_rel,
            axes,
            session,
            verbose,
            scope,
            evidence=evidence,
            active_unit=active_unit,
            selected_authority=selected_authority,
            **kwargs,
        )
        provisional = tuple(
            getattr(active_unit, "provisional_requirement_ids", ()) or ()
        )
        if provisional and not judged.get("pass"):
            _provisional_composition_contract_gap(judged, active_unit, int(m.frame))
        return judged
    status = "PASS ✅" if verdict["pass"] else "REVISE ✎"
    expected = len(
        ((_unit_evidence_ids(active_unit, int(m.frame)) or set()) | extra_required)
        - inactive_ids
    )
    observed = expected - len(verdict["missing_evidence"])
    log(f"unit evidence: {observed}/{expected} bound checks observed · {status}", 1)
    for issue in verdict["issues"][:6]:
        log(f"· fix: {issue}", 2)
    transcript.event(
        "unit_evidence",
        milestone=m.id,
        frame=m.frame,
        candidate=candidate_rel,
        verdict="pass" if verdict["pass"] else "revise",
        decided_by=verdict["decided_by"],
        evidence=evidence,
        missing_evidence=verdict["missing_evidence"],
    )
    return verdict


def _worklist_evidence(shot_folder: str | Path, layer_id: str, active_unit=None) -> list[dict]:
    """Turn builder-declared unfinished work into a deterministic handoff blocker.

    The worklist is durable agent state, not a visual opinion.  If the builder says a
    scoped item remains open, a flattering critic score must not erase that fact.  The
    failed evidence row routes through the ordinary evidence gate, which reopens one
    repair round.  Missing/empty worklists remain non-authoritative so older shots do not
    acquire a new contract merely by being inspected.
    """
    if active_unit is None:
        return []
    constituent_units = tuple(getattr(active_unit, "worklist_units", ()) or ())
    if constituent_units:
        return [
            row
            for unit in constituent_units
            for row in _worklist_evidence(shot_folder, layer_id, unit)
        ]
    try:

        _path, state = load_unit_worklist(
            shot_folder,
            layer_id=layer_id,
            unit_id=str(active_unit.id),
            unit_hash=unit_digest(active_unit),
        )
        items = [str(item) for item in state.get("items", [])]
        done = {str(item) for item in state.get("done", [])}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return [
            {
                "id": f"L{layer_id}@{active_unit.id}-builder-worklist-valid",
                "axis": "",
                "metric": "builder_worklist",
                "value": None,
                "target": "valid durable worklist",
                "pass": False,
                "origin": "harness",
                "source": "builder_state",
                "authoritative": True,
                "owner_layer": layer_id,
                "fault_owner": layer_id,
                "error": str(exc)[:160],
            }
        ]
    if not items:
        return []
    left = [item for item in items if item not in done]
    return [
        {
            "id": f"L{layer_id}@{active_unit.id}-builder-worklist-complete",
            "axis": "",
            "metric": "builder_worklist_open_items",
            "value": len(left),
            "target": "= 0 ± 0",
            "pass": not left,
            "origin": "harness",
            "source": "builder_state",
            "authoritative": True,
            "owner_layer": layer_id,
            "fault_owner": layer_id,
            "open_items": left[:6],
        }
    ]


def _layer_motion_frames(layer, m: Milestone, total_frames: int) -> list[int] | None:
    """Return one strip that spans the whole temporal unit, not one local moment.

    A motion-owned layer with several judge frames is a sequence contract.  Showing the
    primary frame's local acceptance strip can contain only a deliberate hold and still
    invite a high continuity score.  Preserve every judge frame and add the midpoint of
    each interval so starts, transitions and settles all have temporal evidence.
    """
    judges = sorted({int(frame) for frame, _ref in (getattr(layer, "judges", ()) or ())})
    if len(judges) > 1:
        mids = [round((left + right) / 2) for left, right in pairwise(judges)]
        return sorted({frame for frame in [*judges, *mids] if 1 <= frame <= total_frames})
    if m.strip:
        return sorted({int(frame) for frame in m.strip if 1 <= int(frame) <= total_frames})
    return None


def _stash_motion_strip(
    session: BlenderSession,
    shot: Shot,
    m: Milestone,
    tag: str,
    span: int = 6,
    scale: float = 0.4,
    frames_override: list[int] | None = None,
):
    """Montage a few frames around the judge frame so the critic can judge MOTION — a
    single still can't show blur or continuity. Returns (rel_path, frames).

    Prefers the PLAN's strip for this moment (acceptance.json), which the planner chose
    to cover the beat with a stated max gap. The old default only stepped FORWARD from
    the judge frame, so a botched approach was structurally invisible: barrel_roll's M2
    was judged at f20 (correctly near-black, scored 4.0) while f16-f18 sat at mean ~57
    with the world-swap in full view — and the strip [12,16,18,20,22] that would have
    caught it was parsed, stored, and never used.
    """
    if frames_override:
        frames = sorted({f for f in frames_override if 1 <= f <= shot.frames})
    elif m.strip:
        frames = sorted({f for f in m.strip if 1 <= f <= shot.frames})
    else:
        frames = sorted({m.frame, min(shot.frames, m.frame + span), min(shot.frames, m.frame + 2 * span)})
    MAX = 8  # enough for four judge beats plus the transitions between them
    if len(frames) > MAX:
        # Thin the middle, but the JUDGE FRAME is never droppable — it is the frame the
        # verdict is about. (A naive sorted(...)[:MAX] silently cut f72 off SH's M1.)
        others = [f for f in frames if f != m.frame]
        step = max(1, round(len(others) / (MAX - 1)))
        thinned = others[::step][: MAX - 1]
        if others and others[-1] not in thinned:  # always keep the far end of the beat
            thinned = [*thinned[: MAX - 2], others[-1]]
        frames = sorted({*thinned, m.frame})
    ims = [Image.open(session.render(frame=f, mode="eevee", scale=scale)).convert("RGB") for f in frames]
    h = min(im.height for im in ims)
    ims = [im.resize((max(1, round(im.width * h / im.height)), h)) for im in ims]
    sheet = Image.new("RGB", (sum(im.width for im in ims), h), (10, 10, 12))
    x = 0
    for im in ims:
        sheet.paste(im, (x, 0))
        x += im.width
    dest = run_artifacts.renders_dir(shot.folder) / f"{m.id}_{tag}_motion.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest)
    return dest.relative_to(shot.folder).as_posix(), frames


# --------------------------------------------------------------------------- #
# The loop                                                                     #
