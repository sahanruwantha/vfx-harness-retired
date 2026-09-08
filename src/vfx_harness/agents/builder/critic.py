"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

from dataclasses import asdict

from vfx_harness.agents import critic_images, critic_qualification, critic_session
from vfx_harness.agents.build_prompts import (
    CRITIC_SYSTEM,
    critic_prompt,
)
from vfx_harness.agents.builder.axes import _axes_need_motion
from vfx_harness.agents.builder.critic_focus import (
    _apply_evidence_gate,
    _audit_panel_citations,
    _claim_context,
    _filter_critic_issues,
    _focus_references,
    _focus_requests,
    _image_optical_signal,
    _make_focus_panels,
    _required_focus_requests,
)
from vfx_harness.agents.builder.evidence import _unit_raster_mode
from vfx_harness.agents.builder.execution_guard import (
    ExecutionGuard,
)
from vfx_harness.agents.builder.models import PASS_MEAN, PASS_MIN, critic_model
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.critic_verdict import evaluate_critic_scores as _verdict
from vfx_harness.observability import transcript
from vfx_harness.observability.console import log
from vfx_harness.orchestration import authority_selection
from vfx_harness.orchestration.ledger import Milestone


def _critic_images(
    shot: Shot, reference: str, candidate: str, *, focus_panels=None,
    motion_rel=None, motion_frames=None, prior_rel=None, prior_mean=None,
):
    """Attach the complete declared manifest, or refuse before querying the critic."""
    panels = focus_panels or []
    images = [("reference", reference), ("candidate", candidate)]
    images.extend(("focus", panel["image_rel"]) for panel in panels)
    if motion_rel is not None:
        images.append(("motion", motion_rel))
    if prior_rel is not None:
        images.append(("prior", prior_rel))
    slots = critic_images.compile_images(tuple(images))
    for slot in slots:
        if not (shot.folder / slot.path).is_file():
            raise BlenderError(f"critic {slot.label} missing at {slot.path}; prepare the declared image before judging")
    descriptions = [critic_images.describe(slots)]
    panel_index = 0
    for slot in slots:
        label = slot.label
        if slot.role == "focus":
            panel = panels[panel_index]
            panel_index += 1
            label += (
                f" — id {panel['id']}, axis {panel['axis']}, crop {panel['crop']} within "
                f"source frame f{panel['source_frame']} against {panel['reference']} "
                f"(TOP-LEFT normalized), optical res_pct {panel['res_pct']}. "
                f"It contains aligned CANDIDATE | REFERENCE and a 50/50 wipe. Reason: {panel['reason']}"
            )
        elif slot.role == "motion":
            label += f", frames {motion_frames}"
        elif slot.role == "prior":
            label += (
                f", which scored {prior_mean}. Do NOT score this image. Use it to say whether "
                "the candidate improved or regressed, and record as a typed observation "
                "anything the previous attempt got right that the candidate has lost."
            )
        descriptions.append(label)
    return tuple(images), "\n".join(descriptions)


