from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import nullcontext
from types import SimpleNamespace
from typing import TypeVar

import anyio
import pytest

from vfx_harness.agents.builder import critic as critic_runtime
from vfx_harness.agents.builder import verify as verify_runtime
from vfx_harness.agents.builder.attempt_guard import AttemptBoundBlenderSession
from vfx_harness.agents.builder.authority import AuthorityBoundLedger
from vfx_harness.agents.builder.execution_guard import (
    ExecutionAuthorityLost,
    LedgerPublicationScope,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.ledger import Ledger, Milestone

_T = TypeVar("_T")


class _LayerShapedGuard:
    """Structural fixture deliberately exposing no unit or unit-attempt claim."""

    label = "layer 3 finalization"

    def __init__(self) -> None:
        self.authority_binding = {
            "layer_finalization_claim": {
                "schema": "fixture-layer-finalization-claim/v1",
                "claim_id": "layer-finalization-3-1",
                "layer_id": "3",
                "layer_script_path": "build/layer_3.py",
            }
        }
        self.events: list[tuple[str, str]] = []

    def check(self, operation: str) -> object:
        self.events.append(("check", operation))
        return self.authority_binding["layer_finalization_claim"]

    @property
    def ledger_publication_scope(self) -> LedgerPublicationScope:
        return LedgerPublicationScope(
            kind="layer_finalization_claim",
            milestone_id="3",
            claim_id="layer-finalization-3-1",
        )

    def publish(self, operation: str, mutation: Callable[[], _T]) -> _T:
        self.events.append(("publish", operation))
        return mutation()


def _shot(tmp_path) -> Shot:
    return Shot(
        folder=tmp_path,
        frontmatter={"id": "execution-guard-fixture", "frames": 1, "fps": 24},
        body="fixture",
    )


def test_blender_session_accepts_structural_execution_guard() -> None:
    guard = _LayerShapedGuard()

    class _Session:
        @staticmethod
        def render(*, frame: int) -> str:
            return f"render-{frame}"

    session = AttemptBoundBlenderSession(_Session(), guard)

    assert session.render(frame=7) == "render-7"
    assert guard.events == [
        ("check", "start Blender session operation render"),
        ("check", "finish Blender session operation render"),
    ]


def test_ledger_accepts_and_binds_structural_execution_guard(
    tmp_path,
    monkeypatch,
) -> None:
    guard = _LayerShapedGuard()
    selected = SimpleNamespace(
        selection_token=AuthoritySelectionToken(0, None, 0, None),
    )
    captured: list[dict] = []
    original_prepare = Ledger.prepare_save

    def capture_prepare(self, *, authority_binding=None):
        captured.append(json.loads(authority_binding))
        return original_prepare(self, authority_binding=authority_binding)

    monkeypatch.setattr(Ledger, "prepare_save", capture_prepare)
    ledger = AuthorityBoundLedger(
        _shot(tmp_path),
        selected,
        execution_guard=guard,
    )
    ledger._slot(Milestone("3", 1, "refs/layer.png", "fixture")).update(
        {
            "status": "in_progress",
            "script": "build/layer_3.py",
            "layer_finalization_claim": "layer-finalization-3-1",
        }
    )
    assert ledger.save() is None

    assert captured == [
        {
            "schema": "vfx-harness.builder-ledger-publication-binding/v1",
            "selection_token": selected.selection_token.to_dict(),
            **guard.authority_binding,
        }
    ]
    assert guard.events == [
        ("check", "start builder ledger publication staging"),
        ("publish", "builder ledger publication"),
    ]


def _layer_claim_ledger(tmp_path, guard: _LayerShapedGuard) -> AuthorityBoundLedger:
    selected = SimpleNamespace(
        selection_token=AuthoritySelectionToken(0, None, 0, None),
    )
    ledger = AuthorityBoundLedger(
        _shot(tmp_path),
        selected,
        execution_guard=guard,
    )
    ledger._slot(Milestone("3", 1, "refs/layer.png", "fixture")).update(
        {
            "status": "in_progress",
            "script": "build/layer_3.py",
            "layer_finalization_claim": "layer-finalization-3-1",
        }
    )
    return ledger


def test_builder_ledger_refuses_guard_that_skips_exact_mutation(tmp_path) -> None:
    class _SkippingGuard(_LayerShapedGuard):
        def publish(self, operation: str, mutation: Callable[[], _T]) -> None:
            del mutation
            self.events.append(("publish", operation))

    ledger = _layer_claim_ledger(tmp_path, _SkippingGuard())

    with pytest.raises(ValueError, match="invoke and complete its exact mutation once"):
        ledger.save()

    assert not (tmp_path / "shot.json").exists()
    assert list(tmp_path.glob(".shot.json.prepared.*")) == []


def test_builder_ledger_refuses_guard_that_invokes_exact_mutation_twice(
    tmp_path,
) -> None:
    class _DoubleGuard(_LayerShapedGuard):
        def publish(self, operation: str, mutation: Callable[[], _T]) -> _T:
            self.events.append(("publish", operation))
            mutation()
            return mutation()

    ledger = _layer_claim_ledger(tmp_path, _DoubleGuard())

    with pytest.raises(ValueError, match="invoked its exact mutation more than once"):
        ledger.save()

    assert (tmp_path / "shot.json").is_file()
    assert list(tmp_path.glob(".shot.json.prepared.*")) == []


def test_verify_script_uses_guard_label_without_unit_access(
    tmp_path,
    monkeypatch,
) -> None:
    guard = _LayerShapedGuard()
    script_rel = "build/03_composed.py"
    script = tmp_path / script_rel
    script.parent.mkdir(parents=True)
    script.write_text("# fixture\n", encoding="utf-8")
    package = verify_runtime.builder_package()
    monkeypatch.setattr(package, "_preamble", lambda _shot: "")
    monkeypatch.setattr(package, "_run_prior_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(package, "_scene_object_manifest", lambda _session: {})
    monkeypatch.setattr(package, "_render_evidence", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(verify_runtime, "_run_artifact_script", lambda *_args: None)
    monkeypatch.setattr(
        verify_runtime,
        "_unit_requires_raster",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(verify_runtime, "_unit_raster_mode", lambda _unit: "none")
    monkeypatch.setattr(verify_runtime, "_layer_needs_motion", lambda _layer: False)
    monkeypatch.setattr(
        verify_runtime,
        "_persist_contract_gaps",
        lambda *_args, **_kwargs: None,
    )

    async def judge(*_args, **_kwargs):
        return {"pass": True, "mean": 5.0, "scores": {}, "issues": []}

    monkeypatch.setattr(verify_runtime, "_judge_unit_or_layer", judge)

    class _Session:
        @staticmethod
        def run(*_args, **_kwargs):
            return {"result": {}}

    class _Ledger:
        rounds = 0

        def record_round(self, *_args, **_kwargs) -> None:
            self.rounds += 1

    ledger = _Ledger()
    shot = SimpleNamespace(
        folder=tmp_path,
        frontmatter={"type": "still"},
        frames=1,
    )
    layer = SimpleNamespace(
        id="3",
        judges=((1, "refs/f001.png"),),
        temporal_evidence="none",
    )

    result = anyio.run(
        lambda: verify_runtime._verify_script(
            shot,
            Milestone("3", 1, "refs/f001.png", "fixture"),
            script_rel,
            [],
            _Session(),
            [],
            ledger,
            False,
            layer=layer,
            execution_guard=guard,
        )
    )

    assert result == "passed"
    assert ledger.rounds == 1
    assert guard.events == [
        ("check", "score canonical layer 3 finalization at frame 1"),
        ("check", "consume canonical layer 3 finalization score at frame 1"),
    ]


def test_critic_does_not_retry_a_generic_execution_authority_loss(
    tmp_path,
    monkeypatch,
) -> None:
    class _RevokedLayerGuard(_LayerShapedGuard):
        def check(self, operation: str) -> object:
            self.events.append(("check", operation))
            raise ExecutionAuthorityLost("fixture layer claim was revoked")

    guard = _RevokedLayerGuard()
    (tmp_path / "refs").mkdir()
    (tmp_path / "renders").mkdir()
    (tmp_path / "refs/f001.png").write_bytes(b"reference")
    (tmp_path / "renders/candidate.png").write_bytes(b"candidate")
    monkeypatch.setattr(critic_runtime, "critic_prompt", lambda *_args, **_kwargs: "fixture")
    monkeypatch.setattr(critic_runtime, "_image_block", lambda _path: {"type": "image"})
    monkeypatch.setattr(critic_runtime, "_focus_references", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        critic_runtime,
        "_claim_context",
        lambda *_args, **_kwargs: ([], {}, set()),
    )
    monkeypatch.setattr(
        critic_runtime.costlog,
        "scoped",
        lambda **_kwargs: nullcontext(),
    )
    shot = SimpleNamespace(
        folder=tmp_path,
        frontmatter={"type": "still"},
        frames=1,
    )

    async def critique() -> None:
        await critic_runtime._critique(
            shot,
            Milestone("3", 1, "refs/f001.png", "fixture"),
            "renders/candidate.png",
            [("form", "fixture")],
            SimpleNamespace(),
            False,
            selected_authority=SimpleNamespace(),
            execution_guard=guard,
        )

    with pytest.raises(ExecutionAuthorityLost, match="fixture layer claim was revoked"):
        anyio.run(critique)

    assert guard.events == [
        ("check", "query observer critic for layer 3 finalization at frame 1"),
    ]
