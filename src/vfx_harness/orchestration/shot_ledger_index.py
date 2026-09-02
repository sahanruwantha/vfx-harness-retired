"""Opaque derived shot-ledger index carried from the derivation writer to the transport.

``shot.json`` reserves its ``accepted_build`` member for a ``vfx-harness.shot-ledger/v2``
value that only the derivation writer computes.  The transport accepts a replacement
of that member solely as this opaque type, so no caller can hand-author, copy, or
edit accepted rows into the canonical ledger.
"""

from __future__ import annotations

from typing import Any

from vfx_harness.domain.shot_ledger_v2 import ShotLedgerV2

ACCEPTED_BUILD_KEY = "accepted_build"
_MINT_KEY = object()


class DerivedShotLedgerIndex:
    """One exact derived index; minted only by the shot-ledger derivation writer."""

    __slots__ = ("_ledger",)

    def __init__(self, ledger: ShotLedgerV2, *, key: object) -> None:
        if key is not _MINT_KEY:
            raise TypeError(
                "DerivedShotLedgerIndex is minted only by the shot-ledger derivation writer"
            )
        if not isinstance(ledger, ShotLedgerV2):
            raise TypeError("DerivedShotLedgerIndex requires a ShotLedgerV2 value")
        self._ledger = ledger

    @property
    def ledger(self) -> ShotLedgerV2:
        return self._ledger

    def as_dict(self) -> dict[str, Any]:
        return self._ledger.as_dict()

    def __copy__(self) -> DerivedShotLedgerIndex:
        raise TypeError("DerivedShotLedgerIndex cannot be copied; derive it again")

    def __deepcopy__(self, _memo: dict[int, Any]) -> DerivedShotLedgerIndex:
        raise TypeError("DerivedShotLedgerIndex cannot be copied; derive it again")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("DerivedShotLedgerIndex cannot be pickled; derive it again")

    def __repr__(self) -> str:
        return f"DerivedShotLedgerIndex(index_digest={self._ledger.index_digest!r})"


def mint_derived_shot_ledger_index(ledger: ShotLedgerV2) -> DerivedShotLedgerIndex:
    """Mint the opaque index; the derivation writer is its only legal caller."""

    return DerivedShotLedgerIndex(ledger, key=_MINT_KEY)


__all__ = [
    "ACCEPTED_BUILD_KEY",
    "DerivedShotLedgerIndex",
    "mint_derived_shot_ledger_index",
]
