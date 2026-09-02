"""Public opaque capability types for exact prepared-file transactions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class PreparedPublicationRegistryError(RuntimeError):
    """An opaque prepared-publication capability is absent or no longer live."""


_PUBLIC_RECORD_FIELDS = frozenset(
    {
        "shot",
        "relative_path",
        "destination",
        "destination_parent",
        "destination_parent_identity",
        "destination_name",
        "predecessor",
        "predecessor_sha256",
        "temporary",
        "temporary_name",
        "temporary_identity",
        "payload_sha256",
        "lock_parent",
        "lock_parent_identity",
        "lock_name",
        "authority_binding",
    }
)
_INSPECT: Callable[[PreparedFilePublication], Any] | None = None


class PreparedFilePublication:
    """Opaque exact-object capability for one live prepared replacement."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: Any, **_kwargs: Any) -> PreparedFilePublication:
        raise PreparedPublicationRegistryError(
            "prepared file publications can be minted only by prepare_file_update"
        )

    def __getattr__(self, name: str) -> Any:
        if _INSPECT is None:  # pragma: no cover - import initialization invariant
            raise PreparedPublicationRegistryError("prepared file publication registry is not initialized")
        if name == "lock_identity":
            identity = _INSPECT(self).lock_identity
            return (identity.device, identity.inode)
        if name in _PUBLIC_RECORD_FIELDS:
            return getattr(_INSPECT(self), name)
        raise AttributeError(name)

    def __copy__(self) -> PreparedFilePublication:
        raise PreparedPublicationRegistryError("prepared file publication capabilities cannot be copied")

    def __deepcopy__(self, _memo: dict[int, Any]) -> PreparedFilePublication:
        raise PreparedPublicationRegistryError("prepared file publication capabilities cannot be copied")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise PreparedPublicationRegistryError("prepared file publication capabilities cannot be serialized")

    def __repr__(self) -> str:
        return "<PreparedFilePublication opaque>"


class PreparedFilePayloadVerification:
    """Opaque, single-generation proof for one exact live publication."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: Any, **_kwargs: Any) -> PreparedFilePayloadVerification:
        raise PreparedPublicationRegistryError(
            "prepared payload verification can be minted only from a live staged inode"
        )

    def __copy__(self) -> PreparedFilePayloadVerification:
        raise PreparedPublicationRegistryError("prepared payload verification capabilities cannot be copied")

    def __deepcopy__(self, _memo: dict[int, Any]) -> PreparedFilePayloadVerification:
        raise PreparedPublicationRegistryError("prepared payload verification capabilities cannot be copied")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise PreparedPublicationRegistryError("prepared payload verification capabilities cannot be serialized")

    def __repr__(self) -> str:
        return "<PreparedFilePayloadVerification opaque>"


def bind_publication_inspector(inspector: Callable[[PreparedFilePublication], Any]) -> None:
    """Bind the sole registry reader during module initialization."""

    global _INSPECT

    if _INSPECT is not None:  # pragma: no cover - import initialization invariant
        raise RuntimeError("prepared publication inspector is already bound")
    _INSPECT = inspector


__all__ = [
    "PreparedFilePayloadVerification",
    "PreparedFilePublication",
    "PreparedPublicationRegistryError",
]
