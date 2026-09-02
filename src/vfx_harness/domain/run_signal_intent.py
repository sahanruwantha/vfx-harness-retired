"""Typed cessation intent recorded by the root owner's signal handler (HIR-0172).

The terminalizer commits an owned interruption only from this record. A free kind
string could be supplied by any caller; the record is minted in exactly one place, the
root owner's signal handler, and an architecture test pins that issuer.
"""

from __future__ import annotations

import signal
from dataclasses import dataclass

SIGNAL_INTERRUPTION_KINDS: dict[int, str] = {
    int(signal.SIGINT): "operator_interrupt",
    int(signal.SIGTERM): "termination_request",
}


@dataclass(frozen=True, slots=True)
class RecordedSignalIntent:
    """The first SIGINT or SIGTERM delivered to a root owner, as recorded."""

    kind: str
    signal_number: int
    recorded_at: str

    def __post_init__(self) -> None:
        expected = SIGNAL_INTERRUPTION_KINDS.get(int(self.signal_number))
        if expected is None:
            raise ValueError(
                f"recorded signal intent supports only {sorted(SIGNAL_INTERRUPTION_KINDS)}, "
                f"found signal {self.signal_number!r}"
            )
        if self.kind != expected:
            raise ValueError(
                f"recorded signal intent kind {self.kind!r} does not name signal "
                f"{self.signal_number} ({expected!r})"
            )
        if not isinstance(self.recorded_at, str) or not self.recorded_at:
            raise ValueError("recorded signal intent requires its recording timestamp")

    @property
    def exit_code(self) -> int:
        return 128 + int(self.signal_number)


__all__ = ["SIGNAL_INTERRUPTION_KINDS", "RecordedSignalIntent"]
