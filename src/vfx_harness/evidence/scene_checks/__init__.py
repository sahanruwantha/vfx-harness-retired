"""Authoritative Blender-state and cross-layer interface contracts.

``scene_checks.json`` is a strict schema-2 document. Contracts address objects,
materials, shader controls and compositor nodes by semantic custom properties, never by
datablock names. Object selectors distinguish ``bvfx_role`` from ``bvfx_control`` so a
planner cannot put control ids in a role field and publish an unresolvable contract. Their
lifecycle decides which prior-layer guarantees remain active for the layer currently being
built.
"""

from vfx_harness.evidence.scene_checks.deferred_subject import (
    DeferredSubjectCompositionPaymentGap as DeferredSubjectCompositionPaymentGap,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    _selector_overlap as _selector_overlap,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    deferred_subject_composition_activation_ids as deferred_subject_composition_activation_ids,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    deferred_subject_composition_forecast_ids_for_unit as deferred_subject_composition_forecast_ids_for_unit,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    deferred_subject_composition_ids as deferred_subject_composition_ids,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    deferred_subject_composition_ids_for_unit as deferred_subject_composition_ids_for_unit,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    deferred_subject_composition_payment_gaps as deferred_subject_composition_payment_gaps,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    deferred_subject_sharing_for_unit as deferred_subject_sharing_for_unit,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    deferred_subject_union_producers as deferred_subject_union_producers,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    deferred_subject_union_slack as deferred_subject_union_slack,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    irreversible_deferred_subject_forecast_failures as irreversible_deferred_subject_forecast_failures,
)
from vfx_harness.evidence.scene_checks.deferred_subject import (
    load_rows as load_rows,
)
from vfx_harness.evidence.scene_checks.functional import (
    _control_script as _control_script,
)
from vfx_harness.evidence.scene_checks.functional import (
    functional_evidence as functional_evidence,
)
from vfx_harness.evidence.scene_checks.functional import (
    layer_evidence as layer_evidence,
)
from vfx_harness.evidence.scene_checks.functional import (
    prior_interface_evidence as prior_interface_evidence,
)
from vfx_harness.evidence.scene_checks.functional import (
    prior_interface_rows as prior_interface_rows,
)
from vfx_harness.evidence.scene_checks.kinds import (
    _MEASURED_PROPERTY as _MEASURED_PROPERTY,
)
from vfx_harness.evidence.scene_checks.kinds import (
    _PROJECTED_KINDS as _PROJECTED_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    BBOX_KINDS as BBOX_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    CAMERA_REQUIRED_KINDS as CAMERA_REQUIRED_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    FRAME_SCOPED_KINDS as FRAME_SCOPED_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    FUNCTIONAL_KINDS as FUNCTIONAL_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    KEYFRAME_SCHEDULE_PATH_MISS_OVER_HI as KEYFRAME_SCHEDULE_PATH_MISS_OVER_HI,
)
from vfx_harness.evidence.scene_checks.kinds import (
    KIND_DEFINITIONS as KIND_DEFINITIONS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    KIND_DOMAINS as KIND_DOMAINS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    KNOWN_ROW_KEYS as KNOWN_ROW_KEYS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    MATERIAL_KINDS as MATERIAL_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    NODE_KINDS as NODE_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    OBJECT_KINDS as OBJECT_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    OPERATOR_FIELDS as OPERATOR_FIELDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    PATH_CLEARANCE_UNMEASURED as PATH_CLEARANCE_UNMEASURED,
)
from vfx_harness.evidence.scene_checks.kinds import (
    PROJECTED_CONTEXT_KINDS as PROJECTED_CONTEXT_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    PROJECTED_ORIGIN_KINDS as PROJECTED_ORIGIN_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    STATE_KINDS as STATE_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    SUBJECT_COMPOSITION_RULE as SUBJECT_COMPOSITION_RULE,
)
from vfx_harness.evidence.scene_checks.kinds import (
    SUPPORTED_KINDS as SUPPORTED_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    SUPPORTED_OPS as SUPPORTED_OPS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    SURFACE_PROJECTED_KINDS as SURFACE_PROJECTED_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    TEMPORAL_KINDS as TEMPORAL_KINDS,
)
from vfx_harness.evidence.scene_checks.kinds import (
    VACUOUS_NORMALIZED_BAND_SPAN as VACUOUS_NORMALIZED_BAND_SPAN,
)
from vfx_harness.evidence.scene_checks.kinds import (
    WINDOW_KINDS as WINDOW_KINDS,
)
from vfx_harness.evidence.scene_checks.probe import (
    _blender_probe as _blender_probe,
)
from vfx_harness.evidence.scene_checks.probe import (
    _evidence as _evidence,
)
from vfx_harness.evidence.scene_checks.row_sets import (
    derivative_bound_contradictions as derivative_bound_contradictions,
)
from vfx_harness.evidence.scene_checks.row_sets import (
    namespace_count_contradictions as namespace_count_contradictions,
)
from vfx_harness.evidence.scene_checks.row_sets import (
    schedule_smoothness_contradictions as schedule_smoothness_contradictions,
)
from vfx_harness.evidence.scene_checks.row_sets import (
    validate_row_set as validate_row_set,
)
from vfx_harness.evidence.scene_checks.validate import (
    _argmax_span as _argmax_span,
)
from vfx_harness.evidence.scene_checks.validate import (
    _coalesce_adjacent as _coalesce_adjacent,
)
from vfx_harness.evidence.scene_checks.validate import (
    _derivative_segments as _derivative_segments,
)
from vfx_harness.evidence.scene_checks.validate import (
    _holds as _holds,
)
from vfx_harness.evidence.scene_checks.validate import (
    _optional_hi as _optional_hi,
)
from vfx_harness.evidence.scene_checks.validate import (
    _sample_vector as _sample_vector,
)
from vfx_harness.evidence.scene_checks.validate import (
    _selectors as _selectors,
)
from vfx_harness.evidence.scene_checks.validate import (
    _target as _target,
)
from vfx_harness.evidence.scene_checks.validate import (
    curve_derivative_note as curve_derivative_note,
)
from vfx_harness.evidence.scene_checks.validate import (
    data_block_carriers as data_block_carriers,
)
from vfx_harness.evidence.scene_checks.validate import (
    keyframe_schedule_matching_frames as keyframe_schedule_matching_frames,
)
from vfx_harness.evidence.scene_checks.validate import (
    keyframe_schedule_miss_note as keyframe_schedule_miss_note,
)
from vfx_harness.evidence.scene_checks.validate import (
    keyframe_schedule_path_aliases as keyframe_schedule_path_aliases,
)
from vfx_harness.evidence.scene_checks.validate import (
    keyframe_schedule_path_miss_value as keyframe_schedule_path_miss_value,
)
from vfx_harness.evidence.scene_checks.validate import (
    keyframe_schedule_present_paths as keyframe_schedule_present_paths,
)
from vfx_harness.evidence.scene_checks.validate import (
    schedule_derivative_floor as schedule_derivative_floor,
)
from vfx_harness.evidence.scene_checks.validate import (
    validate_row as validate_row,
)
from vfx_harness.evidence.scene_checks.validate import (
    visible_fraction_min as visible_fraction_min,
)
from vfx_harness.evidence.scene_checks.validate import (
    visible_fraction_note as visible_fraction_note,
)
