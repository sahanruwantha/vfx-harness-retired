"""`vfx migrate-digest-schema` republishes the selected view and migrates prior state (HIR-0182)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_plan_records import (
    _add_deferred_layer,
    _candidate,
    publish_current,
)
from vfx_harness.application.digest_schema_migration import migrate_digest_schema
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.authority_selection_heads import read_authority_selection_heads
from vfx_harness.orchestration.authority_state_live_members import authority_state_layer_ids
from vfx_harness.orchestration.unit_state_identity import DIGEST_SCHEMA
from vfx_harness.orchestration.unit_state_queries import load, unit_state_path
from vfx_harness.orchestration.unit_state_serialization import serialize_work_unit_state


def test_migration_republishes_the_view_and_supersedes_prior_generation_state(tmp_path: Path) -> None:
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "digest-migration")
    publish_current(tmp_path, layout, outcome="clean_with_deferred")
    (layer_id, *_rest) = authority_state_layer_ids(tmp_path)

    before = migrate_digest_schema(tmp_path)
    assert before.disposition == "current" and before.migrated_layers == ()
    heads_before = read_authority_selection_heads(tmp_path)

    # Injected prior generation: the durable state binds generation-4 digests.
    path = unit_state_path(tmp_path, layer_id)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["digest_schema"] = 4
    state["plan_hash"] = "1" * 64
    for slot in state["units"].values():
        slot["unit_hash"] = "2" * 64
    path.write_bytes(serialize_work_unit_state(state))

    result = migrate_digest_schema(tmp_path)
    assert result.disposition == "migrated"
    assert result.migrated_layers == (layer_id,)
    assert result.digest_schema == DIGEST_SCHEMA
    heads_after = read_authority_selection_heads(tmp_path)
    assert heads_after.token.jit_revision == heads_before.token.jit_revision + 1
    assert heads_after.jit is not None and heads_after.jit.view_hash == heads_before.jit.view_hash
    migrated = load(tmp_path, layer_id)
    assert migrated["digest_schema"] == DIGEST_SCHEMA
    assert migrated["plan_hash"] != "1" * 64
    assert all(slot["status"] == "pending" for slot in migrated["units"].values())
    assert any(
        row.get("superseded_reason", "").startswith("digest generation 4 migrated to 5")
        for row in migrated["superseded"]
    )

    again = migrate_digest_schema(tmp_path)
    assert again.disposition == "current"
