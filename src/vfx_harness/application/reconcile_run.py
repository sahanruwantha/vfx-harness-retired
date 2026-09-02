"""Public model-free reconciliation of one run whose root owner may have died."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.domain.brief import load_shot
from vfx_harness.orchestration import run_owner_boundary
from vfx_harness.orchestration.run_owner_loss_reconciler import (
    reconcile_lost_run,
    reconciliation_json,
)


class _Clock:
    def __init__(self) -> None:
        self._last = ""

    def __call__(self) -> str:
        value = datetime.now(UTC).isoformat(timespec="microseconds")
        while value <= self._last:
            value = datetime.now(UTC).isoformat(timespec="microseconds")
        self._last = value
        return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vfx reconcile",
        description=(
            "Prove owner loss for one exact run by acquiring its recorded fence, then "
            "publish its action-free interruption receipt; a live owner is left untouched."
        ),
    )
    parser.add_argument("shot", type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    shot = load_shot(args.shot)
    with run_owner_boundary.invocation(
        shot.folder,
        "reconcile",
        shot_id=shot.id,
        parameters={"target_run_id": args.run_id},
    ) as layout:
        result = reconcile_lost_run(shot.folder, args.run_id, reconciler=layout, clock=_Clock())
        layout.terminal_metadata["reconciliation"] = result.as_dict()
    print(reconciliation_json(result))
    return 0


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
