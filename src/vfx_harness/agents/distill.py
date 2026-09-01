"""Fail-closed entry point for the retired legacy recipe distiller.

Legacy distillation queue rows do not bind an immutable accepted unit-completion
receipt, canonical script digest, or authority selection.  Consuming them after a
model call could therefore publish knowledge from revoked work.  The command stays
present only so existing operational wrappers receive a deterministic refusal.
"""

from __future__ import annotations

import argparse
from pathlib import Path

DISTILLATION_RETIRED = (
    "recipe distillation is disabled: legacy queue rows are not bound to an "
    "immutable accepted unit-completion receipt; a receipt-bound staged diff and "
    "post-spend authority revalidation are required before recipe publication"
)


async def _run(_folder: Path, *, keep: bool = False) -> None:
    """Refuse the legacy consumer before reading a queue or spending model budget."""

    del keep
    raise RuntimeError(DISTILLATION_RETIRED)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retired legacy recipe-distillation queue consumer"
    )
    parser.add_argument("folder", type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.parse_args()
    raise SystemExit(DISTILLATION_RETIRED)


if __name__ == "__main__":
    main()
