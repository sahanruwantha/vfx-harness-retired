"""Stage 4 — acceptance: judge the FINISHED chain against the plan's approval moments.

The build stage judges each layer on the axes it owns, at one frame, mid-chain. That is
the right question for a build unit and the wrong one for the shot: an approval moment is
a whole frame produced by the CUMULATIVE chain, and it is only real once every layer has
run. So acceptance is judged exactly once, here, on the full rubric.

    python -m vfx_harness.agents.acceptance <shot-folder> [--moment M2]

Writes an `acceptance` block into shot.json next to `milestones`.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import anyio

from vfx_harness.domain.brief import Shot, load_shot
from vfx_harness.domain.stop_envelope_primitives import require_digest
from vfx_harness.domain.unit_evaluation_receipts import ReplayDependencyBinding
from vfx_harness.evidence.checks import acceptance_evidence
from vfx_harness.evidence.metrics import compare, look_pair, report
from vfx_harness.infrastructure.config import load_environment
from vfx_harness.observability import run_artifacts, transcript
from vfx_harness.observability.log import log
from vfx_harness.orchestration import layer_publication, plan_due
from vfx_harness.orchestration.authority_selection import (
    ResolvedSelectedAuthority,
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.judgment_debt_state import require_judgment_debts_satisfied
from vfx_harness.orchestration.ledger import Ledger, load_layers, load_milestones
from vfx_harness.orchestration.plan_due import require_due_clear, resolve_acceptance_completion
from vfx_harness.orchestration.selected_authority_guard import (
    commit_selected_authority,
)
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain
from vfx_harness.orchestration.shot_authority_capture import (
    shot_authority_writer_fence,
)

from ..blender.session import BlenderError, BlenderSession
from . import acceptance_stop, acceptance_stop_evidence
from .builder import (
    _RESET,
    PASS_MIN,
    _judge,
    _preamble,
    _run_artifact_script,
    _stash_render_with_receipt,
    _verdict,
    ensure_axes,
)
from .builder.prior import (
    PreparedArtifactReplayInput,
    _prepare_artifact_replay_inputs,
    require_prepared_artifact_replay_input_unchanged,
)


class IncompleteChain(RuntimeError):
    """Acceptance was asked to judge a shot that is not finished."""


def _prepare_acceptance_replay_inputs(
    shot: Shot,
    authority: acceptance_stop.AcceptanceAuthoritySnapshot,
) -> tuple[PreparedArtifactReplayInput, ...]:
    """Capture exact accepted layer bytes before any acceptance replay begins."""

    entries: list[tuple[str, Path]] = []
    expected: list[tuple[str, str, tuple[ReplayDependencyBinding, ...]]] = []
    for index, row in enumerate(authority.chain):
        script = row.get("script")
        digest = row.get("script_sha256")
        if not isinstance(script, str) or not script:
            raise IncompleteChain(
                f"acceptance authority chain[{index}] has no replay script"
            )
        try:
            digest = require_digest(
                digest,
                f"acceptance authority chain[{index}].script_sha256",
            )
        except ValueError as exc:
            raise IncompleteChain(str(exc)) from exc
        raw_dependencies = row.get("replay_dependencies")
        if not isinstance(raw_dependencies, list):
            raise IncompleteChain(
                f"acceptance authority chain[{index}].replay_dependencies must be a list"
            )
        try:
            dependencies = tuple(
                ReplayDependencyBinding.parse(
                    dependency,
                    f"acceptance authority chain[{index}].replay_dependencies[{dependency_index}]",
                )
                for dependency_index, dependency in enumerate(raw_dependencies)
            )
        except ValueError as exc:
            raise IncompleteChain(str(exc)) from exc
        entries.append((script, shot.folder / script))
        expected.append((script, digest, dependencies))
    if not entries:
        raise IncompleteChain("acceptance authority contains no replay scripts")
    try:
        prepared = _prepare_artifact_replay_inputs(shot.folder, entries)
    except BlenderError as exc:
        raise IncompleteChain(str(exc)) from exc
    observed = [
        (
            item.executed.script_path,
            item.executed.script_sha256,
            tuple(
                ReplayDependencyBinding.mint(
                    kind=dependency.kind,
                    path=dependency.path,
                    sha256=dependency.sha256,
                )
                for dependency in item.executed.dependencies
            ),
        )
        for item in prepared
    ]
    if observed != expected:
        raise IncompleteChain(
            "descriptor-read acceptance replay bytes do not match authority_before"
        )
    return prepared


def _require_acceptance_replay_current(
    replay_inputs: tuple[PreparedArtifactReplayInput, ...],
) -> None:
    """Retain exact executed-source lineage through acceptance publication."""

    try:
        for item in replay_inputs:
            require_prepared_artifact_replay_input_unchanged(item)
    except BlenderError as exc:
        raise IncompleteChain(str(exc)) from exc


def _current_layer_publications(
    shot: Shot,
    layers,
    selected_authority: ResolvedSelectedAuthority,
) -> tuple[layer_publication.VerifiedLayerPublication, ...]:
    """Capture every public layer boundary or report the complete invalid set."""

    publications: list[layer_publication.VerifiedLayerPublication] = []
    failures: list[str] = []
    for layer in layers:
        try:
            publications.append(
                layer_publication.require_current_layer_publication(
                    shot.folder,
                    layer,
                    selected_authority,
                )
            )
        except layer_publication.LayerPublicationConflict as exc:
            failures.append(f"layer {layer.id}: {exc}")
    if failures:
        raise IncompleteChain(
            "receipt-backed layer publication is incomplete — " + "; ".join(failures)
        )
    return tuple(publications)


def _chain(
    session: BlenderSession,
    shot: Shot,
    *,
    force: bool = False,
    expected_bundle_digest: str | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    prepared_inputs: tuple[PreparedArtifactReplayInput, ...] | None = None,
) -> list[str]:
    """Run every layer script from an empty scene — the deliverable, start to finish.

    Refuses a PARTIAL chain. This used to log a missing script and carry on, so
    acceptance could pronounce on a shot that was never fully built — and then
    reconcile() would mark real layer verdicts `superseded_by_acceptance` on the
    strength of that partial render, corrupting good records with a bad judgement.
    """
    current_selected = selected_authority
    if current_selected is None and not force:
        try:
            current_selected = resolve_selected_authority(shot.folder)
        except SelectedAuthorityResolutionError as exc:
            raise IncompleteChain(str(exc)) from exc
    layers = selected_layer_chain(
        shot,
        selected_authority=current_selected,
        expected_bundle_digest=expected_bundle_digest,
    )
    ledger = Ledger(shot, selected_authority=current_selected)
    missing = [f"layer {g.id} ({g.script}) has no script"
               for g in layers if not (shot.folder / g.script).is_file()]
    unpassed = [f"layer {g.id} is '{ledger.status(g.as_milestone())}'"
                for g in layers
                if (shot.folder / g.script).is_file()
                and ledger.status(g.as_milestone()) != "passed"]
    publications_before: tuple[layer_publication.VerifiedLayerPublication, ...] = ()
    if not force:
        assert current_selected is not None
        publications_before = _current_layer_publications(
            shot,
            layers,
            current_selected,
        )
    if (missing or unpassed) and not force:
        raise IncompleteChain(
            "refusing to judge an unfinished shot — " + "; ".join(missing + unpassed)
            + ". Finish those layers first, or pass --force (the verdict will not be "
              "about the deliverable).")
    if missing or unpassed:
        log(f"! --force: judging an INCOMPLETE chain — {'; '.join(missing + unpassed)}")

    executable_layers = [g for g in layers if (shot.folder / g.script).is_file()]
    if not force:
        if prepared_inputs is None:
            raise IncompleteChain(
                "acceptance requires descriptor-read inputs matched to authority_before"
            )
        expected_locators = [str(g.script) for g in executable_layers]
        observed_locators = [item.executed.script_path for item in prepared_inputs]
        if observed_locators != expected_locators:
            raise IncompleteChain(
                "prepared acceptance replay order does not match the selected layer DAG"
            )
        _require_acceptance_replay_current(prepared_inputs)

    session.run(_RESET)
    session.run(_preamble(shot))
    ran = []
    replay_index = 0
    for g in layers:
        p = shot.folder / g.script
        if not p.is_file():
            continue                      # only reachable under --force
        log(f"chain: {g.script}", 1)
        _run_artifact_script(
            session,
            p,
            None if force else prepared_inputs[replay_index],
        )
        replay_index += 1
        ran.append(g.script)
    if not force:
        _require_acceptance_replay_current(prepared_inputs)
        assert current_selected is not None
        publications_after = _current_layer_publications(
            shot,
            layers,
            current_selected,
        )
        if publications_after != publications_before:
            raise IncompleteChain(
                "receipt-backed layer publication changed during acceptance replay"
            )
    return ran


async def accept(shot: Shot, session: BlenderSession, only: str | None = None,
                 verbose: bool = True, force: bool = False,
                 repair: bool = False) -> dict:
    try:
        selected_authority = resolve_selected_authority(shot.folder)
    except SelectedAuthorityResolutionError as exc:
        raise IncompleteChain(str(exc)) from exc
    plan_due.require_due_clear(
        shot.folder,
        acceptance=True,
        record_kinds=frozenset({"assumption"}),
        selected_authority=selected_authority,
    )
    if only is None:
        try:
            require_judgment_debts_satisfied(
                shot.folder,
                selected_authority,
            )
        except ValueError as exc:
            raise IncompleteChain(str(exc)) from exc
    moments = load_milestones(shot, selected_authority)
    if only:
        moments = {k: v for k, v in moments.items() if k == only} or moments
    authority_before = (
        acceptance_stop.capture_acceptance_authority(
            shot,
            moments,
            selected_authority,
            verify_layer_publications=False,
        )
        if force
        else acceptance_stop.capture_acceptance_authority(
            shot,
            moments,
            selected_authority,
        )
    )
    replay_inputs = (
        ()
        if force
        else _prepare_acceptance_replay_inputs(shot, authority_before)
    )
    axes = await ensure_axes(shot, verbose, selected_authority)
    tpath = transcript.bind(shot.folder, "accept")
    if tpath:
        log(f"transcript → {tpath.relative_to(shot.folder)}", 1)
    ran = _chain(
        session,
        shot,
        force=force,
        expected_bundle_digest=authority_before.bundle_digest,
        selected_authority=selected_authority,
        prepared_inputs=None if force else replay_inputs,
    )
    log(f"chain rebuilt from empty: {len(ran)} scripts — judging {len(moments)} moment(s)")
    transcript.event("accept_start", moments=list(moments), chained=ran,
                     axes=[k for k, _ in axes])

    ledger = Ledger(shot, selected_authority=selected_authority)
    results: dict[str, dict] = {}
    contract_verdicts: dict[tuple[str, str], list[bool]] = {}
    for mid, m in moments.items():
        t0 = time.monotonic()
        log(f"── {mid} @ f{m.frame} vs {m.ref} ──")
        # The whole frame IS the subject here, so NO scope block: the full rubric applies.
        render_rel, render_capture = _stash_render_with_receipt(
            session, shot, m, "accept"
        )
        # DETERMINISTIC first. The critic never once mentioned that barrel_roll M1 was
        # 54% over-exposed against its own measured target; a number catches that for
        # free and grounds the critic's feedback in something checkable.
        deltas = []
        try:
            deltas = compare(*look_pair(str(shot.folder / render_rel),
                                        str(shot.folder / m.ref)))
            log(report(deltas), 1)
        except Exception as e:
            log(f"metrics skipped: {str(e)[:80]}", 1)
        extra = report(deltas) if deltas else ""
        blocking = [d for d in deltas if d.blocking]
        if blocking:
            # The verdict is already decided, so do not pay a model to restate it. 6 of 9
            # recorded acceptance calls were in exactly this position: metrics said
            # points_bot was 84% low, the critic was called anyway, and the moment failed
            # on the metric regardless. The deltas are also better feedback than prose.
            log(f"metrics decide this moment — skipping the critic "
                f"({len(blocking)} blocking)", 1)
            verdict = _verdict({"scores": {}, "issues": [str(d) for d in blocking[:4]]})
            verdict["pass"] = False
            verdict["decided_by"] = "metrics"
        else:
            # _judge, not _critique: acceptance is the last verdict anyone gets, so a
            # borderline call here is the worst place to trust a single noisy score.
            verdict = await _judge(shot, m, render_rel, axes, session, verbose,
                                   ("MEASURED GAPS vs the reference (objective, already "
                                    "computed — treat as fact):\n" + extra) if extra else None,
                                   selected_authority=selected_authority)
            # `_judge` deterministically rejects an empty/black plate before any model
            # call. Preserve that provenance; labelling it "critic" would claim a paid
            # qualitative judgment that never happened.
            if verdict.get("decided_by") != "no_optical_signal":
                verdict["decided_by"] = "critic"
        verdict["round_s"] = round(time.monotonic() - t0, 1)
        if m.fingerprint:
            verdict["fingerprint"] = m.fingerprint
        metric_readings = [
            {
                "metric_id": str(delta.key),
                "value": round(float(delta.got), 6),
                "reference": round(float(delta.ref), 6),
                "relative_delta": round(float(delta.rel), 6),
                "blocking": bool(delta.blocking),
            }
            for delta in blocking
        ]
        blocking = [str(d) for d in blocking]
        contract_evidence = acceptance_evidence(
            shot.folder,
            frame=m.frame,
            ref=m.ref,
            render=render_rel,
            selected_authority=selected_authority,
        )
        failed_authoritative_contracts = [
            row
            for row in contract_evidence
            if row.get("authoritative") is True and row.get("pass") is not True
        ]
        ok = verdict["pass"] and not blocking and not failed_authoritative_contracts
        scores = {
            str(axis): float(score)
            for axis, score in verdict.get("scores", {}).items()
            if isinstance(score, (int, float)) and not isinstance(score, bool)
        }
        results[mid] = {"schema": acceptance_stop_evidence.MOMENT_SCHEMA,
                        "frame": m.frame, "ref": m.ref, "render": render_rel,
                        "render_capture": render_capture,
                        "mean": verdict["mean"], "pass": ok,
                        "critic_pass": verdict["pass"],
                        "decided_by": verdict.get("decided_by", "critic"),
                        "metric_failures": blocking,
                        "metric_readings": metric_readings,
                        "scores": scores,
                        "issues": verdict.get("issues", [])[:4],
                        "contract_evidence": contract_evidence}
        for row in contract_evidence:
            if row.get("authoritative") is not True:
                continue
            binding = (str(row["source"]), str(row["id"]))
            contract_verdicts.setdefault(binding, []).append(row.get("pass") is True)
        verdict["pass"] = ok
        why = "" if not blocking else f"  (critic {verdict['mean']}, but {len(blocking)} metric(s) out of tolerance)"
        log(f"{mid}: mean {verdict['mean']} {'PASS ✅' if ok else 'FAIL ✗'}{why}")
        transcript.event("accept_moment", moment=mid, **results[mid],
                         seconds=verdict.get("round_s"))

    # ``--force`` is a debugging preview.  Even when its incomplete replay happens
    # to look acceptable, it cannot mint a deliverable outcome or discharge plan
    # debt.  Promotion is reserved for the ordinary complete-chain path.
    publishable = not force
    outcome = (
        acceptance_stop.compile_acceptance_outcome(shot, authority_before, results)
        if not only and publishable
        else None
    )
    authority_after = (
        acceptance_stop.capture_acceptance_authority(
            shot,
            moments,
            selected_authority,
            verify_layer_publications=False,
        )
        if force
        else acceptance_stop.capture_acceptance_authority(
            shot,
            moments,
            selected_authority,
        )
    )
    if replay_inputs:
        _require_acceptance_replay_current(replay_inputs)
    if authority_after != authority_before:
        raise ValueError(
            "selected acceptance authority, judgment debt, or accepted build changed "
            "during judgment; the verdict is not attributable to one before-state"
        )
    failed = {mid: result for mid, result in results.items() if not result["pass"]}

    def publish(operation, mutation):
        if replay_inputs:
            _require_acceptance_replay_current(replay_inputs)
        result = commit_selected_authority(
            shot.folder,
            selected_authority,
            operation=operation,
            mutation=mutation,
        )
        if replay_inputs:
            _require_acceptance_replay_current(replay_inputs)
        return result

    def publish_ledger(operation: str) -> str:
        """Stage outside authority locks, then commit in the global writer order."""

        if replay_inputs:
            _require_acceptance_replay_current(replay_inputs)
        binding = json.dumps(
            {
                "schema": "vfx-harness.acceptance-ledger-publication-binding/v1",
                "selection_token": selected_authority.selection_token.to_dict(),
                "operation": operation,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        prepared = ledger.prepare_save(authority_binding=binding)
        committed = False
        try:
            if replay_inputs:
                _require_acceptance_replay_current(replay_inputs)

            def commit() -> str:
                if replay_inputs:
                    _require_acceptance_replay_current(replay_inputs)
                result = ledger.commit_prepared_save(
                    prepared,
                    authority_binding=binding,
                    writer_capability=writer_capability,
                )
                if replay_inputs:
                    _require_acceptance_replay_current(replay_inputs)
                return result

            with shot_authority_writer_fence(shot.folder) as writer_capability:
                result = commit_selected_authority(
                    shot.folder,
                    selected_authority,
                    operation=operation,
                    mutation=commit,
                )
                committed = True
            if replay_inputs:
                _require_acceptance_replay_current(replay_inputs)
            return result
        finally:
            if not committed:
                ledger.discard_prepared_save(prepared)

    if not only and publishable:
        # A frame-unspecified contract is evaluated at every acceptance moment.  One
        # passing reading cannot permanently satisfy the obligation when another
        # authoritative reading of the same binding fails in this attempt.
        passed_contract_evidence = {
            binding
            for binding, verdicts in contract_verdicts.items()
            if verdicts and all(verdicts)
        }
        publish(
            "resolve acceptance completion authority",
            lambda: resolve_acceptance_completion(
                shot.folder,
                passed_evidence=passed_contract_evidence,
                expected_bundle_digest=authority_before.bundle_digest,
                selected_authority=selected_authority,
            ),
        )
    if not failed and not only and publishable:
        require_due_clear(
            shot.folder,
            acceptance=True,
            expected_bundle_digest=authority_before.bundle_digest,
            selected_authority=selected_authority,
        )

    acceptance_record = {
        "scripts": ran,
        "moments": results,
        "passed": sum(1 for r in results.values() if r["pass"]),
        "total": len(results),
    }
    if outcome is not None:
        acceptance_record["outcome"] = outcome.as_dict()
    if force:
        acceptance_record["authoritative"] = False
        acceptance_record["reason"] = "forced_debug_preview"
        if repair:
            log("! --repair is ignored for a forced debugging preview")
    else:
        ledger.data["acceptance"] = acceptance_record
        publish_ledger("publish acceptance outcome")
        if not only:                  # a partial run cannot judge the whole chain
            acceptance_record["superseded"] = publish(
                "reconcile accepted layer outcomes",
                lambda: reconcile(
                    shot,
                    results,
                    ledger,
                    selected_authority,
                ),
            )
            plan = repair_plan(shot, results, selected_authority)
            acceptance_record["repair_plan"] = plan
            ledger.data["acceptance"] = acceptance_record
            publish_ledger("publish acceptance repair diagnosis")
            if plan:
                log(
                    f"! {len(plan)} failing moment group(s) diagnose layer(s) "
                    f"{', '.join(c['layer'] for c in plan)}, but acceptance has no exact "
                    "revision-checked unit transaction; automatic repair is not authorized"
                )
            if repair:
                log("! --repair is fail-closed: the diagnostic layer route does not authorize "
                    "unit-state mutation")
            acceptance_record["repaired"] = []
            ledger.data["acceptance"] = acceptance_record
            publish_ledger("publish terminal acceptance record")
    destination = "run-scoped debug preview" if force else str(ledger.path)
    log(f"acceptance: {acceptance_record['passed']}/{len(results)} moments passed "
        f"→ {destination}")
    for mid, r in results.items():
        log(f"  {mid} f{r['frame']}: {r['mean']} {'✅' if r['pass'] else '✗'}", 1)
    transcript.event("accept_end", **{k: v for k, v in acceptance_record.items()
                                      if k != "moments"})
    transcript.unbind()
    if failed:
        layout = run_artifacts.active(shot.folder)
        if layout is None:
            raise ValueError(
                "failed acceptance requires an active structured run before a typed stop "
                "can be published"
            )
        envelope = acceptance_stop.compile_acceptance_stop(
            shot,
            layout,
            authority_before,
            results,
            axes,
        )
        raise run_artifacts.TypedStop(9, envelope)
    return acceptance_record


def reconcile(
    shot: Shot,
    results: dict,
    ledger: Ledger,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[str]:
    """Correct the record: a layer that PASSED while the moments it answers for FAILED.

    Nothing linked these before, so both verdicts sat in shot.json contradicting each
    other in silence — server_to_hansa's G50 passed at 3.75 on the one axis it owns while
    M3 (2.50) and M4 (2.20), the two moments in its own judge list, both failed. A layer
    verdict is a claim about a layer; an acceptance verdict is a claim about the frame
    that layer is responsible for. When they disagree, acceptance wins — it judged the
    finished chain.
    """
    layers = load_layers(shot, selected_authority=selected_authority)
    failed_frames = {r["frame"] for r in results.values() if not r["pass"]}
    notes = []
    for g in layers.values():
        claimed = {f for f, _ in g.judges}
        bad = sorted(claimed & failed_frames)
        if not bad:
            continue
        m = g.as_milestone()
        if ledger.status(m) != "passed":
            continue
        note = (f"layer {g.id} passed, but the moment(s) it answers for failed at "
                f"f{', f'.join(map(str, bad))} — superseded by acceptance")
        ledger._slot(m)["superseded_by_acceptance"] = {"frames": bad, "at": _now_str()}
        notes.append(note)
        log(f"! {note}")
    return notes


def _now_str() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def repair_plan(
    shot: Shot,
    results: dict,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> list[dict]:
    """Which layers must be rebuilt to fix the failing moments, earliest first.

    Acceptance used to END here: it wrote the verdict, marked contradicting layer
    verdicts superseded, and stopped. A shot could therefore complete the whole pipeline
    with failing moments recorded and nothing done about them — acceptance was a report,
    not a stage. `owns` already maps every axis to the layer responsible for it, so the
    routing was available all along; nothing consumed it.
    """

    layers = load_layers(shot, selected_authority=selected_authority)
    axis_owner: dict[str, str] = {}
    for g in layers.values():
        for ax in (g.owns or ()):
            # earliest owner wins: fixing the axis at its source is what unblocks the rest
            if ax not in axis_owner or _order(layers, g.id) < _order(layers, axis_owner[ax]):
                axis_owner[ax] = g.id

    culprits: dict[str, dict] = {}
    for mid, r in results.items():
        if r["pass"]:
            continue
        weak = sorted(ax for ax, v in (r.get("scores") or {}).items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)
                      and v <= PASS_MIN)
        for ax in weak:
            owner = axis_owner.get(ax)
            if not owner:
                continue
            slot = culprits.setdefault(owner, {"layer": owner, "axes": set(),
                                               "moments": set()})
            slot["axes"].add(ax)
            slot["moments"].add(mid)
        if not weak:
            log(f"! {mid} failed but no owned axis is at/below {PASS_MIN} "
                f"(metric failures: {len(r.get('metric_failures') or [])}) — "
                f"no layer to route it to", 1)

    plan = sorted(culprits.values(), key=lambda c: _order(layers, c["layer"]))
    for c in plan:
        c["axes"], c["moments"] = sorted(c["axes"]), sorted(c["moments"])
        # Everything above the repair root re-runs too: layer scripts chain, so rebuilding
        # layer 3 invalidates the judgements made on 4-8 that were stacked on top of it.
        c["invalidates"] = [g.id for g in layers.values()
                            if _order(layers, g.id) > _order(layers, c["layer"])]
    return plan


def _order(layers: dict, layer_id: str) -> int:
    keys = sorted(layers, key=lambda k: str(layers[k].script))
    return keys.index(layer_id) if layer_id in keys else 10_000


async def _run(folder: str, only: str | None, blender: str, force: bool = False,
               repair: bool = False) -> None:
    shot = load_shot(folder)
    session = BlenderSession(blender=blender, blend_file=None,
                             assets_dir=shot.folder / "assets",
                             cwd=shot.folder).start()
    try:
        await accept(shot, session, only=only, force=force, repair=repair)
    finally:
        session.close()


def main() -> None:
    load_environment()
    ap = argparse.ArgumentParser(
        description="Judge the finished chain against the plan's acceptance moments.")
    ap.add_argument("folder", help="shot folder (contains brief.md + acceptance.json)")
    ap.add_argument("--moment", default=None, help="judge only this moment (e.g. M2)")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--force", action="store_true",
                    help="judge even an incomplete chain (debugging only — the verdict "
                         "will not be about the deliverable)")
    ap.add_argument("--repair", action="store_true",
                    help="request failure routing; currently fail-closed because acceptance "
                         "does not identify an exact revision-checked unit transaction")
    args = ap.parse_args()
    shot = load_shot(args.folder)
    with run_artifacts.invocation(shot.folder, "accept", shot_id=shot.id,
                                  parameters={"moment": args.moment}):
        try:
            anyio.run(_run, args.folder, args.moment, args.blender, args.force,
                      args.repair)
        except IncompleteChain as e:
            log(f"INCOMPLETE CHAIN — {e}")
            raise run_artifacts.RequestedExit(7, f"INCOMPLETE CHAIN — {e}") from None


if __name__ == "__main__":
    main()
