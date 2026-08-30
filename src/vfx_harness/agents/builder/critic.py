"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

from claude_agent_sdk import (
    query,
)

from vfx_harness.agents.build_prompts import (
    critic_prompt,
)
from vfx_harness.agents.builder.axes import _axes_need_motion, _critic_options
from vfx_harness.agents.builder.critic_focus import (
    _apply_evidence_gate,
    _audit_panel_citations,
    _claim_context,
    _filter_critic_issues,
    _focus_references,
    _focus_requests,
    _image_block,
    _image_optical_signal,
    _make_focus_panels,
    _one_user_message,
    _required_focus_requests,
    _structured_or_text,
)
from vfx_harness.agents.builder.drain import _extract_json, _verdict
from vfx_harness.agents.builder.models import PASS_MEAN, PASS_MIN, BuildTruncated, critic_model
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.application.preflight import model_phase_failure
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.observability import costlog, transcript
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.orchestration.ledger import Milestone


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
) -> dict:
    motion_rel, motion_frames = motion_evidence or (None, None)
    wants_motion = _axes_need_motion(axes) if allow_motion is None else allow_motion
    if motion_rel is None and shot.frontmatter.get("type") == "motion" and shot.frames > 1 and wants_motion:
        try:  # a motion strip so motion/finish axes are judged across frames, not a still
            stem = candidate_rel.split("/")[-1].split(".")[0]
            motion_rel, motion_frames = builder_package()._stash_motion_strip(session, shot, m, stem)
        except Exception as e:
            log(f"motion strip skipped: {str(e)[:80]}", 1)
    focus_references = _focus_references(shot, m, focus_frames)
    claim_manifest, claim_bindings, qualified_claims = _claim_context(
        shot, m, enabled=scope is not None, active_unit=active_unit
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
        focus_frames=sorted(focus_references),
    )

    # ATTACH the images instead of asking an agent to fetch them. A missing file is now a
    # loud failure here rather than a confident score on a frame that was never seen.
    ref_abs, cand_abs = shot.folder / m.ref, shot.folder / candidate_rel
    for p, what in ((ref_abs, "reference"), (cand_abs, "candidate render")):
        if not p.is_file():
            raise BlenderError(f"critic cannot score {m.id}: {what} missing at {p}")
    blocks = [
        {"type": "text", "text": prompt},
        {"type": "text", "text": "FIRST — the REFERENCE:"},
        _image_block(ref_abs),
        {"type": "text", "text": "SECOND — the CANDIDATE render:"},
        _image_block(cand_abs),
    ]
    for panel in (focus_panels or [])[:2]:
        panel_abs = shot.folder / panel["image_rel"]
        if not panel_abs.is_file():
            raise BlenderError(f"critic focus panel missing at {panel_abs}")
        blocks += [
            {
                "type": "text",
                "text": (
                    f"FOCUS PANEL {panel['id']} — axis {panel['axis']}, crop "
                    f"{panel['crop']} within source frame f{panel['source_frame']} "
                    f"against {panel['reference']} (TOP-LEFT normalized), optical res_pct "
                    f"{panel['res_pct']}. It contains aligned CANDIDATE | REFERENCE "
                    f"and a 50/50 wipe. Reason: {panel['reason']}"
                ),
            },
            _image_block(panel_abs),
        ]
    if motion_rel and (shot.folder / motion_rel).is_file():
        blocks += [
            {"type": "text", "text": f"THIRD — the MOTION STRIP, frames {motion_frames}:"},
            _image_block(shot.folder / motion_rel),
        ]
    # The previous best, so the critic can judge DIRECTION of travel and not only
    # absolute state. Explicitly framed as context: it must score the candidate.
    if prior_rel and (shot.folder / prior_rel).is_file():
        blocks += [
            {
                "type": "text",
                "text": f"CONTEXT ONLY — the best PREVIOUS attempt at this frame, "
                f"which scored {prior_mean}. Do NOT score this image. Use it "
                f"to say whether the candidate improved or regressed, and "
                f"record as a typed observation anything the previous attempt got right "
                f"that the candidate has lost:",
            },
            _image_block(shot.folder / prior_rel),
        ]

    # Still retried: the critic is a transient-failure choke point — an SDK hiccup here
    # once killed a layer AFTER it had passed at 4.0 and written its script. Scoring is
    # idempotent. What is gone is retrying because the critic never opened its images.
    acc: dict = {}
    for attempt in range(1, 4):
        acc = {}
        try:
            critic_role = "focus_critic" if review_mode == "focus_review" else "critic"
            with costlog.scoped(
                role=critic_role,
                phase=review_mode,
                frame=getattr(m, "frame", None),
                model=critic_model(),
            ):
                async for message in query(
                    prompt=_one_user_message(blocks),
                    options=_critic_options(
                        shot,
                        axes,
                        allow_na=scope is None,
                        focus_frames=sorted(focus_references),
                    ),
                ):
                    _structured_or_text(message, acc)
                    # The critic loop does NOT call log_message, which is where costlog was
                    # hooked — so record at the source while the scoped role is active.
                    costlog.record(message)
                    if isinstance(message, builder_package().ResultMessage):
                        phase_failure = model_phase_failure(
                            {
                                "subtype": getattr(message, "subtype", "unknown"),
                                "turns": getattr(message, "num_turns", 0) or 0,
                                "cost": getattr(message, "total_cost_usd", None) or 0.0,
                                "is_error": bool(getattr(message, "is_error", False)),
                                "api_error_status": getattr(
                                    message, "api_error_status", None
                                ),
                            },
                            0,
                        )
                        if phase_failure:
                            transcript.event(
                                "model_phase_failure",
                                phase=review_mode,
                                why=phase_failure,
                            )
                            raise BuildTruncated(
                                f"critic {review_mode}: {phase_failure}",
                                terminal_cause="model_session_failure",
                            )
            if acc.get("structured") or acc.get("text", "").strip():
                break
            log(f"critic returned nothing (attempt {attempt}/3) — retrying", 1)
        except BuildTruncated:
            raise
        except Exception as e:
            if attempt == 3:
                raise
            log(f"critic error (attempt {attempt}/3): {str(e)[:90]} — retrying", 1)
    if not (acc.get("structured") or acc.get("text", "").strip()):
        raise BlenderError(f"critic returned no verdict for {m.id} after 3 attempts")
    verdict = _verdict(acc.get("structured") or _extract_json(acc["text"]))
    verdict = _audit_panel_citations(verdict, focus_panels)
    verdict = _filter_critic_issues(
        verdict,
        evidence,
        claim_bindings=claim_bindings if scope is not None else None,
        qualified_claims=qualified_claims,
    )
    verdict = _apply_evidence_gate(verdict, evidence)
    verdict["evidence"] = evidence or []
    # A PASS followed by six urgent "fix" bullets is internally inconsistent and was a
    # major source of misleading run logs. Preserve such notes as non-blocking polish for
    # audit, but never route them into a repair path or present them as contractual defects.
    if verdict.get("pass") and verdict.get("issues"):
        verdict["polish"] = list(verdict["issues"])
        verdict["issues"] = []
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
# STALE AS OF THE SWITCH TO OPUS-5. Every number above was measured on FABLE-5. Judge
# noise is a property of the judge, so both the sd and the flip rate belong to a model
# that is no longer scoring anything here. The band may now be too wide (paying for
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
    if (
        motion_evidence is None
        and shot.frontmatter.get("type") == "motion"
        and shot.frames > 1
        and (_axes_need_motion(axes) if allow_motion is None else allow_motion)
    ):
        try:
            stem = candidate_rel.split("/")[-1].split(".")[0]
            motion_evidence = builder_package()._stash_motion_strip(
                session, shot, m, stem, frames_override=motion_frames_override
            )
        except Exception as exc:
            log(f"motion strip skipped: {str(exc)[:80]}", 1)
    focus_references = _focus_references(shot, m, focus_frames_override)
    required = _required_focus_requests(shot, str(m.id).split("@", 1)[0], int(m.frame), axes)
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
        try:
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
        except Exception as exc:
            first["focus_error"] = str(exc)[:200]
            log(f"! focus panel review unavailable: {str(exc)[:120]} — retaining the full-frame verdict", 1)
    if not _needs_critic_panel(first):
        return first
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
    return out
