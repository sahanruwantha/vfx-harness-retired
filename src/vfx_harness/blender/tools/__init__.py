"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from vfx_harness.blender.tools.guards import (
    _black_search_stop_message as _black_search_stop_message,
)
from vfx_harness.blender.tools.guards import (
    _candidate_for_proposed_check as _candidate_for_proposed_check,
)
from vfx_harness.blender.tools.guards import (
    _closed_density_repeat_message as _closed_density_repeat_message,
)
from vfx_harness.blender.tools.guards import (
    _compare_image as _compare_image,
)
from vfx_harness.blender.tools.guards import (
    _pending_black_frame_probe as _pending_black_frame_probe,
)
from vfx_harness.blender.tools.guards import (
    _probe_values_with_original as _probe_values_with_original,
)
from vfx_harness.blender.tools.guards import (
    _render_setting_writes as _render_setting_writes,
)
from vfx_harness.blender.tools.guards import (
    _run_bpy_instrument_hint as _run_bpy_instrument_hint,
)
from vfx_harness.blender.tools.guards import (
    _schedule_override_without_keying as _schedule_override_without_keying,
)
from vfx_harness.blender.tools.guards import (
    _scoped_renderer_write_error as _scoped_renderer_write_error,
)
from vfx_harness.blender.tools.guards import (
    _to_display_size as _to_display_size,
)
from vfx_harness.blender.tools.guards import (
    _to_metric_size as _to_metric_size,
)
from vfx_harness.blender.tools.images import (
    JPEG_QUALITY as JPEG_QUALITY,
)
from vfx_harness.blender.tools.images import (
    MAX_IMAGE_WIDTH as MAX_IMAGE_WIDTH,
)
from vfx_harness.blender.tools.images import (
    _b64 as _b64,
)
from vfx_harness.blender.tools.images import (
    _image as _image,
)
from vfx_harness.blender.tools.images import (
    _load as _load,
)
from vfx_harness.blender.tools.images import (
    _metrics_line as _metrics_line,
)
from vfx_harness.blender.tools.images import (
    _region_metrics as _region_metrics,
)
from vfx_harness.blender.tools.images import (
    _stats as _stats,
)
from vfx_harness.blender.tools.images import (
    encode_jpeg as encode_jpeg,
)
from vfx_harness.blender.tools.images import (
    exposure_summary as exposure_summary,
)
from vfx_harness.blender.tools.images import (
    image_content as image_content,
)
from vfx_harness.blender.tools.images import (
    load_image as load_image,
)
from vfx_harness.blender.tools.images import (
    reference_metrics_summary as reference_metrics_summary,
)
from vfx_harness.blender.tools.images import (
    region_metrics as region_metrics,
)
from vfx_harness.blender.tools.images import (
    subtract_png as subtract_png,
)
from vfx_harness.blender.tools.mcp import (
    build_blender_tools as build_blender_tools,
)
from vfx_harness.blender.tools.payment import (
    _capture_image_artifact as _capture_image_artifact,
)
from vfx_harness.blender.tools.payment import (
    _merge_worklist_items as _merge_worklist_items,
)
from vfx_harness.blender.tools.payment import (
    _parent_chain_hash as _parent_chain_hash,
)
from vfx_harness.blender.tools.payment import (
    _payment_eligible_candidate as _payment_eligible_candidate,
)
from vfx_harness.blender.tools.payment import (
    _refresh_unpaid_image_debts as _refresh_unpaid_image_debts,
)
from vfx_harness.blender.tools.payment import (
    _run_bpy_write_family_error as _run_bpy_write_family_error,
)
from vfx_harness.blender.tools.payment import (
    _sha256_file as _sha256_file,
)
from vfx_harness.blender.tools.payment import (
    _text as _text,
)
from vfx_harness.blender.tools.payment import (
    capture_image_adversaries as capture_image_adversaries,
)
from vfx_harness.blender.tools.reports import (
    _DISPLAY_H as _DISPLAY_H,
)
from vfx_harness.blender.tools.reports import (
    _LOOK_METRICS as _LOOK_METRICS,
)
from vfx_harness.blender.tools.reports import (
    _METRIC_H as _METRIC_H,
)
from vfx_harness.blender.tools.reports import (
    _ROLE_MANIFEST as _ROLE_MANIFEST,
)
from vfx_harness.blender.tools.reports import (
    _SHEET_MAX_W as _SHEET_MAX_W,
)
from vfx_harness.blender.tools.reports import (
    CANNOT_EXPRESS_DESCRIPTION as CANNOT_EXPRESS_DESCRIPTION,
)
from vfx_harness.blender.tools.reports import (
    CANNOT_EXPRESS_SCHEMA as CANNOT_EXPRESS_SCHEMA,
)
from vfx_harness.blender.tools.reports import (
    SERVER_NAME as SERVER_NAME,
)
from vfx_harness.blender.tools.reports import (
    _bound_static_frames as _bound_static_frames,
)
from vfx_harness.blender.tools.reports import (
    _check_args_error as _check_args_error,
)
from vfx_harness.blender.tools.reports import (
    _check_report as _check_report,
)
from vfx_harness.blender.tools.reports import (
    _comparison_lock_error as _comparison_lock_error,
)
from vfx_harness.blender.tools.reports import (
    _comparison_mode_scale as _comparison_mode_scale,
)
from vfx_harness.blender.tools.reports import (
    _deferred_subject_forecast_note as _deferred_subject_forecast_note,
)
from vfx_harness.blender.tools.reports import (
    _image_evidence_ids_at_frame as _image_evidence_ids_at_frame,
)
from vfx_harness.blender.tools.reports import (
    _layer_feedback_policy as _layer_feedback_policy,
)
from vfx_harness.blender.tools.reports import (
    _object_or_role_error as _object_or_role_error,
)
from vfx_harness.blender.tools.reports import (
    _pixel_contract_gate as _pixel_contract_gate,
)
from vfx_harness.blender.tools.reports import (
    _role_in_scope as _role_in_scope,
)
from vfx_harness.blender.tools.reports import (
    _scene_completion_state as _scene_completion_state,
)
from vfx_harness.blender.tools.reports import (
    _scope_offenders as _scope_offenders,
)
from vfx_harness.blender.tools.reports import (
    _unpaid_image_debt_note as _unpaid_image_debt_note,
)
from vfx_harness.blender.tools.reports import (
    _warn_suffix as _warn_suffix,
)
from vfx_harness.blender.tools.reports import (
    followup_after_image_gate_pass as followup_after_image_gate_pass,
)
from vfx_harness.blender.tools.reports import (
    followup_after_scene_contracts_pass as followup_after_scene_contracts_pass,
)
from vfx_harness.blender.tools.reports import (
    preview_render_mode as preview_render_mode,
)
from vfx_harness.blender.tools.reports import (
    record_cannot_express as record_cannot_express,
)
