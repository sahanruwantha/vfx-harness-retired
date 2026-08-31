"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

from vfx_harness.agents.builder.authority import commit_selected_authority
from vfx_harness.agents.builder.evidence import (
    _forecast_blocker_ids,
    _geometry_protected_vis_ids,
    _image_reproduction,
    _scene_ids_active_at_declared_frames,
    _scene_ids_active_on_layer,
    _unit_evidence_ids,
    _unit_raster_mode,
    _unit_requires_raster,
)
from vfx_harness.agents.builder.models import _RESET
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import _run_artifact_script
from vfx_harness.agents.builder.verdicts import _executable_unit_verdict, _lookless_without_executable_verdict
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot
from vfx_harness.evidence.scene_checks import load_rows as _load_contract_rows
from vfx_harness.observability.log import (
    log,
)
from vfx_harness.observability.runid import RUN_ID
from vfx_harness.observability.runlog import summary as run_summary
from vfx_harness.observability.runlog import write as write_run
from vfx_harness.orchestration.layer_plans import load_layer_outcome, record_revalidation
from vfx_harness.orchestration.ledger import Ledger, Milestone, plan_strips
from vfx_harness.orchestration.revalidation import eligibility, input_manifest

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority


def _blender_version(session: BlenderSession) -> str:
    try:
        row = session.run("RESULT = bpy.app.version_string", journal=False)
        return str(row.get("result") or row.get("RESULT") or "unknown")
    except Exception:
        return "unknown"


def _scene_object_manifest(session: BlenderSession) -> dict[str, str]:
    result = session.run(
        "RESULT = {o.name: str(o.get('bvfx_role') or '') for o in bpy.context.scene.objects}",
        journal=False,
    ).get("result")
    return {str(name): str(role) for name, role in (result or {}).items()}


def _scope_added_object_errors(
    before: dict[str, str], after: dict[str, str], allowed_roles: tuple[str, ...]
) -> list[str]:
    """Reject persisted helpers and semantic roles outside a scoped unit's authority."""
    def allowed(role: str) -> bool:
        for pattern in allowed_roles:
            if fnmatch.fnmatchcase(role, pattern):
                return True
            # Mutation manifests name semantic namespaces (``camera``), while
            # executable contracts and objects use concrete descendants
            # (``camera.*`` / ``camera.main``). A namespace owns only its
            # dot-delimited descendants, never a similar sibling such as ``camera_rig``.
            if not any(token in pattern for token in "*?[") and role.startswith(pattern + "."):
                return True
        return False

    errors = []
    for name in sorted(set(after) - set(before)):
        role = after[name]
        if not role:
            errors.append(f"new object {name!r} has no bvfx_role")
        elif not allowed(role):
            errors.append(
                f"new object {name!r} has undeclared role {role!r}; allowed {list(allowed_roles)}"
            )
    return errors


def _candidate_scope_errors(
    scope_mode: str,
    allowed_roles: tuple[str, ...],
    before: dict[str, str],
    after: dict[str, str],
) -> list[str]:
    """Apply canonical object-scope authority to every candidate probe (HIR-0058)."""
    if scope_mode != "scoped":
        return []
    return _scope_added_object_errors(before, after, allowed_roles)


