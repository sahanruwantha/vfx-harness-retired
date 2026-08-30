"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

from pathlib import Path

import anyio

from vfx_harness.agents.builder.axes import _layer_needs_motion
from vfx_harness.agents.builder.drain import _verdict
from vfx_harness.agents.builder.evidence import (
    _image_reproduction,
    _unit_raster_mode,
    _unit_requires_raster,
)
from vfx_harness.agents.builder.falsify import _persist_contract_gaps
from vfx_harness.agents.builder.models import _RESET
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import _run_artifact_script
from vfx_harness.agents.builder.revalidate import _scope_added_object_errors
from vfx_harness.agents.builder.verdicts import _judge_unit_or_layer, _layer_motion_frames, _stash_motion_strip
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.orchestration.ledger import Ledger, Milestone, plan_strips


async def _verify_script(
    shot: Shot,
    m: Milestone,
    script_rel: str,
    prior_paths: list[Path],
    session: BlenderSession,
    axes: list[tuple[str, str]],
    ledger: Ledger,
    verbose: bool,
    live_best_mean: float | None = None,
    live_best_render: str | None = None,
    live_best_verdict: dict | None = None,
    scope: str | None = None,
    layer=None,
    active_unit=None,
    out_verdicts: list | None = None,
) -> str:
    """-> passed | reproduced | contract_gap | judge_conflict | failed.

    Scores EVERY frame the layer answers for, not just the primary. Iteration renders one
    frame for speed; the deliverable has to hold at all of them, and a layer is only as
    good as its worst claimed frame.
    """
    script_path = shot.folder / script_rel
    if not script_path.is_file():
        log(f"! builder never wrote {script_rel}")
        return "failed"
    log(f"verifying {script_path.name} reproduces from an empty scene…")
    try:
        session.run(_RESET)
        session.run(builder_package()._preamble(shot))
        builder_package()._run_prior_paths(session, prior_paths)  # deltas assume priors ran first
        before_objects = builder_package()._scene_object_manifest(session)
        _run_artifact_script(session, script_path)
        if active_unit is not None and active_unit.mutates.mode == "scoped":
            scope_errors = _scope_added_object_errors(
                before_objects,
                builder_package()._scene_object_manifest(session),
                active_unit.mutates.roles,
            )
            if scope_errors:
                log("! scoped artifact violation: " + "; ".join(scope_errors[:6]))
                verdict = _verdict({"scores": {}, "issues": scope_errors})
                ledger.record_round(m, kind="canonical", index=0, render="", verdict=verdict)
                # A scope violation is the MOST repairable canonical failure — its text
                # names the offending objects and the allowed roles. Returning with no
                # per-frame verdicts starved the repair loop (`failed` stayed empty) and
                # runs 20260825T000404Z/022805Z each burned a manual `vfx units retry`
                # on one-line role-tag fixes. Publish the violation AS the failing
                # verdict for every judged frame so the repair loop engages.
                if out_verdicts is not None:
                    for frame, ref in (list(layer.judges) if layer is not None else [(m.frame, m.ref)]):
                        out_verdicts.append(((frame, ref), {
                            **verdict,
                            "mean": 1.0,
                            "pass": False,
                            "issues": list(scope_errors),
                        }))
                return "failed"
    except BlenderError as e:
        log(f"! build script failed: {str(e)[:200]}")
        verdict = _verdict({"scores": {}, "issues": [f"script error: {e}"]})
        ledger.record_round(m, kind="canonical", index=0, render="", verdict=verdict)
        # a script that cannot execute is equally repairable — feed the loop the error
        if out_verdicts is not None:
            for frame, ref in (list(layer.judges) if layer is not None else [(m.frame, m.ref)]):
                out_verdicts.append(((frame, ref), {
                    **verdict,
                    "mean": 1.0,
                    "pass": False,
                    "issues": [f"script error: {e}"],
                }))
        return "failed"
    judges = list(layer.judges) if layer is not None else [(m.frame, m.ref)]
    # Render serially (one Blender session), then score CONCURRENTLY — the critic calls
    # are independent judgements of already-written PNGs, and multi-frame judging tripled
    # the pass count on server_to_hansa (8 -> 18).
    raster_required = _unit_requires_raster(shot, active_unit)
    if not raster_required:
        log(
            "canonical replay owes only executable scene/interface evidence — "
            "skipping raster and visual critic",
            1,
        )
    shots_ = []
    for frame, ref in judges:
        if len(judges) == 1:
            m_i = m
        else:
            unit_tag = str(m.id).split("@", 1)[1] if "@" in str(m.id) and "@f" not in str(m.id) else None
            m_i = (
                Milestone(
                    f"{layer.id}@{unit_tag}",
                    frame,
                    ref,
                    m.reads,
                    plan_strips(shot).get(frame, ()),
                )
                if unit_tag
                else layer.milestone_at(frame, ref, plan_strips(shot))
            )
        render_rel = (
            builder_package()._stash_render(
                session,
                shot,
                m_i,
                f"canonical_f{frame}" if len(judges) > 1 else "canonical",
                mode=_unit_raster_mode(active_unit),
            )
            if raster_required
            else ""
        )
        shots_.append((frame, ref, m_i, render_rel))

    canonical_motion_evidence = None
    if (
        raster_required
        and shot.frontmatter.get("type") == "motion"
        and shot.frames > 1
        and _layer_needs_motion(layer)
    ):
        try:
            canonical_motion_evidence = _stash_motion_strip(
                session,
                shot,
                m,
                f"{m.id}_canonical",
                frames_override=_layer_motion_frames(layer, m, shot.frames),
            )
        except Exception as exc:
            log(f"canonical motion strip skipped: {str(exc)[:80]}", 1)

    # A one-frame layer that already passed live has one reproduction question: did its
    # script rebuild those accepted pixels? Answer that with pixels, not another aesthetic
    # vote. Multi-frame layers still need their additional claimed frames judged because
    # live iteration only rendered the primary one.
    if len(shots_) == 1 and live_best_render and live_best_verdict:
        frame, ref, m_i, render_rel = shots_[0]
        reproduction = _image_reproduction(shot.folder / live_best_render, shot.folder / render_rel)
        if reproduction.get("match"):
            evidence = builder_package()._render_evidence(
                shot, layer, m_i, render_rel, session, active_unit=active_unit
            )
            blocking = [item for item in evidence if item.get("authoritative") and not item.get("pass")]
            if blocking:
                log(
                    f"canonical reproduces live pixels, but {len(blocking)} authoritative "
                    f"check(s) fail — quality remains undecided",
                    1,
                )
            elif live_best_verdict.get("pass") or live_best_verdict.get("contract_gap"):
                is_gap = bool(live_best_verdict.get("contract_gap"))
                verdict = {
                    "scores": dict(live_best_verdict.get("scores") or {}),
                    "mean": live_best_verdict.get("mean", live_best_mean or 0.0),
                    "pass": not is_gap,
                    "issues": [],
                    "scored_axes": list(live_best_verdict.get("scored_axes") or []),
                    "na_axes": list(live_best_verdict.get("na_axes") or []),
                    "decided_by": "pixel_reproduction",
                    "reproduction": reproduction,
                    "evidence": evidence,
                    "contract_gap": is_gap,
                    "contract_gaps": list(live_best_verdict.get("contract_gaps") or []),
                    "observation_reconciliation": list(
                        live_best_verdict.get("observation_reconciliation") or []
                    ),
                }
                ledger.record_round(m, kind="canonical", index=0, render=render_rel, verdict=verdict)
                wrapped = [((frame, ref), verdict)]
                if out_verdicts is not None:
                    out_verdicts.extend(wrapped)
                log(
                    f"canonical pixels reproduce {'contract-gap' if is_gap else 'accepted'} live render "
                    f"(MAE {reproduction['mae']}, p99 {reproduction['p99']}, "
                    f">4 delta {reproduction['changed_gt4']:.2%}) — no second quality vote",
                    1,
                )
                if is_gap:
                    _persist_contract_gaps(
                        shot,
                        layer,
                        m_i,
                        render_rel,
                        verdict,
                        mode=_unit_raster_mode(active_unit),
                    )
                    return "contract_gap"
                return "reproduced"
    results: list = [None] * len(shots_)

    async def _score(i, m_i, render_rel):
        evidence = builder_package()._render_evidence(
            shot, layer, m_i, render_rel, session, active_unit=active_unit
        )
        results[i] = await _judge_unit_or_layer(
            shot,
            m_i,
            render_rel,
            axes,
            session,
            verbose,
            scope,
            evidence=evidence,
            active_unit=active_unit,
            motion_evidence=canonical_motion_evidence,
            allow_motion=_layer_needs_motion(layer),
            focus_frames_override=[int(m_i.frame)],
            layer=layer,
        )

    if not raster_required:
        for i, (_f, _r, m_i, rr) in enumerate(shots_):
            await _score(i, m_i, rr)
    elif len(shots_) == 1:
        await _score(0, shots_[0][2], shots_[0][3])
    else:
        async with anyio.create_task_group() as tg:
            for i, (_f, _r, m_i, rr) in enumerate(shots_):
                tg.start_soon(_score, i, m_i, rr)
    verdicts = []
    for i, (frame, ref, _m_i, render_rel) in enumerate(shots_):
        v = results[i]
        _persist_contract_gaps(
            shot,
            layer,
            _m_i,
            render_rel,
            v,
            mode=_unit_raster_mode(active_unit),
        )
        ledger.record_round(m, kind="canonical", index=i, render=render_rel, verdict=v)
        verdicts.append(((frame, ref), v))
    if out_verdicts is not None:
        out_verdicts.extend(verdicts)  # the run report needs the per-frame results
    if len(judges) > 1:
        log(
            "canonical per-frame: "
            + " · ".join(f"f{f}:{v['mean']}{'✅' if v['pass'] else '✗'}" for (f, _), v in verdicts),
            1,
        )
    # weakest claimed frame decides — that is the entire point of listing them
    (worst_frame, _), verdict = min(verdicts, key=lambda kv: kv[1]["mean"])
    if all(v["pass"] for _, v in verdicts):
        log(f"canonical clears every claimed frame (worst f{worst_frame} {verdict['mean']})", 1)
        return "passed"
    failures = [v for _, v in verdicts if not v.get("pass")]
    if failures and all(v.get("contract_gap") and not v.get("issues") for v in failures):
        log(
            "canonical candidate has uncovered measurable defects — recording "
            "CONTRACT_GAP for transactional replanning",
            1,
        )
        return "contract_gap"
    if failures and all(v.get("judge_conflict") for v in failures):
        log("canonical checks and critic disagree with no evidence-backed repair — recording JUDGE_CONFLICT", 1)
        return "judge_conflict"
    return "failed"
