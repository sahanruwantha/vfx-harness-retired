from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import anyio

import vfx_harness.agents.planner.rematerialize as rematerialize
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)


def test_rematerialization_verifies_state_under_selected_authority_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The published head stays pinned while its capsule and durable DAG are joined."""

    events: list[str] = []
    locks: list[str] = []
    digest = "c" * 64
    base_token = AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256=hashlib.sha256(b"plan").hexdigest(),
        jit_revision=0,
        jit_pointer_sha256=None,
    )
    published_token = AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256=base_token.plan_pointer_sha256,
        jit_revision=1,
        jit_pointer_sha256=hashlib.sha256(b"jit").hexdigest(),
    )
    bundle = SimpleNamespace(root=tmp_path, content_hash="b" * 64)
    base_authority = SimpleNamespace(
        plan=SimpleNamespace(bundle=bundle),
        selection_token=base_token,
    )
    published_authority = SimpleNamespace(
        plan=SimpleNamespace(bundle=bundle),
        selection_token=published_token,
    )
    old_unit = SimpleNamespace(id="old")
    new_unit = SimpleNamespace(id="new")
    old_layer = SimpleNamespace(id="2", execution="ready", stages=(old_unit,))
    deferred_layer = SimpleNamespace(id="2", execution="jit_deferred", stages=())
    refreshed_layer = SimpleNamespace(id="2", execution="ready", stages=(new_unit,))

    selections = iter((base_authority, published_authority))
    monkeypatch.setattr(
        rematerialize,
        "resolve_selected_authority",
        lambda _folder: next(selections),
    )

    async def materialize(*_args, **_kwargs) -> None:
        events.append("publish")

    package = SimpleNamespace(
        load_layers=lambda _shot, **kwargs: {
            "2": (
                old_layer
                if kwargs["selected_authority"] is base_authority
                else refreshed_layer
            )
        },
        load_layers_from_path=lambda _path: {"2": deferred_layer},
        _materialize_deferred_layer=materialize,
    )
    monkeypatch.setattr(rematerialize, "planner_package", lambda: package)
    monkeypatch.setattr(rematerialize, "revert_materialization", lambda *_a, **_k: None)

    @contextmanager
    def selection_lock(_folder, *, exclusive):
        assert exclusive is False
        assert locks == []
        locks.append("selection")
        events.append("selection_enter")
        try:
            yield
        finally:
            assert locks.pop() == "selection"
            events.append("selection_exit")

    @contextmanager
    def state_lock(_folder, layer_id, *, exclusive):
        assert layer_id == "2"
        assert exclusive is False
        assert locks == ["selection"]
        locks.append("state")
        events.append("state_enter")
        try:
            yield
        finally:
            assert locks.pop() == "state"
            events.append("state_exit")

    monkeypatch.setattr(rematerialize, "authority_selection_lock", selection_lock)
    monkeypatch.setattr(rematerialize, "unit_state_lock", state_lock)
    monkeypatch.setattr(
        rematerialize,
        "read_authority_selection_heads",
        lambda _folder: events.append("head_read")
        or SimpleNamespace(token=published_token),
    )

    def require_token(expected, observed) -> None:
        assert locks == ["selection"]
        assert expected == observed == published_token
        events.append("head_required")

    monkeypatch.setattr(
        rematerialize,
        "require_matching_authority_selection_token",
        require_token,
    )

    def capsule_digest(_folder, layer_id, authority) -> str:
        assert locks == ["selection"]
        assert layer_id == "2"
        assert authority is published_authority
        events.append("capsule")
        return digest

    monkeypatch.setattr(
        rematerialize,
        "selected_layer_capsule_digest",
        capsule_digest,
    )

    state_reads = iter(({}, {"units": {}, "plan_hash": digest}))

    def load_state(_folder, layer_id):
        assert layer_id == "2"
        value = next(state_reads)
        if value:
            assert locks == ["selection", "state"]
            events.append("state_read")
        return value

    def validate_state(value, layer_id, units) -> None:
        assert locks == ["selection", "state"]
        assert value["plan_hash"] == digest
        assert layer_id == "2"
        assert units == refreshed_layer.stages
        events.append("dag_validated")

    monkeypatch.setattr(rematerialize.unit_state, "load", load_state)
    monkeypatch.setattr(rematerialize.unit_state, "validate_current", validate_state)

    shot = SimpleNamespace(folder=tmp_path)

    async def invoke():
        return await rematerialize._rematerialize_layer(
            shot,
            old_layer,
            ("operator", "replacement", ["evidence"], False),
            model="model",
            blender="blender",
            max_turns=4,
        )

    assert anyio.run(invoke) is refreshed_layer
    assert events == [
        "publish",
        "selection_enter",
        "head_read",
        "head_required",
        "capsule",
        "state_enter",
        "state_read",
        "dag_validated",
        "state_exit",
        "selection_exit",
    ]
