"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from vfx_harness.agents.builder.axes import _layer_needs_motion
from vfx_harness.agents.builder.drain import _verdict
from vfx_harness.agents.builder.evidence import (
    _image_reproduction,
    _unit_raster_mode,
    _unit_requires_raster,
)
from vfx_harness.agents.builder.execution_guard import ExecutionAuthorityLost, ExecutionGuard
from vfx_harness.agents.builder.falsify import _persist_contract_gaps
from vfx_harness.agents.builder.judgment_payment import (
    JudgmentDebtPayment,
    PreparedJudgmentObservation,
)
from vfx_harness.agents.builder.models import _RESET
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import (
    _prepare_artifact_replay_inputs,
    _run_artifact_script,
)
from vfx_harness.agents.builder.revalidate import _scope_added_object_errors
from vfx_harness.agents.builder.verdicts import _judge_unit_or_layer, _layer_motion_frames, _stash_motion_strip
from vfx_harness.blender.session import BlenderError, BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.orchestration.ledger import Ledger, Milestone, plan_strips
from vfx_harness.orchestration.unit_evaluation_receipts import ExecutedReplayInput

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority


def _bind_canonical_evidence(
    verdict: dict,
    *,
    raster_required: bool,
    render_rel: str,
    render_receipt: dict | None,
) -> dict:
    """Attach the producer-owned canonical discriminant and exact raster receipt."""

    if raster_required:
        if not render_rel or not isinstance(render_receipt, dict):
            raise ValueError("render canonical is missing its produced locator/receipt")
        verdict["evidence_kind"] = "render"
        verdict["render"] = render_rel
        verdict["render_capture"] = render_receipt
    else:
        verdict["evidence_kind"] = "executable_only"
        verdict.pop("render", None)
        verdict.pop("render_capture", None)
    return verdict


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
    out_replay_inputs: list[ExecutedReplayInput] | None = None,
    authority_script_rel: str | None = None,
    on_replay_ready: Callable[[tuple[ExecutedReplayInput, ...]], Any] | None = None,
    on_observation_ready: Callable[
        [tuple[ExecutedReplayInput, ...], tuple[dict, ...], Any],
        tuple[Any, JudgmentDebtPayment | None],
    ]
    | None = None,
    on_replay_failed: Callable[
        [tuple[ExecutedReplayInput, ...], str, str, Any],
        Any,
    ]
    | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    execution_guard: ExecutionGuard | None = None,
    canonical_namespace: str | None = None,
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
    judgment_payment = None
    layer_replay_receipt = None
    replay_context = None
    prepared_replay = None
    replay_stage = "reset"
    try:
        prepared_replay = _prepare_artifact_replay_inputs(
            shot.folder,
            [
                (
                    path.expanduser()
                    .absolute()
                    .relative_to(shot.folder.expanduser().absolute())
                    .as_posix(),
                    path,
                )
                for path in prior_paths
            ]
            + [(authority_script_rel or script_rel, script_path)],
        )
        if out_replay_inputs is not None:
            out_replay_inputs.clear()
            out_replay_inputs.extend(item.executed for item in prepared_replay)
        session.run(_RESET)
        replay_stage = "preamble"
        session.run(builder_package()._preamble(shot))
        replay_stage = "predecessor_replay"
        builder_package()._run_prior_paths(
            session,
            prior_paths,
            prepared_replay[:-1],
        )  # deltas assume priors ran first
        before_objects = builder_package()._scene_object_manifest(session)
        replay_stage = "payer_replay"
        _run_artifact_script(session, script_path, prepared_replay[-1])
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
                if on_replay_failed is not None:
                    replay_inputs = tuple(item.executed for item in prepared_replay)
                    failed_context = (
                        None
                        if on_replay_ready is None
                        else on_replay_ready(replay_inputs)
                    )
                    on_replay_failed(
                        replay_inputs,
                        "scope_validation",
                        "; ".join(scope_errors),
                        failed_context,
                    )
                    return "failed"
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
        if on_replay_ready is not None:
            # Debt activation is a claim about the actual cumulative replay, not merely
            # selected plan rows. Invoke the harness callback only after every prior and
            # the current artifact executed and the scoped-mutation check stayed clean,
            # but before any raster or critic work can spend against that prefix.
            replay_context = on_replay_ready(
                tuple(item.executed for item in prepared_replay)
            )
    except BlenderError as e:
        log(f"! build script failed: {str(e)[:200]}")
        verdict = _verdict({"scores": {}, "issues": [f"script error: {e}"]})
        ledger.record_round(m, kind="canonical", index=0, render="", verdict=verdict)
        if on_replay_failed is not None and prepared_replay is not None:
            replay_inputs = tuple(item.executed for item in prepared_replay)
            failed_context = (
                None if on_replay_ready is None else on_replay_ready(replay_inputs)
            )
            on_replay_failed(
                replay_inputs,
                replay_stage,
                str(e),
                failed_context,
            )
            return "failed"
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
    raster_required = _unit_requires_raster(
        shot,
        active_unit,
        selected_authority=selected_authority,
    )
    if not raster_required:
        log(
            "canonical replay owes only executable scene/interface evidence — "
            "skipping raster and visual critic",
            1,
        )
    shots_: list[
        tuple[
            int,
            str,
            Milestone,
            str,
            dict | None,
        ]
    ] = []
    render_mode = _unit_raster_mode(active_unit)
    render_scale = 0.5
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
                    plan_strips(shot, selected_authority).get(frame, ()),
                )
                if unit_tag
                else layer.milestone_at(
                    frame,
                    ref,
                    plan_strips(shot, selected_authority),
                )
            )
        render_rel = ""
        render_receipt = None
        if raster_required:
            canonical_tag = (
                "canonical"
                if canonical_namespace is None
                else f"{canonical_namespace}_canonical"
            )
            render_rel, render_receipt = builder_package()._stash_render_with_receipt(
                session,
                shot,
                m_i,
                (
                    f"{canonical_tag}_f{frame}"
                    if len(judges) > 1
                    else canonical_tag
                ),
                scale=render_scale,
                mode=render_mode,
            )
        shots_.append((frame, ref, m_i, render_rel, render_receipt))

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
                (
                    f"{m.id}_canonical"
                    if canonical_namespace is None
                    else f"{m.id}_{canonical_namespace}_canonical"
                ),
                frames_override=_layer_motion_frames(layer, m, shot.frames),
            )
        except ExecutionAuthorityLost:
            raise
        except Exception as exc:
            log(f"canonical motion strip skipped: {str(exc)[:80]}", 1)

    evidence_by_index = tuple(
        builder_package()._render_evidence(
            shot,
            layer,
            m_i,
            render_rel,
            session,
            active_unit=active_unit,
            selected_authority=selected_authority,
        )
        for _frame, _ref, m_i, render_rel, _render_receipt in shots_
    )
    prepared_observations: list[PreparedJudgmentObservation | None] = [
        None
    ] * len(shots_)
    if on_observation_ready is not None:
        layer_replay_receipt, judgment_payment = on_observation_ready(
            tuple(item.executed for item in prepared_replay),
            tuple(
                {
                    "frame": int(frame),
                    "ref": str(ref),
                    "render": render_rel or None,
                    "render_capture": render_receipt,
                    "evidence": evidence_by_index[index],
                    "motion_evidence": canonical_motion_evidence,
                }
                for index, (frame, ref, _m_i, render_rel, render_receipt) in enumerate(
                    shots_
                )
            ),
            replay_context,
        )
        if layer_replay_receipt is None:
            raise ValueError("layer finalization observation produced no replay receipt")
        if judgment_payment is not None:
            prepared_observations = [
                judgment_payment.prepare(
                    frame=int(frame),
                    ref=str(ref),
                    render_mode=render_mode,
                    render_scale=render_scale,
                )
                for frame, ref, _m_i, _render_rel, _render_receipt in shots_
            ]

    # A one-frame layer that already passed live has one reproduction question: did its
    # script rebuild those accepted pixels? Answer that with pixels, not another aesthetic
    # vote. Multi-frame layers still need their additional claimed frames judged because
    # live iteration only rendered the primary one.
    if len(shots_) == 1 and live_best_render and live_best_verdict:
        frame, ref, m_i, render_rel, render_receipt = shots_[0]
        reproduction = _image_reproduction(shot.folder / live_best_render, shot.folder / render_rel)
        if reproduction.get("match"):
            evidence = evidence_by_index[0]
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
                _bind_canonical_evidence(
                    verdict,
                    raster_required=True,
                    render_rel=render_rel,
                    render_receipt=render_receipt,
                )
                if layer_replay_receipt is not None:
                    sealed_point = layer_replay_receipt.observation.points[0]
                    verdict["evidence"] = list(sealed_point.evidence)
                    verdict["evidence_failures"] = [
                        row
                        for row in sealed_point.evidence
                        if row.get("authoritative") is True
                        and row.get("pass") is False
                    ]
                    verdict["missing_evidence"] = list(
                        sealed_point.missing_evidence_ids
                    )
                    verdict["layer_replay_receipt_digest"] = (
                        layer_replay_receipt.receipt_digest
                    )
                    verdict["auxiliary_captures"] = list(
                        layer_replay_receipt.observation.auxiliary_captures
                    )
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
                        scale=render_scale,
                        selected_authority=selected_authority,
                    )
                    return "contract_gap"
                return "reproduced"
    results: list = [None] * len(shots_)

    async def _score(i, m_i, render_rel, prepared):
        if prepared is not None and prepared.prior_failure is not None:
            assert judgment_payment is not None
            results[i] = judgment_payment.cached_verdict(prepared)
            return
        evidence = evidence_by_index[i]
        if execution_guard is not None:
            execution_guard.check(
                f"score canonical {execution_guard.label} at frame {m_i.frame}"
            )
        verdict = await _judge_unit_or_layer(
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
            selected_authority=selected_authority,
            execution_guard=execution_guard,
        )
        if execution_guard is not None:
            execution_guard.check(
                f"consume canonical {execution_guard.label} score at frame {m_i.frame}"
            )
        results[i] = verdict

    if not raster_required:
        for i, (_f, _r, m_i, rr, _render_receipt) in enumerate(shots_):
            await _score(i, m_i, rr, prepared_observations[i])
    elif len(shots_) == 1:
        await _score(0, shots_[0][2], shots_[0][3], prepared_observations[0])
    else:
        async with anyio.create_task_group() as tg:
            for i, (_f, _r, m_i, rr, _render_receipt) in enumerate(shots_):
                tg.start_soon(_score, i, m_i, rr, prepared_observations[i])
    verdicts = []
    for i, (frame, ref, _m_i, render_rel, render_receipt) in enumerate(shots_):
        prepared = prepared_observations[i]
        v = results[i]
        if (
            judgment_payment is not None
            and prepared is not None
            and prepared.prior_failure is None
        ):
            if render_receipt is None:
                raise ValueError("judgment observation is missing its canonical render receipt")
            capture = judgment_payment.candidate_capture(
                prepared,
                render_rel=render_rel,
                render_receipt=render_receipt,
            )
            v["judgment_observation"] = {
                "request": prepared.request.as_dict(),
                "candidate_capture": capture,
                "reused_attempt": None,
            }
        if (
            judgment_payment is not None
            and prepared is not None
            and prepared.prior_failure is None
            and v.get("decided_by") == "no_optical_signal"
        ):
            failure = judgment_payment.record_no_optical_signal(
                prepared,
                render_rel=render_rel,
                render_receipt=render_receipt,
                verdict=v,
            )
            v["payment_attempt"] = {
                "request_digest": prepared.request.digest,
                "attempt_digest": failure.digest,
                "reason": failure.reason,
                "candidate_capture_digest": failure.candidate_capture_digest,
                "signal_metrics_digest": failure.signal_metrics_digest,
                "suppressed": False,
            }
        sealed_point = (
            None
            if layer_replay_receipt is None
            else layer_replay_receipt.observation.points[i]
        )
        if sealed_point is not None and (sealed_point.frame, sealed_point.ref) != (
            int(frame),
            str(ref),
        ):
            raise ValueError("canonical verdict does not match its sealed replay point")
        sealed_evidence = (
            list(evidence_by_index[i])
            if sealed_point is None
            else list(sealed_point.evidence)
        )
        v["evidence"] = sealed_evidence
        v["evidence_failures"] = [
            row
            for row in sealed_evidence
            if row.get("authoritative") is True and row.get("pass") is False
        ]
        v["missing_evidence"] = (
            list(v.get("missing_evidence") or [])
            if sealed_point is None
            else list(sealed_point.missing_evidence_ids)
        )
        _bind_canonical_evidence(
            v,
            raster_required=raster_required,
            render_rel=render_rel,
            render_receipt=render_receipt,
        )
        if layer_replay_receipt is not None:
            v["layer_replay_receipt_digest"] = layer_replay_receipt.receipt_digest
            v["auxiliary_captures"] = list(
                layer_replay_receipt.observation.auxiliary_captures
            )
        _persist_contract_gaps(
            shot,
            layer,
            _m_i,
            render_rel,
            v,
            mode=_unit_raster_mode(active_unit),
            scale=render_scale,
            selected_authority=selected_authority,
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
            "CONTRACT_GAP for a validated authority amendment",
            1,
        )
        return "contract_gap"
    if failures and all(v.get("judge_conflict") for v in failures):
        log("canonical checks and critic disagree with no evidence-backed repair — recording JUDGE_CONFLICT", 1)
        return "judge_conflict"
    return "failed"