async def _critique(
    shot: Shot,
    m: Milestone,
    candidate_rel: str,
    axes: list[tuple[str, str]],
    session: BlenderSession,
    verbose: bool,
    scope: str | None = None,
    prior_rel: str | None = None,
    prior_mean: float | None = None,
    evidence: list[dict] | None = None,
    review_mode: str = "observer",
    focus_panels: list[dict] | None = None,
    motion_evidence: tuple[str, list[int]] | None = None,
    allow_motion: bool | None = None,
    focus_frames: list[int] | tuple[int, ...] | set[int] | None = None,
    active_unit=None,
    selected_authority: authority_selection.ResolvedSelectedAuthority | None = None,
    execution_guard: ExecutionGuard | None = None,
) -> dict:
    if execution_guard is not None:
        execution_guard.check(
            f"query {review_mode} critic for {execution_guard.label} at frame {m.frame}"
        )
    if selected_authority is None:
        selected_authority = authority_selection.resolve_selected_authority(
            shot.folder
        )
    motion_rel, motion_frames = motion_evidence or (None, None)
    wants_motion = _axes_need_motion(axes) if allow_motion is None else allow_motion
    if motion_rel is None and shot.frontmatter.get("type") == "motion" and shot.frames > 1 and wants_motion:
        stem = candidate_rel.split("/")[-1].split(".")[0]
        motion_rel, motion_frames = builder_package()._stash_motion_strip(session, shot, m, stem)
    focus_references = _focus_references(
        shot,
        m,
        focus_frames,
        selected_authority=selected_authority,
    )
    claim_manifest, claim_bindings, _declared_qualified_claims = _claim_context(
        shot,
        m,
        enabled=scope is not None,
        active_unit=active_unit,
        selected_authority=selected_authority,
    )
    log(
        f"critic[{critic_model()}]: scoring {candidate_rel} vs {m.ref}"
        + (f" (+motion {motion_frames})" if motion_rel else ""),
        1,
    )
    prompt = critic_prompt(
        shot,
        m,
        candidate_rel,
        axes,
        motion_rel,
        motion_frames,
        scope,
        evidence=evidence,
        claims=claim_manifest,
        review_mode=review_mode,
        focus_panels=focus_panels,
        # What may be ASKED is bounded by what the plate can SHOW. The same function
        # that chose the raster mode names the medium, so the rubric cannot drift from
        # the render: a workbench_solid debt suppresses materials, and a critic given an
        # appearance rubric there returns "reads flat grey" about a facade whose emissive
        # window mask is simply not in that image (HIR-0241).
        render_medium=_unit_raster_mode(active_unit),
        focus_frames=sorted(focus_references),
    )

    images, descriptions = _critic_images(
        shot, m.ref, candidate_rel, focus_panels=focus_panels,
        motion_rel=motion_rel, motion_frames=motion_frames, prior_rel=prior_rel, prior_mean=prior_mean,
    )
    claims = tuple(claim for claim in getattr(getattr(active_unit, "evaluation", None), "claims", ())
                   if m.frame in claim.moments)
    claim_snapshot = critic_qualification.digest([asdict(claim) for claim in claims])

    def check_inputs():
        if critic_qualification.digest([asdict(claim) for claim in claims]) != claim_snapshot:
            raise ValueError("critic selected claims changed; prepare a new owned judgment")

    observed = await critic_session.execute(
        shot=shot, milestone=m, phase=review_mode,
        prompt="\n\n".join((CRITIC_SYSTEM, prompt, descriptions)), axes=axes,
        frames=tuple(sorted(focus_references)) or (m.frame,), images=images, allow_na=scope is None,
        selected_authority=selected_authority, execution_guard=execution_guard,
        claims=claims, check_inputs=check_inputs,
    )
    selected_qualified = {claim.id for claim in claims if claim.required
                          and claim.authority == "qualified_qualitative_required" and claim.qualification is not None}
    qualified_claims = (set(observed["qualified_claim_ids"]) & selected_qualified
                        if observed["qualification_verified"] is True else set())
    verdict = _verdict(observed["verdict"])
    verdict = _audit_panel_citations(verdict, focus_panels)
    verdict = _filter_critic_issues(
        verdict,
        evidence,
        claim_bindings=claim_bindings if scope is not None else None,
        qualified_claims=qualified_claims,
    )
    verdict = _apply_evidence_gate(verdict, evidence)
    verdict["evidence"] = evidence or []
    required = {claim.id for claim in claims if claim.required
                and claim.authority == "qualified_qualitative_required"}
    qualified_axes = {claim.axis for claim in claims if claim.id in qualified_claims}
    if not required or not required.issubset(qualified_claims) or not set(dict(axes)).issubset(qualified_axes):
        verdict["pass"] = False
        verdict["needs_human"] = True
        verdict["qualification_gap"] = (
            "This visual opinion lacks measured qualification for the complete selected scope; "
            "select and qualify its owning claims before autonomous acceptance or repair."
        )
        if verdict.get("decided_by") != "checks":
            verdict["advisory_issues"] = verdict.get("issues", [])
            verdict["issues"] = []
    verdict["native_observation"] = {key: observed[key] for key in ("report", "report_sha256")}
    # A PASS followed by six urgent "fix" bullets is internally inconsistent and was a
    # major source of misleading run logs. Preserve such notes as non-blocking polish for
    # audit, but never route them into a repair path or present them as contractual defects.
    if verdict.get("pass") and verdict.get("issues"):
        verdict["polish"] = list(verdict["issues"])
        verdict["issues"] = []
    if verdict.get("qualification_gap"):
        log(verdict["qualification_gap"], 1)
    if verdict.get("reference_unusable"):
        log(
            f"✗ critic says the REFERENCE is unusable for {m.id}: "
            f"{verdict.get('reference_note', '(no note)')} — fix {m.ref} in the plan; "
            f"no score is meaningful against it",
            1,
        )
    scores = ", ".join(f"{k}={v}" for k, v in verdict.get("scores", {}).items())
    na = verdict.get("na_axes") or []
    log(
        f"critic: {scores} | mean {verdict['mean']} (over {len(verdict.get('scored_axes', []))} "
        f"in-scope axes{f'; n/a: {len(na)}' if na else ''}) | "
        f"{'PASS ✅' if verdict['pass'] else 'REVISE ✎'}",
        1,
    )
    for issue in verdict.get("issues", [])[:6]:
        log(f"· fix: {issue}", 2)
    for item in verdict.get("contradicted_issues", [])[:6]:
        log(f"· discarded measurable claim: {item['issue']} ({item['reason']})", 2)
    for item in verdict.get("contract_gaps", [])[:6]:
        observation = item.get("observation", {})
        log(
            f"· contract gap: {observation.get('property')}: "
            f"{observation.get('observation')} ({item.get('reason')})",
            2,
        )
    for item in verdict.get("unverified_observations", [])[:6]:
        observation = item.get("observation", {})
        log(f"· unverified qualitative observation: {observation.get('observation')}", 2)
    if verdict.get("judge_conflict"):
        log("⚠ critic score has no evidence-backed blocking issue — judge conflict", 1)
    # The judge's answer is what every control decision downstream hangs on, and it was
    # the one output with no durable home: `_critique` drains its own stream and never
    # calls log_message, so nothing but this console line recorded WHICH image scored
    # what against which reference. Recorded with the inputs beside it, because a score
    # without its render/reference pair cannot be re-checked.
    transcript.event(
        "critic",
        milestone=m.id,
        frame=m.frame,
        candidate=candidate_rel,
        ref=m.ref,
        model=critic_model(),
        mean=verdict.get("mean"),
        verdict="pass" if verdict.get("pass") else "revise",
        decided_by=verdict.get("decided_by", "critic"),
        scores=verdict.get("scores", {}),
        scored_axes=verdict.get("scored_axes", []),
        na_axes=na,
        borderline=_borderline(verdict),
        reference_unusable=bool(verdict.get("reference_unusable")),
        issues=verdict.get("issues", []),
        contradicted_issues=verdict.get("contradicted_issues", []),
        observations=verdict.get("observations", []),
        observation_reconciliation=verdict.get("observation_reconciliation", []),
        contract_gaps=verdict.get("contract_gaps", []),
        contract_gap=bool(verdict.get("contract_gap")),
        unverified_observations=verdict.get("unverified_observations", []),
        protocol_errors=verdict.get("protocol_errors", []),
        invalid_panel_citations=verdict.get("invalid_panel_citations", []),
        judge_conflict=bool(verdict.get("judge_conflict")),
        evidence=evidence or [],
        focus_requests=verdict.get("focus_requests", []),
        focus_panels=[{k: v for k, v in panel.items() if k != "image_abs"} for panel in (focus_panels or [])],
        scope="layer" if scope else "full-rubric",
        review_mode=review_mode,
        native_observation=verdict["native_observation"],
        qualified_claim_ids=sorted(qualified_claims),
        qualification_gap=verdict.get("qualification_gap"),
        needs_human=bool(verdict.get("needs_human")),
        motion_strip=motion_rel,
    )
    return verdict


