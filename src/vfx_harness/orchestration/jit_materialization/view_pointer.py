"""Compatibility exports for the dependency-free JIT pointer contract.

The closed pointer parser lives in ``domain.authority_head_records`` so selected-head
resolution does not initialize the JIT materialization package and its publication
pipeline merely to parse an authority record.
"""

from vfx_harness.domain.authority_head_records import (
    JIT_VIEW_POINTER_SCHEMA as VIEW_SCHEMA,
)
from vfx_harness.domain.authority_head_records import (
    JitViewPointer as JitViewPointer,
)
from vfx_harness.domain.authority_head_records import (
    JitViewPointerError as JitViewPointerError,
)
from vfx_harness.domain.authority_head_records import (
    canonical_view_hash as canonical_view_hash,
)
from vfx_harness.domain.authority_head_records import is_sha256 as is_sha256
from vfx_harness.domain.authority_head_records import (
    materialized_layers_from_document as materialized_layers_from_document,
)
from vfx_harness.domain.authority_head_records import (
    parse_jit_view_pointer as parse_jit_view_pointer,
)
from vfx_harness.domain.authority_head_records import (
    require_live_jit_artifact_locators as require_live_jit_artifact_locators,
)
from vfx_harness.domain.authority_head_records import (
    require_materialized_layers_match as require_materialized_layers_match,
)

__all__ = [
    "VIEW_SCHEMA",
    "JitViewPointer",
    "JitViewPointerError",
    "canonical_view_hash",
    "is_sha256",
    "materialized_layers_from_document",
    "parse_jit_view_pointer",
    "require_live_jit_artifact_locators",
    "require_materialized_layers_match",
]
