"""Public deterministic recovery boundary for a selected authority-state WAL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vfx_harness.domain.authority_state_records import AuthorityStateRecoveryResult
from vfx_harness.orchestration.authority_state_recovery import (
    recover_pending_authority_state,
)


def recover_authority_state(
    shot_folder: str | Path,
) -> AuthorityStateRecoveryResult:
    """Roll forward an exact pending intent, or prove the current exact head."""

    observation = recover_pending_authority_state(shot_folder)
    context = observation.context
    result = AuthorityStateRecoveryResult(
        disposition=observation.disposition,
        transaction_id=context.proposal.transaction_id,
        transition_revision=context.head.revision,
        intent_ref=context.commit.intent_ref,
        coordinator_head_ref=context.head_ref,
        selection_token=context.head.selection_token,
        state_member_ids=tuple(
            member.layer_id for member in context.intent.state_members
        ),
        accepted_build_projection=observation.accepted_build,
    )
    if result.transition_revision != context.intent.transition_revision:
        raise ValueError(
            "authority-state recovery result revision differs from its transition intent"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vfx recover-authority-state",
        description=(
            "Deterministically roll a selected authority-state WAL forward to its "
            "sole staged successor."
        ),
    )
    parser.add_argument("shot", type=Path)
    args = parser.parse_args(argv)
    result = recover_authority_state(args.shot)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


__all__ = ["main", "recover_authority_state"]


if __name__ == "__main__":
    raise SystemExit(main())