# How close to the pass line counts as "noise could flip this".
#
# MEASURED (N=12, same render vs same reference, layer-1 scope, one axis):
#   scores [2,4,3,2,3,3,3,3,3,3,4,3] → median 3.0, spread 2.0, sd 0.603,
#   and 2 of 12 draws flipped the verdict — a 17% flip rate on an unchanged image.
#
# A FIXED band was the wrong shape. Noise in the MEAN falls as 1/sqrt(n), so one constant
# is simultaneously too narrow on a 3-axis layer and wasteful on an 8-axis one. Two
# sigma of the mean at this sd: n=3 → 0.70, n=4 → 0.60, n=6 → 0.49, n=8 → 0.43. The old
# flat 0.4 was only defensible at n≈8, and most layers here are narrower than that.
#
# HISTORICAL MEASUREMENT; NOT FLYNN QUALIFICATION. Every number above was measured on FABLE-5. Judge
# noise is a property of the judge, so both the sd and the flip rate belong to a model
# that is no longer scoring anything here. Each native panel invocation must independently
# match its selected qualification; this heuristic supplies no credential. The band may now be too wide (paying for
# panels that were never in doubt) or too narrow (passing verdicts that a second opinion
# would have flipped) — and which of those it is, is not currently known.
# Re-measure before trusting the adjudication economics: python -m vfx_harness.evaluation.cli variance <shot>
_JUDGE_SD = 0.603  # fable-5 measurement; see above