def _try_revalidate(
    shot: Shot,
    m: Milestone,
    script_rel: str,
    prior_paths: list[Path],
    session: BlenderSession,
    *,
    layer,
    ledger: Ledger,
    t_layer: float,
    active_unit=None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> Ledger | None:
    """Replay an unchanged sealed layer without launching builder or critic models."""
    if layer is None:
        return None

    outcome = load_layer_outcome(shot.folder, str(layer.id))
    blender_version = _blender_version(session)
    manifest = input_manifest(
        shot.folder,
        layer,
        blender_version=blender_version,
        selected_authority=selected_authority,
    )
    eligible, reasons = eligibility(outcome, manifest, shot.folder)
    if not eligible:
        if outcome:
            log(f"revalidation fast path unavailable: {'; '.join(reasons[:3])}", 1)
        return None

    log("REVALIDATE: sealed inputs are unchanged; replaying scripts and authoritative evidence before any model launch")
    try:
        session.run(_RESET)
        session.run(builder_package()._preamble(shot))
        builder_package()._run_prior_paths(session, prior_paths)
        before_objects = builder_package()._scene_object_manifest(session)
        _run_artifact_script(session, shot.folder / script_rel)
        # The unit whose script just replayed owns the scope being checked. Falling back
        # to "the only stage" silently SKIPPED scope entirely for every multi-unit
        # layer — a fast path that cannot verify scope must not be taken at all.
        unit = active_unit or (layer.stages[0] if len(layer.stages) == 1 else None)
        if unit is not None and unit.mutates.mode == "scoped":
            scope_errors = _scope_added_object_errors(
                before_objects, builder_package()._scene_object_manifest(session), unit.mutates.roles
            )
            if scope_errors:
                log(f"REVALIDATE miss: scoped artifact violation ({'; '.join(scope_errors[:3])})", 1)
                return None
        elif unit is None and any(
            stage.mutates.mode == "scoped" for stage in layer.stages
        ):
            log(
                "REVALIDATE miss: layer has scoped units but no active unit to check "
                "their artifact scope against",
                1,
            )
            return None
    except Exception as exc:
        log(f"REVALIDATE miss: deterministic replay failed ({str(exc)[:120]})", 1)
        return None

    sealed = {int(row["frame"]): row for row in outcome.get("canonical") or []}
    raster_required = _unit_requires_raster(
        shot,
        active_unit,
        selected_authority=selected_authority,
    )
    try:

        contract_frames = {
            str(row.get("id")): int(row.get("frame"))
            for row in _load_contract_rows(shot.folder, selected_authority)
            if isinstance(row, dict)
            and row.get("id")
            and row.get("frame") is not None
        }
    except (OSError, ValueError, TypeError):
        contract_frames = {}
    canonical = []
    frame_results = []
    judges = list(layer.judges)
    for frame, ref in judges:
        m_i = (
            m
            if len(judges) == 1
            else layer.milestone_at(
                frame,
                ref,
                plan_strips(shot, selected_authority),
            )
        )
        if raster_required:
            render_rel = builder_package()._stash_render(
                session,
                shot,
                m_i,
                f"revalidate_f{frame}",
                mode=_unit_raster_mode(active_unit),
            )
            evidence = builder_package()._render_evidence(
                shot,
                layer,
                m_i,
                render_rel,
                session,
                active_unit=active_unit,
                selected_authority=selected_authority,
            )
            authoritative = [row for row in evidence if row.get("authoritative")]
            prior = sealed.get(int(frame)) or {}
            reproduction = _image_reproduction(
                shot.folder / str(prior.get("render", "")),
                shot.folder / render_rel,
            )
            passed = bool(
                authoritative
                and all(row.get("pass") for row in authoritative)
                and reproduction.get("match")
            )
            verdict = {
                "scores": {},
                "mean": float((outcome.get("best") or {}).get("mean") or 4.0),
                "pass": passed,
                "issues": [],
                "evidence": evidence,
                "decided_by": "deterministic_revalidation",
                "reproduction": reproduction,
                "render": render_rel,
            }
        else:
            render_rel = ""
            evidence = builder_package()._render_evidence(
                shot,
                layer,
                m_i,
                None,
                session,
                active_unit=active_unit,
                selected_authority=selected_authority,
            )
            extra_required: set[str] = set()
            inactive_ids: set[str] = set()
            if active_unit is not None:
                with contextlib.suppress(OSError, ValueError, KeyError, json.JSONDecodeError):
                    extra_required = _scene_ids_active_at_declared_frames(
                        shot,
                        str(layer.id),
                        _geometry_protected_vis_ids(
                            shot,
                            layer,
                            active_unit,
                            selected_authority=selected_authority,
                        ),
                        [int(frame)],
                        selected_authority=selected_authority,
                    )
                    bound_ids = set(_unit_evidence_ids(active_unit, int(frame)) or set())
                    due = _scene_ids_active_on_layer(
                        shot,
                        str(layer.id),
                        bound_ids,
                        [int(frame)],
                        selected_authority=selected_authority,
                    )
                    inactive_ids = bound_ids - due
            extra_required.update(_forecast_blocker_ids(evidence))
            verdict = _executable_unit_verdict(
                active_unit,
                int(frame),
                [(str(axis), str(axis)) for axis in getattr(layer, "owns", ())],
                evidence,
                contract_frames=contract_frames,
                extra_required_ids=extra_required,
                inactive_ids=inactive_ids,
            ) or _lookless_without_executable_verdict(
                active_unit,
                int(frame),
                [(str(axis), str(axis)) for axis in getattr(layer, "owns", ())],
            )
            passed = bool(verdict.get("pass"))
            reproduction = {
                "required": False,
                "reason": "executable_only_scene_evidence",
            }
            verdict = {
                **verdict,
                "decided_by": "deterministic_executable_revalidation",
                "reproduction": reproduction,
                "render": render_rel,
            }
            authoritative = [row for row in evidence if row.get("authoritative")]
        canonical.append(((frame, ref), verdict))
        frame_results.append(
            {
                "frame": frame,
                "pass": passed,
                "authoritative_total": len(authoritative),
                "authoritative_passed": sum(bool(row.get("pass")) for row in authoritative),
                "reproduction": reproduction,
            }
        )
    if not all(row[1]["pass"] for row in canonical):
        bad = [str(frame) for (frame, _ref), verdict in canonical if not verdict["pass"]]
        changed = "evidence or canonical pixels" if raster_required else "executable evidence"
        log(
            f"REVALIDATE miss: {changed} changed at f{', f'.join(bad)}; "
            "falling back to the full builder",
            1,
        )
        return None

    best = dict(outcome.get("best") or {})
    ledger.record_round(m, kind="revalidate", index=0, render=canonical[0][1]["render"], verdict=canonical[0][1])
    ledger.mark(m, "passed", best=best)
    attempt = int(ledger._slot(m).get("attempt") or 0)
    if selected_authority is None:
        record_revalidation(
            shot.folder,
            str(layer.id),
            run_id=RUN_ID,
            attempt=attempt,
            evidence=frame_results,
        )
    else:
        commit_selected_authority(
            shot.folder,
            selected_authority,
            operation=f"record layer {layer.id} revalidation",
            mutation=lambda: record_revalidation(
                shot.folder,
                str(layer.id),
                run_id=RUN_ID,
                attempt=attempt,
                evidence=frame_results,
            ),
        )
    rec_path = write_run(
        shot.folder,
        layer,
        status="passed",
        rounds=[{"kind": "revalidate", "mean": canonical[0][1]["mean"], "pass": True}],
        canonical=[
            {
                "frame": frame,
                "mean": verdict["mean"],
                "pass": True,
                "decided_by": verdict.get("decided_by", "deterministic_revalidation"),
                "evidence": verdict["evidence"],
                "reproduction": verdict["reproduction"],
            }
            for (frame, _ref), verdict in canonical
        ],
        cost=0.0,
        turns=0,
        seconds=time.monotonic() - t_layer,
        extra={
            "run_id": RUN_ID,
            "attempt": attempt,
            "revalidation": True,
            "cost_sessions": 0,
            "cost_by_role": {},
            "tools": {
                "calls": {},
                "total": 0,
                "adoption": {},
                "applicability": dict.fromkeys(("render_pass", "check_scene", "diff_frames", "verify_change"), False),
                "unused_required_tools": [],
                "not_applicable_tools": ["render_pass", "check_scene", "diff_frames", "verify_change"],
            },
        },
    )
    log("\n" + run_summary(json.loads(rec_path.read_text(encoding="utf-8"))))
    proof = "authoritative evidence and sealed pixels" if raster_required else "executable evidence"
    log(f"REVALIDATE PASS: {proof} unchanged; builder and critic sessions skipped")
    return ledger


def _live_reopen_reason(events) -> str:
    """The operator's retry reason, iff it is still the unit's live lifecycle fact.

    Newest-first scan stopping at the first lifecycle marker: a reopen record is
    consumed by a later seal (`passed`) or an amendment re-entry (`pending`).
    Surfacing a retired record — or arming mutation on it — is stale-context
    injection, the same defect as a superseded approach's role names steering a
    rematerialized unit.
    """
    for event in reversed(events or []):
        to_state = event.get("to")
        if to_state == "retryable":
            return str(event.get("reason") or "")
        if to_state in ("passed", "pending"):
            break
    return ""


def _live_round_budget(rounds: int, comparison_state: dict) -> int:
    """A typed in-scope abstention ends model-guided visual search immediately."""
    return 0 if comparison_state.get("cannot_express") else int(rounds)


def _retry_warm_start(
    previous_status: str,
    script_path: Path,
    *,
    previous_artifact_unit_hash: str,
    current_unit_hash: str,
) -> bool:
    """Whether a retry artifact belongs to the exact current unit authority."""
    retryable = {"failed", "judge_conflict", "contract_gap", "truncated", "in_progress"}
    return (
        previous_status in retryable
        and script_path.is_file()
        and bool(previous_artifact_unit_hash)
        and previous_artifact_unit_hash == current_unit_hash
    )
