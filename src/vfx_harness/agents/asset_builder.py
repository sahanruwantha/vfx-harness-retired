"""Retired freestanding asset stage.

Image-to-3D is a generate construction route on a mesh-family work unit (ADR-0009,
HIR-0162). ``vfx asset`` fails closed so shot-root ``assets/`` cannot remain a
second authority beside typed construction.

    python -m vfx_harness.agents.asset_builder  # exits with the retirement rule
"""

from __future__ import annotations

from pathlib import Path

from vfx_harness.domain.construction import ASSET_STAGE_RETIRED_RULE


def build_assets(folder: str | Path, *, verbose: bool = True) -> None:
    raise SystemExit(ASSET_STAGE_RETIRED_RULE)


def main() -> None:
    raise SystemExit(ASSET_STAGE_RETIRED_RULE)


if __name__ == "__main__":
    main()