def _adjudicate_band(n_scored: int) -> float:
    """2σ of the mean for this many axes, clamped to a sane range."""
    if n_scored <= 0:
        return 0.4
    return min(0.8, max(0.4, 2 * _JUDGE_SD / (n_scored**0.5)))


def _borderline(verdict: dict) -> bool:
    """Could judge noise flip this verdict?"""
    scores = [v for v in verdict.get("scores", {}).values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not scores:
        return False
    if len(scores) <= 2:
        # Only scores adjacent to the 2/3 decision boundary can flip the verdict with one
        # point of ordinary judge noise.  Treating a perfect 4/5 on one owned axis as
        # "borderline" doubled every canonical critic call without changing a decision.
        return any(score in (2, 3) for score in scores)
    return abs(verdict.get("mean", 0.0) - PASS_MEAN) <= _adjudicate_band(len(scores)) or min(scores) == PASS_MIN


def _needs_critic_panel(verdict: dict) -> bool:
    """Whether another subjective opinion could change the decision.

    An authoritative executable check is deterministic for this scene state.  Paying a
    second critic to look at the same pixels cannot turn that failed contract into a pass;
    it only duplicates cost and creates another transient-failure point.
    """
    return (
        not verdict.get("reference_unusable")
        and verdict.get("decided_by") != "checks"
        and not verdict.get("contract_gap")
        and not verdict.get("needs_human")
        and not verdict.get("protocol_errors")
        and _borderline(verdict)
    )


def _round_rank(verdict: dict | None) -> tuple[bool, float]:
    """Rank a round by validity first, aesthetic score second.

    The evidence gate can force REVISE while retaining the critic's visual mean.  A
    contract-failing 4.0 must never beat a contract-passing 4.0 merely because it was
    encountered first.
    """
    verdict = verdict or {}
    return bool(verdict.get("pass")), float(verdict.get("mean", -1.0))


def _aggregate_critic_panel(panel: list[dict]) -> dict:
    """Aggregate noisy scores without voting away an actionable observation.

    A passing opinion contains no blocking observation by protocol, so it cannot refute
    a dissenting judge's exact qualified observation.  Executable reconciliation may
    contradict that observation before this boundary; an uncontradicted actionable row
    remains a failure even when bare pass votes are the majority.
    """
    votes = [bool(item.get("pass")) for item in panel]
    means = sorted(float(item.get("mean", 0.0)) for item in panel)
    majority_pass = sum(votes) > len(votes) / 2
    out = dict(panel[0])
    out["pass"] = majority_pass
    out["mean"] = means[len(means) // 2]
    out["panel"] = [
        {"mean": item.get("mean"), "pass": bool(item.get("pass"))}
        for item in panel
    ]
    agreeing = [item for item in panel if bool(item.get("pass")) == majority_pass]
    chosen = agreeing[0] if agreeing else panel[0]
    for field, default in (
        ("issues", []),
        ("scores", {}),
        ("observations", []),
        ("observation_reconciliation", []),
        ("contradicted_issues", []),
        ("contract_gaps", []),
        ("unverified_observations", []),
        ("protocol_errors", []),
    ):
        out[field] = chosen.get(field, default)
    out["contract_gap"] = bool(chosen.get("contract_gap"))
    out["judge_conflict"] = bool(
        not majority_pass
        and agreeing
        and all(item.get("judge_conflict") for item in agreeing)
    )

    actionable_dissent = [
        item
        for item in panel
        if not item.get("pass")
        and item.get("issues")
        and not item.get("contract_gap")
        and not item.get("judge_conflict")
        and not item.get("protocol_errors")
    ]
    if majority_pass and actionable_dissent:
        dissent = actionable_dissent[0]
        out["pass"] = False
        out["issues"] = list(dissent.get("issues") or [])
        out["scores"] = dict(dissent.get("scores") or {})
        out["observations"] = list(dissent.get("observations") or [])
        out["observation_reconciliation"] = list(
            dissent.get("observation_reconciliation") or []
        )
        out["contradicted_issues"] = list(dissent.get("contradicted_issues") or [])
        out["contract_gaps"] = list(dissent.get("contract_gaps") or [])
        out["contract_gap"] = bool(dissent.get("contract_gap"))
        out["unverified_observations"] = list(
            dissent.get("unverified_observations") or []
        )
        out["protocol_errors"] = list(dissent.get("protocol_errors") or [])
        out["judge_conflict"] = False
        out["decided_by"] = "actionable_panel_dissent"
        out["actionable_dissent"] = {
            "mean": dissent.get("mean"),
            "issues": list(dissent.get("issues") or []),
        }
    return out


def _decided(verdict: dict, how: str = "critic") -> dict:
    """Stamp how this verdict was decided, at the one place it is produced.

    Every branch that ends the judgement early sets its own value -- checks,
    no_optical_signal, actionable_panel_dissent -- and the ordinary passing critic
    verdict set nothing. One consumer defaulted it to "critic" and the layer evaluation
    receipt required it, so the first composed critic row that ever PASSED reached mint
    with an empty field and killed the layer (HIR-0211).
    """
    if not str(verdict.get("decided_by") or "").strip():
        verdict["decided_by"] = how
    return verdict


async def _judge(
    shot: Shot,
    m: Milestone,
    candidate_rel: str,
    axes: list[tuple[str, str]],
    session: BlenderSession,
    verbose: bool,
    scope: str | None = None,
    **kw,
) -> dict:
    """Score the frame, buying extra opinions ONLY where the decision is uncertain.

    One critic call decided every layer until now. That is fine when a verdict is far
    from the line and indefensible when it is near it: the identical render/reference
    pair scored 4.0, 3.0, 3.0, 2.0 on repeats, so a single 3 was deciding whether the
    whole chain proceeded. Here a borderline verdict goes to best-of-three on the
    pass/fail question, which is where the noise actually hurts.
    """
    critic_kw = dict(kw)
    critic_kw.pop("review_mode", None)
    critic_kw.pop("focus_panels", None)
    motion_evidence = critic_kw.pop("motion_evidence", None)
    allow_motion = critic_kw.pop("allow_motion", None)
    motion_frames_override = critic_kw.pop("motion_frames_override", None)
    focus_frames_override = critic_kw.pop("focus_frames_override", None)
    active_unit = critic_kw.pop("active_unit", None)
    selected_authority = critic_kw.get("selected_authority")
    if selected_authority is None:
        selected_authority = authority_selection.resolve_selected_authority(
            shot.folder
        )
        critic_kw["selected_authority"] = selected_authority
    if (
        motion_evidence is None
        and shot.frontmatter.get("type") == "motion"
        and shot.frames > 1
        and (_axes_need_motion(axes) if allow_motion is None else allow_motion)
    ):
        stem = candidate_rel.split("/")[-1].split(".")[0]
        motion_evidence = builder_package()._stash_motion_strip(
            session, shot, m, stem, frames_override=motion_frames_override
        )
    focus_references = _focus_references(
        shot,
        m,
        focus_frames_override,
        selected_authority=selected_authority,
    )
    required = _required_focus_requests(
        shot,
        str(m.id).split("@", 1)[0],
        int(m.frame),
        axes,
        selected_authority=selected_authority,
    )
    signal = _image_optical_signal(shot.folder / candidate_rel)
    if signal is not None and not signal["has_signal"]:
        log(
            f"candidate {candidate_rel} has no optical signal "
            f"(stddev={signal['stddev']}, edges={signal['edge_mean']}, "
            f"range={signal['dynamic_range']}) — not calling the critic",
            1,
        )
        scored_axes = [key for key, _description in axes]
        return {
            "scores": dict.fromkeys(scored_axes, 1),
            "mean": 1.0,
            "pass": False,
            "issues": [
                "candidate plate has no optical signal (black/empty). A look score on "
                "this frame is not a judgment — light and surface this unit in a unit "
                "that declares look_capabilities, or keep the critic off executable-only units"
            ],
            "scored_axes": scored_axes,
            "na_axes": [],
            "observations": [],
            "decided_by": "no_optical_signal",
            "judge_conflict": False,
            "contract_gap": False,
            "signal": signal,
        }
    focus_panels = []
    if required:
        log(f"contract requires {len(required)} aligned focus panel(s) before judgment", 1)
        focus_panels = await _make_focus_panels(shot, m, session, required, candidate_rel=candidate_rel)
        transcript.event(
            "contract_focus",
            milestone=m.id,
            frame=m.frame,
            candidate=candidate_rel,
            reference=m.ref,
            requests=required,
            panels=focus_panels,
        )
    first = await _critique(
        shot,
        m,
        candidate_rel,
        axes,
        session,
        verbose,
        scope,
        review_mode="observer",
        focus_panels=focus_panels or None,
        motion_evidence=motion_evidence,
        allow_motion=allow_motion,
        focus_frames=focus_frames_override,
        active_unit=active_unit,
        **critic_kw,
    )
    requests = (
        _focus_requests(
            first,
            axes,
            focus_references=focus_references,
            motion_frames=(motion_evidence[1] if motion_evidence else None),
        )
        if not first.get("reference_unusable") and first.get("decided_by") != "checks"
        else []
    )
    if requests and not focus_panels:
        log(f"critic requested {len(requests)} aligned focus panel(s) — rendering optical crops before deciding", 1)
        focus_panels = await _make_focus_panels(shot, m, session, requests, candidate_rel=candidate_rel)
        transcript.event(
            "critic_focus",
            milestone=m.id,
            frame=m.frame,
            candidate=candidate_rel,
            reference=m.ref,
            requests=requests,
            panels=focus_panels,
        )
        focused = await _critique(
            shot,
            m,
            candidate_rel,
            axes,
            session,
            verbose,
            scope,
            review_mode="focus_review",
            focus_panels=focus_panels,
            motion_evidence=motion_evidence,
            allow_motion=allow_motion,
            focus_frames=focus_frames_override,
            active_unit=active_unit,
            **critic_kw,
        )
        focused["focus_requested"] = requests
        focused["focus_panels"] = focus_panels
        first = focused
    if not _needs_critic_panel(first):
        return _decided(first)
    log(
        f"borderline verdict (mean {first['mean']}, "
        f"{len(first.get('scored_axes', []))} axis/axes) — seeking a second opinion",
        1,
    )
    panel = [first]
    for _extra in range(2, 4):
        mode = "evidence_audit" if _extra == 2 else "tie_breaker"
        v = await _critique(
            shot,
            m,
            candidate_rel,
            axes,
            session,
            verbose,
            scope,
            review_mode=mode,
            focus_panels=focus_panels or None,
            motion_evidence=motion_evidence,
            allow_motion=allow_motion,
            focus_frames=focus_frames_override,
            active_unit=active_unit,
            **critic_kw,
        )
        panel.append(v)
        votes = [p["pass"] for p in panel]
        if len(panel) == 2 and votes[0] == votes[1]:
            break  # unanimous; a third cannot change it
        if len(panel) == 3:
            break
    out = _aggregate_critic_panel(panel)
    votes = [p["pass"] for p in panel]
    majority_pass = sum(votes) > len(votes) / 2
    actionable_dissent = bool(out.get("actionable_dissent"))
    panel_result = (
        "REVISE ✎ (actionable dissent preserved)"
        if actionable_dissent
        else "PASS ✅" if majority_pass else "REVISE ✎"
    )
    log(
        f"panel of {len(panel)}: means {[p['mean'] for p in panel]} · "
        f"votes {['PASS' if v else 'REVISE' for v in votes]} → "
        f"{panel_result} (median {out['mean']})",
        1,
    )
    return _decided(out)
