"""Strict schema-4 layer, work-unit, claim, and evidence contracts.

The planner owns shot-specific decomposition.  This package owns only the generic shape,
validation, dependency semantics, claim authority, and protection resolution.  There is
deliberately no adapter for the former top-level array schema: execution authority must
not be guessed from an obsolete document.
"""

from vfx_harness.domain.claim_bindings import bound_claim_contract_ids as bound_claim_contract_ids
from vfx_harness.domain.evidence_kinds import PROJECTED_ORIGIN_KINDS as PROJECTED_ORIGIN_KINDS
from vfx_harness.domain.work_units.authoring import (
    NAMESPACE_RELATIVE_STAGING_KEYS as NAMESPACE_RELATIVE_STAGING_KEYS,
)
from vfx_harness.domain.work_units.authoring import (
    clustered_mutation_dialect as clustered_mutation_dialect,
)
from vfx_harness.domain.work_units.authoring import (
    compile_clustered_mutation as compile_clustered_mutation,
)
from vfx_harness.domain.work_units.authoring import (
    compile_clustered_mutation_roles as compile_clustered_mutation_roles,
)
from vfx_harness.domain.work_units.authoring import work_unit_authoring_schema as work_unit_authoring_schema
from vfx_harness.domain.work_units.capabilities import (
    CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE as CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
)
from vfx_harness.domain.work_units.capabilities import (
    DEFERRED_CONTRACT_CONTEXT_RULE as DEFERRED_CONTRACT_CONTEXT_RULE,
)
from vfx_harness.domain.work_units.capabilities import (
    DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE as DEFERRED_CONTRACT_FRAME_AUTHORITY_RULE,
)
from vfx_harness.domain.work_units.capabilities import (
    DEFERRED_SUBJECT_ACTIVATION_RULE as DEFERRED_SUBJECT_ACTIVATION_RULE,
)
from vfx_harness.domain.work_units.capabilities import DEFERRED_SUBJECT_BBOX_KINDS as DEFERRED_SUBJECT_BBOX_KINDS
from vfx_harness.domain.work_units.capabilities import GLOBAL_SCENE_CAPABILITIES as GLOBAL_SCENE_CAPABILITIES
from vfx_harness.domain.work_units.capabilities import GRANT_REQUIRED_CAPABILITIES as GRANT_REQUIRED_CAPABILITIES
from vfx_harness.domain.work_units.capabilities import LAYER_EXCLUSIVE_CAPABILITIES as LAYER_EXCLUSIVE_CAPABILITIES
from vfx_harness.domain.work_units.capabilities import UNIT_PROVIDES as UNIT_PROVIDES
from vfx_harness.domain.work_units.capabilities import DeferredSubjectActivationGap as DeferredSubjectActivationGap
from vfx_harness.domain.work_units.capabilities import allowed_unit_provides as allowed_unit_provides
from vfx_harness.domain.work_units.capabilities import (
    compile_deferred_subject_activation as compile_deferred_subject_activation,
)
from vfx_harness.domain.work_units.capabilities import (
    deferred_subject_activation_gaps as deferred_subject_activation_gaps,
)
from vfx_harness.domain.work_units.capabilities import (
    extra_reserved_roles_on_camera_layer as extra_reserved_roles_on_camera_layer,
)
from vfx_harness.domain.work_units.capabilities import sparse_layer_dependencies as sparse_layer_dependencies
from vfx_harness.domain.work_units.capabilities import (
    strict_topological_sparse_layer_ids as strict_topological_sparse_layer_ids,
)
from vfx_harness.domain.work_units.capabilities import topological_sparse_layer_ids as topological_sparse_layer_ids
from vfx_harness.domain.work_units.claims import Claim as Claim
from vfx_harness.domain.work_units.claims import CompositionContext as CompositionContext
from vfx_harness.domain.work_units.claims import EvaluationPolicy as EvaluationPolicy
from vfx_harness.domain.work_units.claims import EvidenceBinding as EvidenceBinding
from vfx_harness.domain.work_units.claims import JudgePoint as JudgePoint
from vfx_harness.domain.work_units.claims import MutationScope as MutationScope
from vfx_harness.domain.work_units.claims import ProtectionSpec as ProtectionSpec
from vfx_harness.domain.work_units.claims import validate_qualification as validate_qualification
from vfx_harness.domain.work_units.evidence_domains import CLAIM_DOMAINS as CLAIM_DOMAINS
from vfx_harness.domain.work_units.evidence_domains import EVIDENCE_DOMAINS as EVIDENCE_DOMAINS
from vfx_harness.domain.work_units.evidence_domains import (
    REQUIREMENT_DOMAIN_COVERAGE_FIX as REQUIREMENT_DOMAIN_COVERAGE_FIX,
)
from vfx_harness.domain.work_units.evidence_domains import STRUCTURAL_CLAIM_DOMAINS as STRUCTURAL_CLAIM_DOMAINS
from vfx_harness.domain.work_units.evidence_domains import (
    layers_covering_evidence_domains as layers_covering_evidence_domains,
)
from vfx_harness.domain.work_units.evidence_domains import parse_evidence_domains as parse_evidence_domains
from vfx_harness.domain.work_units.evidence_domains import (
    requirement_domain_coverage_what as requirement_domain_coverage_what,
)
from vfx_harness.domain.work_units.evidence_domains import uncovered_evidence_domains as uncovered_evidence_domains
from vfx_harness.domain.work_units.frames import EXTRA_FRAME_BINDING_RULE as EXTRA_FRAME_BINDING_RULE
from vfx_harness.domain.work_units.frames import LAYER_JUDGE_CLAIM_COVERAGE_RULE as LAYER_JUDGE_CLAIM_COVERAGE_RULE
from vfx_harness.domain.work_units.frames import LOOK_IMAGE_EVIDENCE_KINDS as LOOK_IMAGE_EVIDENCE_KINDS
from vfx_harness.domain.work_units.frames import LOOK_REQUIRES_IMAGE_DOMAIN_RULE as LOOK_REQUIRES_IMAGE_DOMAIN_RULE
from vfx_harness.domain.work_units.frames import UNIT_JUDGE_CLAIM_COVERAGE_RULE as UNIT_JUDGE_CLAIM_COVERAGE_RULE
from vfx_harness.domain.work_units.frames import compile_frame_authority as compile_frame_authority
from vfx_harness.domain.work_units.frames import composed_evaluation_is_lookless as composed_evaluation_is_lookless
from vfx_harness.domain.work_units.frames import layer_judge_frames as layer_judge_frames
from vfx_harness.domain.work_units.frames import required_claim_certifies_look as required_claim_certifies_look
from vfx_harness.domain.work_units.frames import uncovered_layer_judge_frames as uncovered_layer_judge_frames
from vfx_harness.domain.work_units.frames import uncovered_unit_judge_frames as uncovered_unit_judge_frames
from vfx_harness.domain.work_units.frames import unearned_look_judge_frames as unearned_look_judge_frames
from vfx_harness.domain.work_units.graph import DeferredClaimBindingGap as DeferredClaimBindingGap
from vfx_harness.domain.work_units.graph import consumption_is_satisfied as consumption_is_satisfied
from vfx_harness.domain.work_units.graph import deferred_claim_binding_gaps as deferred_claim_binding_gaps
from vfx_harness.domain.work_units.graph import dependency_ordered_units as dependency_ordered_units
from vfx_harness.domain.work_units.graph import offered_interface_keys as offered_interface_keys
from vfx_harness.domain.work_units.graph import ready_units as ready_units
from vfx_harness.domain.work_units.parsing import CLAIM_AUTHORITIES as CLAIM_AUTHORITIES
from vfx_harness.domain.work_units.parsing import CLAIM_KINDS as CLAIM_KINDS
from vfx_harness.domain.work_units.parsing import EVIDENCE_KINDS as EVIDENCE_KINDS
from vfx_harness.domain.work_units.parsing import SCHEMA as SCHEMA
from vfx_harness.domain.work_units.parsing import TEMPORAL_EVIDENCE as TEMPORAL_EVIDENCE
from vfx_harness.domain.work_units.parsing import UNIT_STATES as UNIT_STATES
from vfx_harness.domain.work_units.parsing import _id as _id
from vfx_harness.domain.work_units.parsing import _mapping as _mapping
from vfx_harness.domain.work_units.parsing import _relative_path as _relative_path
from vfx_harness.domain.work_units.parsing import _strings as _strings
from vfx_harness.domain.work_units.parsing import _text as _text
from vfx_harness.domain.work_units.parsing import canonical_unit_script_path as canonical_unit_script_path
from vfx_harness.domain.work_units.projection import PROJECTED_ORIGIN_REPAIR_RULE as PROJECTED_ORIGIN_REPAIR_RULE
from vfx_harness.domain.work_units.projection import PointProjectionInterfaceGap as PointProjectionInterfaceGap
from vfx_harness.domain.work_units.projection import point_projection_interface_gaps as point_projection_interface_gaps
from vfx_harness.domain.work_units.projection import (
    unit_requires_surface_visibility as unit_requires_surface_visibility,
)
from vfx_harness.domain.work_units.unit import ATOMICITY_PADDING_FIELDS as ATOMICITY_PADDING_FIELDS
from vfx_harness.domain.work_units.unit import CONSUME_INTERFACE_RULE as CONSUME_INTERFACE_RULE
from vfx_harness.domain.work_units.unit import CONSUMED_ROLE_MUTATION_RULE as CONSUMED_ROLE_MUTATION_RULE
from vfx_harness.domain.work_units.unit import LOOK_CAPABILITIES as LOOK_CAPABILITIES
from vfx_harness.domain.work_units.unit import ConsumeSpec as ConsumeSpec
from vfx_harness.domain.work_units.unit import PublishSpec as PublishSpec
from vfx_harness.domain.work_units.unit import WorkUnit as WorkUnit
from vfx_harness.domain.work_units.unit import parse_look_capabilities as parse_look_capabilities
from vfx_harness.domain.work_units.unit import parse_provides as parse_provides
from vfx_harness.domain.work_units.unit import read_document as read_document
from vfx_harness.domain.work_units.unit import validate_unit_dag as validate_unit_dag
from vfx_harness.domain.work_units.unit import validate_unit_script_path as validate_unit_script_path
from vfx_harness.domain.work_units.visibility import GEOMETRY_VIS_CYCLE_RULE as GEOMETRY_VIS_CYCLE_RULE
from vfx_harness.domain.work_units.visibility import GEOMETRY_VIS_DEPENDENCY_RULE as GEOMETRY_VIS_DEPENDENCY_RULE
from vfx_harness.domain.work_units.visibility import VIS_REPAIR_OWNER_RULE as VIS_REPAIR_OWNER_RULE
from vfx_harness.domain.work_units.visibility import GeometryVisDependencyCycle as GeometryVisDependencyCycle
from vfx_harness.domain.work_units.visibility import GeometryVisDependencyGap as GeometryVisDependencyGap
from vfx_harness.domain.work_units.visibility import geometry_vis_dependency_cycles as geometry_vis_dependency_cycles
from vfx_harness.domain.work_units.visibility import geometry_vis_dependency_gaps as geometry_vis_dependency_gaps
from vfx_harness.domain.work_units.visibility import geometry_vis_protection_ids as geometry_vis_protection_ids
from vfx_harness.domain.work_units.visibility import (
    geometry_vis_protection_ids_for_unit as geometry_vis_protection_ids_for_unit,
)
from vfx_harness.domain.work_units.visibility import (
    layer_active_visible_fraction_ids as layer_active_visible_fraction_ids,
)
from vfx_harness.domain.work_units.visibility import plan_selector_declared as plan_selector_declared
from vfx_harness.domain.work_units.visibility import vis_roles_unrepairable_by as vis_roles_unrepairable_by
from vfx_harness.domain.work_units.visibility import visible_fraction_repair_owners as visible_fraction_repair_owners
