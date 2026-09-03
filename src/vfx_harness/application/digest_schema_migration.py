"""Public deterministic migration of prior digest-generation work-unit state.

`vfx migrate-digest-schema <shot>` republishes the selected JIT view under the next
revision through the ordinary authority-state transaction. Every layer whose durable
state binds a prior digest generation migrates as an `incomparable` effect: its units
and terminal receipt are archived with a typed reason and fresh pending state binds the
current generation. No model, Blender, render, or critic work runs (HIR-0182).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.authority_head_records import canonical_json_bytes
from vfx_harness.orchestration.authority_selection import resolve_selected_authority_from_heads
from vfx_harness.orchestration.authority_selection_heads import read_authority_selection_heads
from vfx_harness.orchestration.authority_selection_transaction import authority_selection_lock
from vfx_harness.orchestration.authority_state_live_members import authority_state_layer_ids
from vfx_harness.orchestration.authority_state_transaction import (
    commit_prepared_authority_state_transition_locked,
)
from vfx_harness.orchestration.jit_materialization.transition import (
    prepare_selected_view_republication_locked,
)
from vfx_harness.orchestration.unit_state_identity import DIGEST_SCHEMA
from vfx_harness.orchestration.unit_state_queries import load_snapshot

DIGEST_GENERATION_MIGRATION_SCHEMA = "vfx-harness.digest-generation-migration/v1"


@dataclass(frozen=True, slots=True)
class DigestGenerationMigrationResult:
    disposition: str
    digest_schema: int
    migrated_layers: tuple[str, ...]
    jit_revision: int
    view_hash: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": "vfx-harness.digest-generation-migration-result/v1",
            "disposition": self.disposition,
            "digest_schema": self.digest_schema,
            "migrated_layers": list(self.migrated_layers),
            "jit_revision": self.jit_revision,
            "view_hash": self.view_hash,
        }


def _is_prior_generation(shot: Path, layer_id: str) -> bool:
    state, payload = load_snapshot(shot, layer_id)
    if payload is None or not state:
        return False
    return int(state.get("digest_schema", 0)) != DIGEST_SCHEMA


def migrate_digest_schema(shot_folder: str | Path) -> DigestGenerationMigrationResult:
    """Migrate every prior-generation layer state, or prove there is nothing to migrate."""

    shot = Path(shot_folder).expanduser().absolute()
    with authority_selection_lock(shot, exclusive=True):
        heads = read_authority_selection_heads(shot)
        prior = tuple(
            layer_id for layer_id in authority_state_layer_ids(shot) if _is_prior_generation(shot, layer_id)
        )
        if not prior:
            return DigestGenerationMigrationResult(
                disposition="current",
                digest_schema=DIGEST_SCHEMA,
                migrated_layers=(),
                jit_revision=heads.token.jit_revision,
                view_hash=None if heads.jit is None else heads.jit.view_hash,
            )
        if heads.jit is None:
            raise ValueError(
                "prior-generation work-unit state exists without a selected JIT view; "
                "publish the layer view before migrating"
            )
        live = resolve_selected_authority_from_heads(shot, heads)
        producer = {
            "schema": DIGEST_GENERATION_MIGRATION_SCHEMA,
            "to_digest_schema": DIGEST_SCHEMA,
            "layers": list(prior),
            "view_hash": heads.jit.view_hash,
            "jit_revision": heads.token.jit_revision,
        }
        producer_payload = canonical_json_bytes(producer)
        publication = prepare_selected_view_republication_locked(
            shot,
            heads=heads,
            selected_before=live,
            producer_payload=producer_payload,
            producer_schema=DIGEST_GENERATION_MIGRATION_SCHEMA,
            producer_digest=hashlib.sha256(producer_payload).hexdigest(),
        )
        assert publication.transition is not None
        commit_prepared_authority_state_transition_locked(shot, publication.transition)
        verified = read_authority_selection_heads(shot)
        if verified.jit != publication.pointer:
            raise ValueError("digest migration did not select its republished JIT pointer")
        stale = [layer_id for layer_id in prior if _is_prior_generation(shot, layer_id)]
        if stale:
            raise ValueError(
                "digest migration committed but layer(s) " + ", ".join(stale) + " remain prior generation"
            )
        return DigestGenerationMigrationResult(
            disposition="migrated",
            digest_schema=DIGEST_SCHEMA,
            migrated_layers=prior,
            jit_revision=verified.token.jit_revision,
            view_hash=publication.pointer.view_hash,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vfx migrate-digest-schema",
        description=(
            "Republish the selected view through the authority-state transaction so "
            "prior digest-generation work-unit state migrates with a typed reason."
        ),
    )
    parser.add_argument("shot", type=Path)
    args = parser.parse_args(argv)
    result = migrate_digest_schema(args.shot)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


__all__ = ["DigestGenerationMigrationResult", "main", "migrate_digest_schema"]


if __name__ == "__main__":
    raise SystemExit(main())
