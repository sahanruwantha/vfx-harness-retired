from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.unit.test_layer_finalization_state import (
    _finalize,
    _layer,
)
from tests.unit.test_layer_finalization_state import (
    _lower_boundary_receipt_authority as _lower_boundary_receipt_authority,
)
from tests.unit.test_layer_publication import _selected, _write_projections
from tests.unit.test_plan_records import _candidate
from vfx_harness.domain.layer_finalizations import LayerFinalizationReceipt
from vfx_harness.evaluation import determinism
from vfx_harness.evaluation.layer_publications import (
    EvaluationLayerPublicationConflict,
    capture_exact_layer_publications,
    receipt_backed_layer_prefix,
    require_exact_layer_publications,
)
from vfx_harness.evaluation.plan_gate import hierarchical
from vfx_harness.orchestration import unit_state
from vfx_harness.orchestration.layer_publication import LayerPublicationConflict
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.unit_state_lock import unit_state_path

pytestmark = pytest.mark.usefixtures("_lower_boundary_receipt_authority")


@pytest.fixture(autouse=True)
def _selected_prefix_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these publication tests below selected-DAG integration."""

    monkeypatch.setattr(
        "vfx_harness.orchestration.layer_publication."
        "selected_finalization_predecessor_layers",
        lambda *_args, **_kwargs: (),
    )


def _replace_terminal_receipt(
    folder,
    layer,
    receipt,
) -> LayerFinalizationReceipt:
    projection = json.loads(json.dumps(receipt.projection))
    projection["ablation"]["note"] = "independently valid replacement receipt"
    replacement = LayerFinalizationReceipt.mint(
        evaluation_receipt=receipt.evaluation_receipt,
        evaluation_receipt_locator=receipt.evaluation_receipt_locator,
        evaluation_receipt_sha256=receipt.evaluation_receipt_sha256,
        projection=projection,
        completed_at="2026-09-01T10:04:00+00:00",
    )
    state = unit_state.load(folder, layer.id)
    state["layer_finalization"]["terminal_receipt"] = replacement.as_dict()
    unit_state._write(unit_state_path(folder, layer.id), state)
    _write_projections(folder, replacement)
    return replacement


def test_evaluation_prefix_does_not_promote_ledger_or_script_without_receipt(
    tmp_path,
) -> None:
    layer = _layer()
    script = tmp_path / layer.script
    script.parent.mkdir(parents=True)
    script.write_text("# unclaimed layer script\n", encoding="utf-8")
    (tmp_path / "shot.json").write_text(
        json.dumps(
            {
                "milestones": {
                    layer.id: {
                        "status": "passed",
                        "script": layer.script,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert receipt_backed_layer_prefix(tmp_path, (layer,), _selected()) == ()


def test_evaluation_prefix_refuses_stale_receipt_source(tmp_path) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    (tmp_path / layer.script).write_text(
        "# substituted after terminal finalization\n",
        encoding="utf-8",
    )

    with pytest.raises(
        LayerPublicationConflict,
        match="terminal layer replay group 0 input 0 bytes changed",
    ):
        receipt_backed_layer_prefix(tmp_path, (layer,), _selected())


def test_exact_evaluation_publication_rejects_valid_a_to_b_replacement(
    tmp_path,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    selected = _selected()
    expected = capture_exact_layer_publications(
        tmp_path,
        (layer,),
        selected,
    )

    replacement = _replace_terminal_receipt(
        tmp_path,
        layer,
        receipt,
    )
    assert replacement.receipt_digest != receipt.receipt_digest
    # B is independently valid; the post-check must still reject it because replay
    # began against A rather than merely asking whether anything valid exists now.
    assert capture_exact_layer_publications(tmp_path, (layer,), selected)[
        0
    ].finalization_receipt_digest == replacement.receipt_digest

    with pytest.raises(
        EvaluationLayerPublicationConflict,
        match="exact publication identity changed during evaluation",
    ):
        require_exact_layer_publications(
            tmp_path,
            (layer,),
            selected,
            expected=expected,
        )


def test_replay_equivalence_rejects_receipt_replacement_during_blender_work(
    tmp_path,
    monkeypatch,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    selected = SimpleNamespace(plan=object(), selection_token=_selected().selection_token)
    rendered = tmp_path / "replayed.png"
    rendered.write_bytes(b"fixture image")
    replaced = False

    class ReplacingSession:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, _source):
            nonlocal replaced
            if not replaced:
                _replace_terminal_receipt(
                    tmp_path,
                    layer,
                    receipt,
                )
                replaced = True
            return {}

        def inspect(self, _kind):
            return "stable scene"

        def call(self, *_args, **_kwargs):
            return {"scene": {"objects": 1}}

        def render(self, **_kwargs):
            return str(rendered)

    monkeypatch.setattr(determinism, "resolve_selected_authority", lambda _root: selected)
    monkeypatch.setattr(
        determinism,
        "accepted_prefix",
        lambda *_args, **_kwargs: ([layer], 1),
    )
    monkeypatch.setattr(determinism, "BlenderSession", ReplacingSession)
    monkeypatch.setattr(determinism, "_preamble", lambda _shot: "# preamble")
    monkeypatch.setattr(
        determinism,
        "_run_artifact_script",
        lambda _session, _path: None,
    )
    monkeypatch.setattr(determinism, "look_vector", lambda _path: {"mean": 1.0})

    result = determinism.replay_equivalence(
        SimpleNamespace(folder=tmp_path),
        passes=2,
    )

    assert replaced
    assert result.ok is None
    assert "publication changed while replay equivalence ran" in result.detail


def test_determinism_prefix_uses_selected_dag_and_receipt_publication(
    tmp_path,
    monkeypatch,
) -> None:
    layer, _claim_guard, _replay, _stored, receipt = _finalize(tmp_path)
    _write_projections(tmp_path, receipt)
    selected = SimpleNamespace(plan=object(), selection_token=_selected().selection_token)
    monkeypatch.setattr(
        determinism,
        "selected_layer_chain",
        lambda *_args, **_kwargs: (layer,),
    )

    prefix, total = determinism.accepted_prefix(
        SimpleNamespace(folder=tmp_path),
        selected_authority=selected,
    )

    assert prefix == [layer]
    assert total == 1


def test_determinism_prefix_excludes_missing_receipt_despite_passed_ledger(
    tmp_path,
    monkeypatch,
) -> None:
    layer = _layer()
    script = tmp_path / layer.script
    script.parent.mkdir(parents=True)
    script.write_text("# unclaimed layer script\n", encoding="utf-8")
    (tmp_path / "shot.json").write_text(
        json.dumps({"milestones": {layer.id: {"status": "passed"}}}),
        encoding="utf-8",
    )
    selected = SimpleNamespace(plan=object(), selection_token=_selected().selection_token)
    monkeypatch.setattr(
        determinism,
        "selected_layer_chain",
        lambda *_args, **_kwargs: (layer,),
    )

    prefix, total = determinism.accepted_prefix(
        SimpleNamespace(folder=tmp_path),
        selected_authority=selected,
    )

    assert prefix == []
    assert total == 1


def test_hierarchy_gate_uses_exact_selected_layer_publication_prefix(
    tmp_path,
    monkeypatch,
) -> None:
    _candidate(tmp_path)
    (tmp_path / "brief.md").write_text(
        "---\nid: evaluation-publication-fixture\nframes: 240\nfps: 24\n---\n"
        "Final image must hold unchanged.\n",
        encoding="utf-8",
    )
    layer = load_layers_from_path(tmp_path / "layers.json")["1"]
    selected = SimpleNamespace(plan=object(), selection_token=_selected().selection_token)
    context = hierarchical._SelectedPublicationContext(
        shot_folder=tmp_path,
        selected_authority=selected,
        layers=(layer,),
    )
    observed = []
    monkeypatch.setattr(
        hierarchical,
        "_selected_publication_context",
        lambda _folder: context,
    )

    def missing_receipt(folder, layers, selected_authority):
        observed.append((folder, layers, selected_authority))
        return ()

    monkeypatch.setattr(
        hierarchical,
        "receipt_backed_layer_prefix",
        missing_receipt,
    )
    (tmp_path / "shot.json").write_text(
        json.dumps({"milestones": {"1": {"status": "passed"}}}),
        encoding="utf-8",
    )

    _findings, stats = hierarchical._check_hierarchical_plans(tmp_path)

    assert observed == [(tmp_path, [layer], selected)]
    assert stats["layers_passed"] == 0


def test_hierarchy_gate_blocks_a_stale_selected_publication(
    tmp_path,
    monkeypatch,
) -> None:
    _candidate(tmp_path)
    (tmp_path / "brief.md").write_text(
        "---\nid: evaluation-publication-fixture\nframes: 240\nfps: 24\n---\n"
        "Final image must hold unchanged.\n",
        encoding="utf-8",
    )
    layer = load_layers_from_path(tmp_path / "layers.json")["1"]
    selected = SimpleNamespace(plan=object(), selection_token=_selected().selection_token)
    monkeypatch.setattr(
        hierarchical,
        "_selected_publication_context",
        lambda _folder: hierarchical._SelectedPublicationContext(
            shot_folder=tmp_path,
            selected_authority=selected,
            layers=(layer,),
        ),
    )

    def stale_publication(*_args, **_kwargs):
        raise EvaluationLayerPublicationConflict(
            "1",
            ValueError("terminal receipt source changed"),
            published_count=0,
        )

    monkeypatch.setattr(
        hierarchical,
        "receipt_backed_layer_prefix",
        stale_publication,
    )

    findings, stats = hierarchical._check_hierarchical_plans(tmp_path)

    assert stats == {"unit_plans_required": 0, "layers_passed": 0}
    assert any(
        finding.blocking
        and finding.where == "plans/outcomes/layer-31.json"
        and "receipt-backed publication" in finding.what
        for finding in findings
    )


def test_builder_and_plan_gate_import_cleanly_in_fresh_interpreter() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import vfx_harness.agents.builder; "
            "import vfx_harness.orchestration.jit_materialization; "
            "import vfx_harness.evaluation.plan_gate",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "circular import" not in completed.stderr
