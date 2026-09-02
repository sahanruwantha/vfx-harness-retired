"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import itertools
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anyio
from claude_agent_sdk import create_sdk_mcp_server

from vfx_harness.agents.plan_tools.constants import _JPEG_Q, SERVER_NAME
from vfx_harness.agents.plan_tools.gate import register_gate_tools
from vfx_harness.agents.plan_tools.materialize_mcp import register_materialize_tools
from vfx_harness.agents.plan_tools.session_tools import register_session_tools
from vfx_harness.agents.plan_tools.spike import _CheckBatchBudget, _SpikeBudget
from vfx_harness.agents.plan_tools.spike_tools import register_spike_tools
from vfx_harness.domain.work_units import allowed_unit_provides
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.orchestration.jit_materialization import materialization_candidate_revision
from vfx_harness.orchestration.plan_authority import PlanPublicationError, resolve_current


def build_plan_tools(
    shot_folder: Path,
    *,
    blender: str = "blender",
    lab_dir: Path | None = None,
    include_gate: bool = False,
    run_layout: run_artifacts.RunLayout | None = None,
    measure_ref_paths: tuple[str, ...] | None = None,
    enabled_tools: frozenset[str] | None = None,
    candidate_materialization: str | Path | None = None,
    overlay_root: str | Path | None = None,
    unit_plan_target: str | Path | None = None,
    unit_plan_selected_authority=None,
):
    shot_folder = Path(shot_folder)
    work = Path(tempfile.mkdtemp(prefix="planlab-"))  # raw ffmpeg output
    layout = run_layout or run_artifacts.ensure(shot_folder, command="plan-lab")
    lab = (Path(lab_dir) if lab_dir else
           layout.scratch / "plan-lab" / "global")
    lab.mkdir(parents=True, exist_ok=True)
    try:
        lab_rel = lab.relative_to(shot_folder).as_posix()
    except ValueError:
        # Tests and external callers may deliberately supply an isolated temporary lab.
        lab_rel = str(lab)
    seq = itertools.count(1)
    spikes = itertools.count(1)

    def _resolve(p: str) -> Path:
        path = Path(p).expanduser()
        resolved = (path if path.is_absolute() else shot_folder / path).resolve()
        root = shot_folder.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"plan tool path escapes the active planning workspace: {p!r}"
            ) from exc
        return resolved

    def _keep(im, stem: str) -> Path:
        """Persist exactly what the agent saw (downscaled JPEG) for post-mortem."""
        out = lab / f"{next(seq):03d}_{stem}.jpg"
        im.save(out, format="JPEG", quality=_JPEG_Q)
        return out

    # Exploration is useful; turning fifty independent checks into fifty narrated tool
    # turns is not. This counter is scoped to one plan-agent session. Two single probes let
    # the planner learn a metric/region; after that the batch tool is the only path until a
    # batch has run, at which point two more targeted follow-ups are available.
    check_budget = _CheckBatchBudget()
    spike_budget = _SpikeBudget()
    # Draft and verify share one run, so deterministic reference fingerprints are
    # computed once per image content and reused across both sessions.
    measure_cache_path = (
        lab.parent if lab.parent.name == "plan-lab" else lab
    ) / "measure_ref_cache.json"
    materialization_write_lock = anyio.Lock()
    materialization_revision_token: str | None = None
    materialization_axis_ids: tuple[str, ...] | None = None
    materialization_layer_id: str | None = None
    materialization_allowed_provides: frozenset[str] | None = None
    materialization_requirement_statements: dict[str, str] = {}
    if candidate_materialization is not None:
        candidate_path = Path(candidate_materialization)
        if candidate_path.is_file():

            materialization_revision_token = materialization_candidate_revision(candidate_path)
            try:
                candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
                materialization_layer_id = str(
                    (candidate_payload.get("layer") or {}).get("id") or ""
                ) or None
                materialization_axis_ids = tuple(
                    str(value)
                    for value in ((candidate_payload.get("layer") or {}).get("owns") or [])
                    if str(value)
                )
                bundle = resolve_current(shot_folder)
                if candidate_payload.get("bundle_hash") != bundle.content_hash:
                    raise ValueError("materialization candidate is pinned to another bundle")
                global_document = json.loads(
                    (bundle.root / "layers.json").read_text(encoding="utf-8")
                )
                global_row = next(
                    row
                    for row in global_document.get("layers") or []
                    if isinstance(row, dict)
                    and str(row.get("id") or "") == materialization_layer_id
                )
                materialization_allowed_provides = allowed_unit_provides(global_row)
                owned_requirement_ids = {
                    str(value)
                    for value in ((global_row.get("jit") or {}).get("owned_requirements") or [])
                    if str(value)
                }
                global_requirements = json.loads(
                    (bundle.root / "requirements.json").read_text(encoding="utf-8")
                )
                materialization_requirement_statements = {
                    str(row.get("id")): str(row.get("statement") or "").strip()
                    for row in (global_requirements.get("requirements") or [])
                    if isinstance(row, dict)
                    and str(row.get("id") or "") in owned_requirement_ids
                    and str(row.get("statement") or "").strip()
                }
            except (
                OSError,
                ValueError,
                TypeError,
                AttributeError,
                StopIteration,
                PlanPublicationError,
                json.JSONDecodeError,
            ):
                # The staging transaction reports malformed candidate authority. Keep
                # an empty enum here so the tool cannot accept guessed claim axes first.
                materialization_axis_ids = ()
                materialization_allowed_provides = frozenset()

    ns = SimpleNamespace(
        gate_calls=0,
        prior_gate_signature=None,
        prior_preview_signature=None,
        materialization_revision_token=materialization_revision_token,
        materialization_axis_ids=materialization_axis_ids,
        materialization_layer_id=materialization_layer_id,
        materialization_allowed_provides=materialization_allowed_provides,
        materialization_requirement_statements=materialization_requirement_statements,
    )
    closed = {
        "shot_folder": shot_folder,
        "work": work,
        "lab": lab,
        "lab_rel": lab_rel,
        "seq": seq,
        "spikes": spikes,
        "_resolve": _resolve,
        "_keep": _keep,
        "check_budget": check_budget,
        "spike_budget": spike_budget,
        "measure_cache_path": measure_cache_path,
        "blender": blender,
        "include_gate": include_gate,
        "measure_ref_paths": measure_ref_paths,
        "unit_plan_target": unit_plan_target,
        "unit_plan_selected_authority": unit_plan_selected_authority,
        "candidate_materialization": candidate_materialization,
        "overlay_root": overlay_root,
        "materialization_write_lock": materialization_write_lock,
        "layout": layout,
        "ns": ns,
    }
    (
        publish_unit_plan,
        probe_video,
        contact_sheet,
        extract_frames,
        measure_ref,
        measure_check,
        measure_checks,
    ) = register_session_tools(**closed)
    spike = register_spike_tools(**closed)
    (
        ask_supervisor,
        escalate_vocabulary_gap,
        run_gate,
        evidence_vocabulary,
        gate_preview,
    ) = register_gate_tools(**closed)
    (
        stage_materialization_unit_tool,
        unstage_materialization_unit_tool,
        mint_refobs_tool,
        materialization_status,
        finalize_materialization,
        patch_materialization,
    ) = register_materialize_tools(**closed)
    # probe_video / contact_sheet / extract_frames were DEFINED and never registered, so
    # they were unreachable on every shot — not just stills-only ones. contact_sheet's own
    # description reads "This is how you do the scene read", and it has never once been
    # callable. Nothing detected that, because an absent tool is indistinguishable from a
    # tool the model chose not to call.
    #
    # Registered conditionally on the shot actually having video: a stills-only shot should
    # not carry three tools whose every call can only fail, and a shot WITH video must not
    # silently lose its scene read. The exclusion is now a decision with a reason instead
    # of an omission.
    video = sorted((shot_folder / "refs").glob("*.mp4")) if (shot_folder / "refs").is_dir() else []
    tools = [measure_ref, measure_check, measure_checks, spike, ask_supervisor,
             evidence_vocabulary, gate_preview, escalate_vocabulary_gap]
    if unit_plan_target is not None:
        tools.append(publish_unit_plan)
    if candidate_materialization is not None:
        tools.extend([
            stage_materialization_unit_tool,
            unstage_materialization_unit_tool,
            mint_refobs_tool,
            materialization_status,
            finalize_materialization,
            patch_materialization,
        ])
    if include_gate:
        tools.append(run_gate)
    if video:
        tools = [probe_video, contact_sheet, extract_frames, *tools]
    if enabled_tools is not None:
        tools = [candidate for candidate in tools if candidate.name in enabled_tools]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    log(
        f"plan tools: {', '.join(t.name for t in tools)}"
        + (f"  ({len(video)} video ref(s))" if video else "  (stills only — video tools not registered)"),
        1,
    )
    return server, names
