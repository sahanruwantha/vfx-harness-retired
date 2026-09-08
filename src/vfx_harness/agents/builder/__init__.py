"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from claude_agent_sdk import ResultMessage as ResultMessage

from vfx_harness.agents.builder.axes import _MOTION_AXIS_WORDS as _MOTION_AXIS_WORDS
from vfx_harness.agents.builder.axes import _axes_need_motion as _axes_need_motion
from vfx_harness.agents.builder.axes import _builder_ticket_context as _builder_ticket_context
from vfx_harness.agents.builder.axes import _evidence_convergence_stop as _evidence_convergence_stop
from vfx_harness.agents.builder.axes import _layer_needs_motion as _layer_needs_motion
from vfx_harness.agents.builder.axes import _owned_axes as _owned_axes
from vfx_harness.agents.builder.axes import _warn_unowned_axes as _warn_unowned_axes
from vfx_harness.agents.builder.axes import ensure_axes as ensure_axes
from vfx_harness.agents.builder.cli import _run as _run
from vfx_harness.agents.builder.cli import main as main
from vfx_harness.agents.builder.critic import _JUDGE_SD as _JUDGE_SD
from vfx_harness.agents.builder.critic import _adjudicate_band as _adjudicate_band
from vfx_harness.agents.builder.critic import _aggregate_critic_panel as _aggregate_critic_panel
from vfx_harness.agents.builder.critic import _borderline as _borderline
from vfx_harness.agents.builder.critic import _critique as _critique
from vfx_harness.agents.builder.critic import _judge as _judge
from vfx_harness.agents.builder.critic import _needs_critic_panel as _needs_critic_panel
from vfx_harness.agents.builder.critic import _round_rank as _round_rank
from vfx_harness.agents.builder.critic_focus import _apply_evidence_gate as _apply_evidence_gate
from vfx_harness.agents.builder.critic_focus import _audit_panel_citations as _audit_panel_citations
from vfx_harness.agents.builder.critic_focus import _canonical_failing_ids as _canonical_failing_ids
from vfx_harness.agents.builder.critic_focus import _claim_context as _claim_context
from vfx_harness.agents.builder.critic_focus import _filter_critic_issues as _filter_critic_issues
from vfx_harness.agents.builder.critic_focus import _focus_references as _focus_references
from vfx_harness.agents.builder.critic_focus import _focus_requests as _focus_requests
from vfx_harness.agents.builder.critic_focus import _image_optical_signal as _image_optical_signal
from vfx_harness.agents.builder.critic_focus import _make_focus_panels as _make_focus_panels
from vfx_harness.agents.builder.critic_focus import _motion_strip_crop as _motion_strip_crop
from vfx_harness.agents.builder.critic_focus import _repair_action as _repair_action
from vfx_harness.agents.builder.critic_focus import _repair_change_summary as _repair_change_summary
from vfx_harness.agents.builder.critic_focus import _repair_delta as _repair_delta
from vfx_harness.agents.builder.critic_focus import _required_focus_requests as _required_focus_requests
from vfx_harness.agents.builder.critic_focus import _unsatisfiable_pair_findings as _unsatisfiable_pair_findings
from vfx_harness.agents.builder.drain import _collect_approach as _collect_approach
from vfx_harness.agents.builder.drain import _collect_errors as _collect_errors
from vfx_harness.agents.builder.drain import _drain as _drain
from vfx_harness.agents.builder.drain import _drain_once as _drain_once
from vfx_harness.agents.builder.drain import _extract_json as _extract_json
from vfx_harness.agents.builder.evidence import _fault_owner_options_for_unit as _fault_owner_options_for_unit
from vfx_harness.agents.builder.evidence import _forecast_blocker_ids as _forecast_blocker_ids
from vfx_harness.agents.builder.evidence import (
    _geometry_forecast_blocking_evidence as _geometry_forecast_blocking_evidence,
)
from vfx_harness.agents.builder.evidence import _geometry_protected_evidence as _geometry_protected_evidence
from vfx_harness.agents.builder.evidence import _geometry_protected_vis_ids as _geometry_protected_vis_ids
from vfx_harness.agents.builder.evidence import _image_reproduction as _image_reproduction
from vfx_harness.agents.builder.evidence import _render_evidence as _render_evidence
from vfx_harness.agents.builder.evidence import _reproduction_hint as _reproduction_hint
from vfx_harness.agents.builder.evidence import _scene_contract_issue as _scene_contract_issue
from vfx_harness.agents.builder.evidence import (
    _scene_ids_active_at_declared_frames as _scene_ids_active_at_declared_frames,
)
from vfx_harness.agents.builder.evidence import _scene_ids_active_on_layer as _scene_ids_active_on_layer
from vfx_harness.agents.builder.evidence import _scope_bound_evidence as _scope_bound_evidence
from vfx_harness.agents.builder.evidence import _scope_unit_evidence as _scope_unit_evidence
from vfx_harness.agents.builder.evidence import _stash_render as _stash_render
from vfx_harness.agents.builder.evidence import (
    _stash_render_with_receipt as _stash_render_with_receipt,
)
from vfx_harness.agents.builder.evidence import _unit_completion_evidence_ids as _unit_completion_evidence_ids
from vfx_harness.agents.builder.evidence import _unit_evidence_ids as _unit_evidence_ids
from vfx_harness.agents.builder.evidence import _unit_evidence_ids_by_frame as _unit_evidence_ids_by_frame
from vfx_harness.agents.builder.evidence import _unit_raster_mode as _unit_raster_mode
from vfx_harness.agents.builder.evidence import _unit_requires_raster as _unit_requires_raster
from vfx_harness.agents.builder.evidence import _unit_scene_evidence_ids as _unit_scene_evidence_ids
from vfx_harness.agents.builder.evidence import image_evidence_required_for as image_evidence_required_for
from vfx_harness.agents.builder.evidence import look_unsettled_for as look_unsettled_for
from vfx_harness.agents.builder.falsify import _persist_contract_gaps as _persist_contract_gaps
from vfx_harness.agents.builder.falsify import (
    _record_bound_contract_falsification as _record_bound_contract_falsification,
)
from vfx_harness.agents.builder.falsify import (
    _record_composed_contract_gap_falsification as _record_composed_contract_gap_falsification,
)
from vfx_harness.agents.builder.falsify import _record_contract_gap_falsification as _record_contract_gap_falsification
from vfx_harness.agents.builder.falsify import (
    _record_unsatisfiable_pair_falsification as _record_unsatisfiable_pair_falsification,
)
from vfx_harness.agents.builder.layer import _ablate as _ablate
from vfx_harness.agents.builder.layer import _ablation_frames as _ablation_frames
from vfx_harness.agents.builder.layer import _active_unit_layer_view as _active_unit_layer_view
from vfx_harness.agents.builder.layer import _compose_unit_artifact_source as _compose_unit_artifact_source
from vfx_harness.agents.builder.layer import _unit_artifact_path as _unit_artifact_path
from vfx_harness.agents.builder.layer import build_layer as build_layer
from vfx_harness.agents.builder.layer import build_layer_already_fenced as build_layer_already_fenced
from vfx_harness.agents.builder.models import _REPO as _REPO
from vfx_harness.agents.builder.models import _RESET as _RESET
from vfx_harness.agents.builder.models import _TRUNCATED as _TRUNCATED
from vfx_harness.agents.builder.models import CRITIC_MODEL as CRITIC_MODEL
from vfx_harness.agents.builder.models import DISTILL_SYSTEM as DISTILL_SYSTEM
from vfx_harness.agents.builder.models import MAX_BUDGET_USD as MAX_BUDGET_USD
from vfx_harness.agents.builder.models import MAX_CANON_REPAIRS as MAX_CANON_REPAIRS
from vfx_harness.agents.builder.models import MAX_CONTINUES as MAX_CONTINUES
from vfx_harness.agents.builder.models import MAX_TURNS as MAX_TURNS
from vfx_harness.agents.builder.models import MODEL as MODEL
from vfx_harness.agents.builder.models import PASS_MEAN as PASS_MEAN
from vfx_harness.agents.builder.models import PASS_MIN as PASS_MIN
from vfx_harness.agents.builder.models import TASK_BUDGET_TOKENS as TASK_BUDGET_TOKENS
from vfx_harness.agents.builder.models import BuildAuthorityDefect as BuildAuthorityDefect
from vfx_harness.agents.builder.models import BuildTruncated as BuildTruncated
from vfx_harness.agents.builder.models import BuildUnpassed as BuildUnpassed
from vfx_harness.agents.builder.models import LayerVerdictFailed as LayerVerdictFailed
from vfx_harness.agents.builder.models import UnpassedPrior as UnpassedPrior
from vfx_harness.agents.builder.models import _budget_terminal_cause as _budget_terminal_cause
from vfx_harness.agents.builder.models import builder_model as builder_model
from vfx_harness.agents.builder.models import critic_model as critic_model
from vfx_harness.agents.builder.models import distiller_model as distiller_model
from vfx_harness.agents.builder.models import script_model as script_model
from vfx_harness.agents.builder.prior import _ARTIFACT_EVALUATION_BARRIER as _ARTIFACT_EVALUATION_BARRIER
from vfx_harness.agents.builder.prior import ChainBroken as ChainBroken
from vfx_harness.agents.builder.prior import _builder_options as _builder_options
from vfx_harness.agents.builder.prior import _plan_layer_excerpt as _plan_layer_excerpt
from vfx_harness.agents.builder.prior import _preamble as _preamble
from vfx_harness.agents.builder.prior import _prior_layer_paths as _prior_layer_paths
from vfx_harness.agents.builder.prior import _run_artifact_script as _run_artifact_script
from vfx_harness.agents.builder.prior import _run_prior_paths as _run_prior_paths
from vfx_harness.agents.builder.revalidate import _blender_version as _blender_version
from vfx_harness.agents.builder.revalidate import _candidate_scope_errors as _candidate_scope_errors
from vfx_harness.agents.builder.revalidate import _live_reopen_reason as _live_reopen_reason
from vfx_harness.agents.builder.revalidate import _live_round_budget as _live_round_budget
from vfx_harness.agents.builder.revalidate import _retry_warm_start as _retry_warm_start
from vfx_harness.agents.builder.revalidate import _scene_object_manifest as _scene_object_manifest
from vfx_harness.agents.builder.revalidate import _scope_added_object_errors as _scope_added_object_errors
from vfx_harness.agents.builder.revalidate import _try_revalidate as _try_revalidate
from vfx_harness.agents.builder.script_agent import _SCRIPT_SYSTEM as _SCRIPT_SYSTEM
from vfx_harness.agents.builder.script_agent import _build_probe_candidate_server as _build_probe_candidate_server
from vfx_harness.agents.builder.script_agent import _run_script_agent as _run_script_agent
from vfx_harness.agents.builder.script_agent import _script_options as _script_options
from vfx_harness.agents.builder.script_agent import probe_preview_modes as probe_preview_modes
from vfx_harness.agents.builder.state import _APPROACH as _APPROACH
from vfx_harness.agents.builder.state import _ERRORS as _ERRORS
from vfx_harness.agents.builder.state import _FOCUS_RENDER_LOCK as _FOCUS_RENDER_LOCK
from vfx_harness.agents.builder.state import _JOURNAL_INFO as _JOURNAL_INFO
from vfx_harness.agents.builder.state import _RECIPES_USED as _RECIPES_USED
from vfx_harness.agents.builder.stops import (
    compile_hypothesis_falsification_stop as compile_hypothesis_falsification_stop,
)
from vfx_harness.agents.builder.unit_dispatch import build_unit as build_unit
from vfx_harness.agents.builder.unit_finalize import _metric_report as _metric_report
from vfx_harness.agents.builder.unit_finalize import (
    _persist_journal_and_finalize_script as _persist_journal_and_finalize_script,
)
from vfx_harness.agents.builder.unit_finalize import _publish_unit_outcome as _publish_unit_outcome
from vfx_harness.agents.builder.verdicts import _composition_judge_unit as _composition_judge_unit
from vfx_harness.agents.builder.verdicts import _executable_unit_verdict as _executable_unit_verdict
from vfx_harness.agents.builder.verdicts import _judge_unit_or_layer as _judge_unit_or_layer
from vfx_harness.agents.builder.verdicts import _layer_motion_frames as _layer_motion_frames
from vfx_harness.agents.builder.verdicts import _load_provisional_decisions as _load_provisional_decisions
from vfx_harness.agents.builder.verdicts import _look_without_image_domain_verdict as _look_without_image_domain_verdict
from vfx_harness.agents.builder.verdicts import (
    _lookless_without_executable_verdict as _lookless_without_executable_verdict,
)
from vfx_harness.agents.builder.verdicts import (
    _provisional_composition_contract_gap as _provisional_composition_contract_gap,
)
from vfx_harness.agents.builder.verdicts import _provisional_decisions_for_layer as _provisional_decisions_for_layer
from vfx_harness.agents.builder.verdicts import _required_claims_at as _required_claims_at
from vfx_harness.agents.builder.verdicts import _stash_motion_strip as _stash_motion_strip
from vfx_harness.agents.builder.verdicts import _uncovered_judge_frame_verdict as _uncovered_judge_frame_verdict
from vfx_harness.agents.builder.verdicts import _worklist_evidence as _worklist_evidence
from vfx_harness.agents.builder.verdicts import composed_group_plans as composed_group_plans
from vfx_harness.agents.builder.verify import _verify_script as _verify_script
from vfx_harness.application.preflight import model_phase_failure as model_phase_failure
from vfx_harness.domain.critic_verdict import evaluate_critic_scores
from vfx_harness.infrastructure.config import Settings as Settings
from vfx_harness.observability import costlog as costlog
from vfx_harness.observability import transcript as transcript
from vfx_harness.observability.log import TOOL_USE as TOOL_USE
from vfx_harness.observability.log import reset_tool_use as reset_tool_use
from vfx_harness.observability.log import tool_use_summary as tool_use_summary

_verdict = evaluate_critic_scores
