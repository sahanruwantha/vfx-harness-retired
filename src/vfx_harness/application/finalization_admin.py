"""Reviewed administrative transactions for layer-finalization claims."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vfx_harness.domain.brief import load_shot
from vfx_harness.orchestration.authority_selection import (
    SelectedAuthorityResolutionError,
    resolve_selected_authority,
)
from vfx_harness.orchestration.builder_execution_fence import (
    BuilderExecutionFenceActive,
    BuilderExecutionFenceError,
    builder_execution_fence,
)
from vfx_harness.orchestration.layer_finalization_release import (
    LayerFinalizationReleaseConflict,
    release_active_layer_finalization,
)
from vfx_harness.orchestration.ledger import load_layers


def _release(args: argparse.Namespace) -> int:
    """Release one reviewed orphan claim without reopening any work unit."""

    shot = load_shot(args.folder)
    layer_id = str(args.layer)
    try:
        with builder_execution_fence(shot.folder) as fence_lease:
            selected = resolve_selected_authority(shot.folder)
            layers = load_layers(shot, selected_authority=selected)
            try:
                layer = layers[layer_id]
            except KeyError as exc:
                raise SystemExit(f"unknown layer {layer_id!r}") from exc
            stored = release_active_layer_finalization(
                shot.folder,
                layer,
                claim_id=str(args.claim_id),
                selected_authority=selected,
                reason=str(args.reason),
                evidence=tuple(args.evidence),
                fence_lease=fence_lease,
            )
    except BuilderExecutionFenceActive as exc:
        raise SystemExit(
            "layer-finalization release refused because a live builder still owns "
            "the shot; prove that process exited before reviewed release"
        ) from exc
    except BuilderExecutionFenceError as exc:
        raise SystemExit(
            "layer-finalization release could not prove an exclusive execution "
            f"fence: {exc}"
        ) from exc
    except SelectedAuthorityResolutionError as exc:
        raise SystemExit(
            f"layer-finalization release cannot resolve selected authority: {exc}"
        ) from exc
    except LayerFinalizationReleaseConflict as exc:
        raise SystemExit(f"layer-finalization release refused: {exc}") from exc
    print(
        json.dumps(
            {
                "release_receipt": stored.receipt.as_dict(),
                "locator": stored.locator,
                "sha256": stored.sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vfx finalizations",
        description=__doc__,
    )
    commands = parser.add_subparsers(dest="action", required=True)
    release = commands.add_parser(
        "release",
        help=(
            "release one exact orphaned pre-terminal claim after independent review"
        ),
    )
    release.add_argument("folder", type=Path, help="shot folder")
    release.add_argument("--layer", required=True, help="layer id")
    release.add_argument(
        "--claim-id",
        required=True,
        help="exact active layer-finalization claim id",
    )
    release.add_argument(
        "--reason",
        required=True,
        help="reviewed reason the orphaned attempt may be abandoned",
    )
    release.add_argument(
        "--evidence",
        action="append",
        required=True,
        help=(
            "existing shot-relative evidence file; repeat for every reviewed input"
        ),
    )
    release.set_defaults(handler=_release)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
