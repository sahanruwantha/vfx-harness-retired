"""Strict parsing of the two revisioned authority-selection heads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.authority_head_records import (
    JIT_CURRENT_PATH,
    AuthorityHeadRecordError,
    decode_canonical_json_object,
)
from vfx_harness.orchestration import plan_bundle_integrity
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
    authority_selection_token_from_pointer_bytes,
    read_optional_pointer_bytes,
)
from vfx_harness.orchestration.jit_materialization.view_pointer import (
    JitViewPointer,
    JitViewPointerError,
    parse_jit_view_pointer,
    require_live_jit_artifact_locators,
)
from vfx_harness.orchestration.plan_pointer import PLAN_POINTER_PATH, PlanPointer


class AuthoritySelectionHeadError(ValueError):
    """One named selection head is malformed or schema-incompatible."""

    def __init__(self, head: str, message: str) -> None:
        super().__init__(message)
        self.head = head


@dataclass(frozen=True, slots=True)
class AuthoritySelectionHeads:
    """Exact parsed plan/JIT pointer bytes observed while the caller holds the lock."""

    token: AuthoritySelectionToken
    plan: PlanPointer | None
    jit: JitViewPointer | None
    plan_pointer_bytes: bytes | None
    jit_pointer_bytes: bytes | None


def read_authority_selection_heads(
    shot_folder: str | Path,
) -> AuthoritySelectionHeads:
    """Read both pointer records exactly once; the caller owns lock lifetime."""

    shot = Path(shot_folder).expanduser().resolve()
    plan_bytes = read_optional_pointer_bytes(
        shot,
        shot / PLAN_POINTER_PATH,
    )
    jit_bytes = read_optional_pointer_bytes(
        shot,
        shot / JIT_CURRENT_PATH,
    )
    plan = None
    if plan_bytes is not None:
        try:
            plan = PlanPointer.from_dict(
                decode_canonical_json_object(
                    plan_bytes,
                    "selected plan pointer",
                )
            )
        except (
            AuthorityHeadRecordError,
            plan_bundle_integrity.PlanPublicationError,
        ) as exc:
            raise AuthoritySelectionHeadError("plan", str(exc)) from exc
    jit = None
    if jit_bytes is not None:
        try:
            jit = parse_jit_view_pointer(
                decode_canonical_json_object(
                    jit_bytes,
                    "selected JIT pointer",
                )
            )
            require_live_jit_artifact_locators(jit)
        except (
            JitViewPointerError,
            AuthorityHeadRecordError,
            plan_bundle_integrity.PlanPublicationError,
        ) as exc:
            raise AuthoritySelectionHeadError("jit", str(exc)) from exc
    if plan is None and jit is not None:
        raise AuthoritySelectionConflict(
            "selected JIT pointer exists without a selected global plan pointer"
        )
    token = authority_selection_token_from_pointer_bytes(
        plan_revision=0 if plan is None else plan.revision,
        plan_pointer_bytes=plan_bytes,
        jit_revision=0 if jit is None else jit.revision,
        jit_pointer_bytes=jit_bytes,
    )
    return AuthoritySelectionHeads(
        token=token,
        plan=plan,
        jit=jit,
        plan_pointer_bytes=plan_bytes,
        jit_pointer_bytes=jit_bytes,
    )
