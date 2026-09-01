"""Selection-first guard for exact-attempt state publications."""

from __future__ import annotations

from functools import wraps

from vfx_harness.domain.unit_attempts import UnitAttemptClaim
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    AuthoritySelectionToken,
    authority_selection_lock,
    require_matching_authority_selection_token,
)


def _claim_token(claim: UnitAttemptClaim) -> AuthoritySelectionToken:
    token = claim.selection_token
    return AuthoritySelectionToken(
        plan_revision=token.plan_revision,
        plan_pointer_sha256=token.plan_pointer_sha256,
        jit_revision=token.jit_revision,
        jit_pointer_sha256=token.jit_pointer_sha256,
    )


def selected_attempt_state_mutation(mutation):
    """Guard an attempt-bearing call with selection SH before its state lock.

    The wrapped mutation must itself carry the serialized state decorator. Calls
    without an attempt remain available only for non-spend migration transitions.
    """

    @wraps(mutation)
    def guarded(folder, layer_id, *args, **kwargs):
        supplied_attempt = kwargs.get("attempt")
        preserves_accepted = kwargs.get("preserve_accepted_source") is True
        if supplied_attempt is None and not preserves_accepted:
            return mutation(folder, layer_id, *args, **kwargs)
        supplied_token = kwargs.get("selection_token")
        if not isinstance(supplied_token, AuthoritySelectionToken):
            raise AuthoritySelectionConflict(
                "attempt-bound state mutation requires an exact typed authority "
                "selection token"
            )
        expected_token = supplied_token
        if supplied_attempt is not None:
            claim = (
                supplied_attempt
                if isinstance(supplied_attempt, UnitAttemptClaim)
                else UnitAttemptClaim.parse(
                    supplied_attempt,
                    "attempt-bound state mutation claim",
                )
            )
            expected_token = _claim_token(claim)
            require_matching_authority_selection_token(expected_token, supplied_token)
        with authority_selection_lock(folder, exclusive=False):
            # Lazy module import breaks authority-heads -> JIT publish -> unit-state.
            from vfx_harness.orchestration import (  # noqa: PLC0415
                authority_selection_heads,
            )

            observed = authority_selection_heads.read_authority_selection_heads(folder).token
            require_matching_authority_selection_token(expected_token, observed)
            return mutation(folder, layer_id, *args, **kwargs)

    return guarded
