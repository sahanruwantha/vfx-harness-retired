"""Opaque nominal capability types for the plan-consumer view owner."""

from __future__ import annotations

from typing import Any

from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerViewMutationConflict,
)


class PlanConsumerViewMutationCapability:
    """Opaque scope proof for one exact isolated plan-consumer view."""

    __slots__ = ("__weakref__",)

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> PlanConsumerViewMutationCapability:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer view mutation capabilities are issued only by an "
            "isolated-view context"
        )

    def __copy__(self) -> PlanConsumerViewMutationCapability:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer view mutation capabilities cannot be copied"
        )

    def __deepcopy__(
        self,
        _memo: dict[int, Any],
    ) -> PlanConsumerViewMutationCapability:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer view mutation capabilities cannot be copied"
        )

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer view mutation capabilities cannot be serialized"
        )

    def __repr__(self) -> str:
        return "<PlanConsumerViewMutationCapability opaque>"


class PreparedPlanConsumerViewInstallation:
    """Opaque proof that one constructed inode may become the installed view."""

    __slots__ = ("__weakref__",)

    def __new__(
        cls,
        *_args: Any,
        **_kwargs: Any,
    ) -> PreparedPlanConsumerViewInstallation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer installation proofs are minted only from a live "
            "construction capability"
        )

    def __copy__(self) -> PreparedPlanConsumerViewInstallation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer installation proofs cannot be copied"
        )

    def __deepcopy__(
        self,
        _memo: dict[int, Any],
    ) -> PreparedPlanConsumerViewInstallation:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer installation proofs cannot be copied"
        )

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer installation proofs cannot be serialized"
        )

    def __repr__(self) -> str:
        return "<PreparedPlanConsumerViewInstallation opaque>"


__all__ = [
    "PlanConsumerViewMutationCapability",
    "PreparedPlanConsumerViewInstallation",
]
