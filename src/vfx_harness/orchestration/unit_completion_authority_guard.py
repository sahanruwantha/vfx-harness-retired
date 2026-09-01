"""Live-head guard for typed work-unit completion authorizations."""

from __future__ import annotations

from pathlib import Path

from vfx_harness.domain.authority_head_records import parse_authority_selection_token
from vfx_harness.orchestration.authority_selection_heads import (
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.authority_state_context import (
    resolve_current_authority_state,
)
from vfx_harness.orchestration.unit_completion_authorizations import (
    AuthorizedUnitCompletionSet,
    UnitCompletionAuthorizationError,
)


def require_current_unit_completion_authorization(
    folder: str | Path,
    authorization: AuthorizedUnitCompletionSet,
    *,
    selection_token: AuthoritySelectionToken | None = None,
) -> None:
    """Verify one live attestation while its caller holds the selection lock.

    The state-owning caller must then validate the attestation's completion projection
    while holding the corresponding state lock.  Splitting those two checks would let
    an authority-state head or passed receipt change between authorization and use.
    """

    if not isinstance(authorization, AuthorizedUnitCompletionSet):
        raise UnitCompletionAuthorizationError(
            "live work-unit completion requires current coordinator authorization"
        )
    observed = read_authority_selection_heads(folder).token
    if selection_token is not None:
        require_matching_authority_selection_token(selection_token, observed)
    projection = parse_authority_selection_token(
        observed.to_dict(),
        "live work-unit completion authority selection",
    )
    if authorization.selection_token != projection:
        raise UnitCompletionAuthorizationError(
            "work-unit completion authorization belongs to another selection"
        )
    context = resolve_current_authority_state(folder)
    if context is None or context.head_ref != authorization.authority_state_head_ref:
        raise UnitCompletionAuthorizationError(
            "work-unit completion authorization belongs to another authority-state head"
        )


__all__ = ["require_current_unit_completion_authorization"]
