"""Every finished-chain consumer replays the selected global layer DAG."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import vfx_harness.orchestration.selected_layer_chain as selected_chain
from vfx_harness.agents import acceptance, acceptance_stop
from vfx_harness.agents.builder import prior as builder_prior
from vfx_harness.application import render_shot
from vfx_harness.domain.brief import Shot
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.ledger import Milestone


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def _ready_layer(layer_id: str, script: str) -> dict:
    axis = f"axis_{layer_id}"
    role = f"subject.{layer_id}"
    unit_id = "unit"
    judge = [{"frame": 1, "ref": "refs/M1.png"}]
    return {
        "id": layer_id,
        "script": script,
        "title": layer_id.title(),
        "primary_judge": 1,
        "judge": judge,
        "owns": [axis],
        "reads": f"{layer_id} is present",
        "evidence_domains": ["scene"],
        "execution": "ready",
        "stages": [
            {
                "id": unit_id,
                "title": f"Build {layer_id}",
                "plan": f"plans/{layer_id}/unit.md",
                "depends_on": [],
                "mutates": {
                    "mode": "scoped",
                    "roles": [role],
                    "controls": [],
                    "script_spans": [f"build/units/{layer_id}/{unit_id}.py"],
                },
                "protects": {
                    "selector": "all_active_upstream_interfaces",
                    "resolve_to_explicit_ids_at": "freeze",
                },
                "evaluation": {
                    "primary_judge": 1,
                    "judge": judge,
                    "temporal_evidence": "none",
                    "claims": [
                        {
                            "id": f"{layer_id}-exists",
                            "proposition": f"{layer_id} exists",
                            "axis": axis,
                            "property": "object_count",
                            "subject_roles": [role],
                            "subject_controls": [],
                            "moments": [1],
                            "kind": "atomic",
                            "required": True,
                            "authority": "executable_required",
                            "repair_owner": unit_id,
                            "asserts": "scene",
                            "evidence": [
                                {
                                    "kind": "scene_contract",
                                    "id": f"{layer_id}-exists",
                                }
                            ],
                        }
                    ],
                },
                "completion": "all_required_claims_and_protected_contracts_pass",
                "look_capabilities": [],
            }
        ],
    }


def _sparse_layer(ready: dict, dependencies: list[str]) -> dict:
    role = ready["stages"][0]["mutates"]["roles"][0]
    return {
        **{key: value for key, value in ready.items() if key not in {"execution", "stages"}},
        "execution": "jit_deferred",
        "stages": [],
        "jit": {
            "depends_on_layers": dependencies,
            "required_outcomes": [],
            "provides": {},
            "reserved_roles": [role],
            "owned_requirements": [f"R-{ready['id']}"],
        },
    }


def _selected_dag_fixture(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Shot:
    (root / "refs").mkdir()
    (root / "refs" / "M1.png").write_bytes(b"reference")
    shot = Shot(
        folder=root,
        frontmatter={
            "id": "selected-chain",
            "frames": 1,
            "fps": 24,
            "resolution": [16, 16],
            "engine": "BLENDER_EEVEE_NEXT",
        },
        body="fixture",
    )

    # Authored position, numeric/lexical script order, and executable order conflict:
    # authored = composite, camera, form; scripts = composite, form, camera;
    # selected DAG = camera, form, composite.
    composite = _ready_layer("composite", "build/01_composite.py")
    camera = _ready_layer("camera", "build/30_camera.py")
    form = _ready_layer("form", "build/20_form.py")
    global_root = root / "selected-global"
    executable = root / "selected-executable-layers.json"
    _write(
        global_root / "layers.json",
        {
            "schema": 5,
            "layers": [
                _sparse_layer(composite, ["camera", "form"]),
                _sparse_layer(camera, []),
                _sparse_layer(form, []),
            ],
        },
    )
    _write(executable, {"schema": 5, "layers": [composite, camera, form]})

    for layer in (composite, camera, form):
        layer_script = root / layer["script"]
        layer_script.parent.mkdir(parents=True, exist_ok=True)
        layer_script.write_text(f"# {layer['id']} layer\n", encoding="utf-8")
        unit_script = root / layer["stages"][0]["mutates"]["script_spans"][0]
        unit_script.parent.mkdir(parents=True, exist_ok=True)
        unit_script.write_text(f"# {layer['id']} unit\n", encoding="utf-8")
    _write(
        root / "shot.json",
        {
            "shot": shot.id,
            "milestones": {
                layer_id: {"status": "passed"}
                for layer_id in ("composite", "camera", "form")
            },
        },
    )
    _write(root / "acceptance.json", [])

    bundle = SimpleNamespace(root=global_root, content_hash="a" * 64)
    monkeypatch.setattr(
        selected_chain,
        "resolve_selected_authority",
        lambda _root: SimpleNamespace(
            plan=SimpleNamespace(bundle=bundle),
            assertion=SimpleNamespace(effective_view=SimpleNamespace(digest="b" * 64)),
            artifact_paths={"layers.json": executable},
        ),
    )
    return shot


def test_selected_layer_chain_uses_stable_global_dag_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)

    chain = selected_chain.selected_layer_chain(shot)

    assert [layer.id for layer in chain] == ["camera", "form", "composite"]
    assert [layer.script for layer in chain] == [
        "build/30_camera.py",
        "build/20_form.py",
        "build/01_composite.py",
    ]


def test_builder_prior_prefix_uses_selected_dag_not_script_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    chain = selected_chain.selected_layer_chain(shot)
    (tmp_path / "build" / "00_orphan.py").write_text("# stray\n", encoding="utf-8")

    monkeypatch.setattr(builder_prior, "selected_layer_chain", lambda *_args, **_kwargs: chain)
    monkeypatch.setattr(
        builder_prior.layer_publication,
        "require_current_layer_publication",
        lambda _folder, layer, _selected: SimpleNamespace(
            receipt=SimpleNamespace(
                receipt_digest=_digest(f"{layer.id}:terminal"),
                layer_script_path=layer.script,
                layer_script_sha256=hashlib.sha256(
                    (tmp_path / layer.script).read_bytes()
                ).hexdigest(),
            ),
        ),
    )

    paths = builder_prior._prior_layer_paths(
        shot,
        chain[-1],
        selected_authority=SimpleNamespace(),
    )

    assert [path.relative_to(tmp_path).as_posix() for path in paths] == [
        "build/30_camera.py",
        "build/20_form.py",
    ]


def test_builder_prior_replay_refuses_source_swap_after_publication_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    chain = selected_chain.selected_layer_chain(shot)
    monkeypatch.setattr(builder_prior, "selected_layer_chain", lambda *_args, **_kwargs: chain)

    def publication(_folder, layer, _selected):
        path = tmp_path / layer.script
        return SimpleNamespace(
            receipt=SimpleNamespace(
                receipt_digest=_digest(f"{layer.id}:terminal"),
                layer_script_path=layer.script,
                layer_script_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )

    monkeypatch.setattr(
        builder_prior.layer_publication,
        "require_current_layer_publication",
        publication,
    )
    paths = builder_prior._prior_layer_paths(
        shot,
        chain[-1],
        selected_authority=SimpleNamespace(),
    )
    paths[0].write_text("# substituted after selection\n", encoding="utf-8")

    with pytest.raises(builder_prior.ChainBroken, match="changed before replay"):
        builder_prior._run_prior_paths(
            SimpleNamespace(run=lambda *_args, **_kwargs: pytest.fail("must not execute")),
            paths,
        )


def test_builder_prior_replay_rechecks_terminal_receipts_after_blender_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    chain = selected_chain.selected_layer_chain(shot)
    monkeypatch.setattr(builder_prior, "selected_layer_chain", lambda *_args, **_kwargs: chain)
    receipt_digests = {
        layer.id: _digest(f"{layer.id}:terminal") for layer in chain
    }

    def publication(_folder, layer, _selected):
        path = tmp_path / layer.script
        return SimpleNamespace(
            receipt=SimpleNamespace(
                receipt_digest=receipt_digests[layer.id],
                layer_script_path=layer.script,
                layer_script_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )

    monkeypatch.setattr(
        builder_prior.layer_publication,
        "require_current_layer_publication",
        publication,
    )
    paths = builder_prior._prior_layer_paths(
        shot,
        chain[-1],
        selected_authority=SimpleNamespace(),
    )
    calls = 0

    def run(_source, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            receipt_digests[chain[0].id] = _digest("replacement terminal")
        return {}

    with pytest.raises(builder_prior.ChainBroken, match="changed during replay"):
        builder_prior._run_prior_paths(SimpleNamespace(run=run), paths)

    assert calls == 2


def test_builder_prior_prefix_fails_closed_without_terminal_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    chain = selected_chain.selected_layer_chain(shot)
    monkeypatch.setattr(builder_prior, "selected_layer_chain", lambda *_args, **_kwargs: chain)
    monkeypatch.setattr(
        builder_prior.layer_publication,
        "require_current_layer_publication",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            builder_prior.layer_publication.LayerPublicationConflict(
                "shot.json is invalid JSON"
            )
        ),
    )

    with pytest.raises(builder_prior.UnpassedPrior, match="invalid JSON"):
        builder_prior._prior_layer_paths(
            shot,
            chain[-1],
            selected_authority=SimpleNamespace(),
        )


def test_finished_chain_consumers_share_selected_global_dag_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    chain = selected_chain.selected_layer_chain(shot)
    expected = ["build/30_camera.py", "build/20_form.py", "build/01_composite.py"]

    replayed: list[str] = []
    prepared = builder_prior._prepare_artifact_replay_inputs(
        shot.folder,
        [
            (relative, shot.folder / relative)
            for relative in expected
        ],
    )
    monkeypatch.setattr(acceptance, "_RESET", "reset")
    monkeypatch.setattr(acceptance, "_preamble", lambda _shot: "preamble")
    selected_marker = SimpleNamespace()
    publications = tuple(SimpleNamespace(layer_id=layer.id) for layer in chain)
    monkeypatch.setattr(
        acceptance,
        "resolve_selected_authority",
        lambda _root: selected_marker,
    )
    monkeypatch.setattr(
        acceptance,
        "selected_layer_chain",
        lambda *_args, **_kwargs: chain,
    )
    monkeypatch.setattr(
        acceptance,
        "_current_layer_publications",
        lambda _shot, _layers, selected: (
            publications
            if selected is selected_marker
            else pytest.fail("acceptance used another selected authority")
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "_run_artifact_script",
        lambda _session, path, _prepared: replayed.append(
            path.relative_to(shot.folder).as_posix()
        ),
    )
    session = SimpleNamespace(run=lambda _code: None)
    assert acceptance._chain(session, shot, prepared_inputs=prepared) == expected
    assert replayed == expected

    bundle = SimpleNamespace(
        content_hash=_digest("bundle"),
        root=tmp_path / "selected-global",
    )
    token = AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256=_digest("plan pointer"),
        jit_revision=1,
        jit_pointer_sha256=_digest("jit pointer"),
    )
    selected = SimpleNamespace(
        plan=SimpleNamespace(bundle=bundle),
        assertion=SimpleNamespace(
            effective_view=SimpleNamespace(digest=_digest("view")),
        ),
        artifact_paths={
            "layers.json": tmp_path / "selected-executable-layers.json",
            "acceptance.json": tmp_path / "acceptance.json",
        },
        selection_token=token,
    )
    resolution_calls: list[Path] = []

    def resolve_once(root: Path) -> SimpleNamespace:
        resolution_calls.append(root)
        return selected

    monkeypatch.setattr(acceptance_stop, "resolve_selected_authority", resolve_once)
    monkeypatch.setattr(
        acceptance_stop,
        "current_judgment_debt_state_digest_for_authority",
        lambda _root, authority: (
            _digest("judgment-debt-state")
            if authority is selected
            else pytest.fail("acceptance used another authority snapshot")
        ),
    )
    monkeypatch.setattr(
        acceptance_stop,
        "authority_selection_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        acceptance_stop,
        "read_authority_selection_heads",
        lambda _root: SimpleNamespace(token=token),
    )
    def publication(layer):
        unit = layer.stages[0]
        return SimpleNamespace(
            receipt=SimpleNamespace(
                receipt_digest=_digest(f"receipt:{layer.id}"),
                claim=SimpleNamespace(
                    unit_inputs=(
                        SimpleNamespace(
                            unit_id=unit.id,
                            unit_digest=_digest(f"unit:{layer.id}"),
                            completion_receipt_digest=_digest(
                                f"completion:{layer.id}"
                            ),
                            script_path=unit.mutates.script_spans[0],
                            script_sha256=_digest(f"script:{layer.id}"),
                        ),
                    ),
                ),
            ),
            ledger_status="passed",
            ledger_script_path=str(layer.script),
            ledger_script_sha256=hashlib.sha256(
                (shot.folder / layer.script).read_bytes()
            ).hexdigest(),
            outcome_locator=f"plans/outcomes/layer-{layer.id}.json",
            outcome_sha256=_digest(f"outcome:{layer.id}"),
        )

    monkeypatch.setattr(
        acceptance_stop.layer_publication,
        "require_current_layer_publication",
        lambda _root, layer, authority: (
            publication(layer)
            if authority is selected
            else pytest.fail("acceptance used another authority snapshot")
        ),
    )
    snapshot = acceptance_stop.capture_acceptance_authority(
        shot,
        {"M1": Milestone("M1", 1, "refs/M1.png", "finished frame")},
    )
    assert [row["script"] for row in snapshot.chain] == expected
    assert [row["finalization_receipt_digest"] for row in snapshot.chain] == [
        _digest(f"receipt:{layer.id}") for layer in chain
    ]
    assert [row["layer_outcome"] for row in snapshot.chain] == [
        f"plans/outcomes/layer-{layer.id}.json" for layer in chain
    ]
    assert all(
        row["units"][0]["completion_receipt_digest"]
        == _digest(f"completion:{layer.id}")
        for row, layer in zip(snapshot.chain, chain, strict=True)
    )
    assert resolution_calls == [shot.folder.resolve()]

    monkeypatch.setattr(
        render_shot.layer_publication,
        "require_current_layer_publication",
        lambda _root, layer, authority: (
            publication(layer)
            if authority is selected
            else pytest.fail("render used another authority snapshot")
        ),
    )
    assert [
        path.relative_to(shot.folder).as_posix()
        for path in render_shot._chain_scripts(
            shot,
            selected_authority=selected,
        )
    ] == expected


def test_acceptance_replay_rejects_script_swap_before_execution_then_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    chain = selected_chain.selected_layer_chain(shot)
    entries = [
        (str(layer.script), shot.folder / layer.script)
        for layer in chain
    ]
    prepared = builder_prior._prepare_artifact_replay_inputs(shot.folder, entries)
    target = entries[0][1]
    original = target.with_suffix(".accepted")
    calls = 0

    def run(_source: str, **_kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            target.rename(original)
            target.write_text("# transient substituted script\n", encoding="utf-8")

    monkeypatch.setattr(acceptance, "_RESET", "reset")
    monkeypatch.setattr(acceptance, "_preamble", lambda _shot: "preamble")
    selected_marker = SimpleNamespace()
    publications = tuple(SimpleNamespace(layer_id=layer.id) for layer in chain)
    monkeypatch.setattr(
        acceptance,
        "resolve_selected_authority",
        lambda _root: selected_marker,
    )
    monkeypatch.setattr(
        acceptance,
        "selected_layer_chain",
        lambda *_args, **_kwargs: chain,
    )
    monkeypatch.setattr(
        acceptance,
        "_current_layer_publications",
        lambda _shot, _layers, _selected: publications,
    )

    try:
        with pytest.raises(builder_prior.BlenderError, match="trusted path changed"):
            acceptance._chain(
                SimpleNamespace(run=run),
                shot,
                prepared_inputs=prepared,
            )
    finally:
        target.unlink(missing_ok=True)
        original.rename(target)


def test_acceptance_authority_capture_rejects_publication_generation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    chain = selected_chain.selected_layer_chain(shot)
    token = AuthoritySelectionToken(
        plan_revision=1,
        plan_pointer_sha256=_digest("plan pointer"),
        jit_revision=1,
        jit_pointer_sha256=_digest("jit pointer"),
    )
    selected = SimpleNamespace(
        plan=SimpleNamespace(
            bundle=SimpleNamespace(
                content_hash=_digest("bundle"),
                root=tmp_path / "selected-global",
            )
        ),
        assertion=SimpleNamespace(
            effective_view=SimpleNamespace(digest=_digest("view")),
        ),
        artifact_paths={
            "layers.json": tmp_path / "selected-executable-layers.json",
            "acceptance.json": tmp_path / "acceptance.json",
        },
        selection_token=token,
    )
    calls = 0

    def publication(_root, layer, authority):
        nonlocal calls
        assert authority is selected
        calls += 1
        generation = "before" if calls <= len(chain) else "replacement"
        unit = layer.stages[0]
        return SimpleNamespace(
            receipt=SimpleNamespace(
                receipt_digest=_digest(f"{generation}:{layer.id}"),
                claim=SimpleNamespace(
                    unit_inputs=(
                        SimpleNamespace(
                            unit_id=unit.id,
                            unit_digest=_digest(f"unit:{layer.id}"),
                            completion_receipt_digest=_digest(
                                f"completion:{layer.id}"
                            ),
                            script_path=unit.mutates.script_spans[0],
                            script_sha256=hashlib.sha256(
                                (tmp_path / unit.mutates.script_spans[0]).read_bytes()
                            ).hexdigest(),
                        ),
                    ),
                ),
            ),
            ledger_status="passed",
            ledger_script_path=str(layer.script),
            ledger_script_sha256=hashlib.sha256(
                (tmp_path / layer.script).read_bytes()
            ).hexdigest(),
            outcome_locator=f"plans/outcomes/layer-{layer.id}.json",
            outcome_sha256=_digest(f"outcome:{generation}:{layer.id}"),
        )

    monkeypatch.setattr(
        acceptance_stop.layer_publication,
        "require_current_layer_publication",
        publication,
    )
    monkeypatch.setattr(
        acceptance_stop,
        "current_judgment_debt_state_digest_for_authority",
        lambda *_args: _digest("judgment-debt-state"),
    )
    monkeypatch.setattr(
        acceptance_stop,
        "authority_selection_lock",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        acceptance_stop,
        "read_authority_selection_heads",
        lambda _root: SimpleNamespace(token=token),
    )

    with pytest.raises(
        ValueError,
        match="publication changed during acceptance authority capture",
    ):
        acceptance_stop.capture_acceptance_authority(
            shot,
            {"M1": Milestone("M1", 1, "refs/M1.png", "finished frame")},
            selected,
        )


def test_acceptance_replay_rejects_a_replaced_valid_layer_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    chain = selected_chain.selected_layer_chain(shot)
    prepared = builder_prior._prepare_artifact_replay_inputs(
        shot.folder,
        [(str(layer.script), shot.folder / layer.script) for layer in chain],
    )
    selected = SimpleNamespace()
    captures = iter(
        (
            tuple(SimpleNamespace(receipt=f"before:{layer.id}") for layer in chain),
            tuple(SimpleNamespace(receipt=f"after:{layer.id}") for layer in chain),
        )
    )
    monkeypatch.setattr(acceptance, "resolve_selected_authority", lambda _root: selected)
    monkeypatch.setattr(
        acceptance,
        "selected_layer_chain",
        lambda *_args, **_kwargs: chain,
    )
    monkeypatch.setattr(
        acceptance,
        "_current_layer_publications",
        lambda *_args, **_kwargs: next(captures),
    )
    monkeypatch.setattr(acceptance, "_RESET", "reset")
    monkeypatch.setattr(acceptance, "_preamble", lambda _shot: "preamble")
    monkeypatch.setattr(acceptance, "_run_artifact_script", lambda *_args: None)

    with pytest.raises(
        acceptance.IncompleteChain,
        match="publication changed during acceptance replay",
    ):
        acceptance._chain(
            SimpleNamespace(run=lambda _code: None),
            shot,
            prepared_inputs=prepared,
        )


def test_render_mp4_replays_selected_scripts_through_evaluated_frame_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _selected_dag_fixture(tmp_path, monkeypatch)
    expected = ["build/30_camera.py", "build/20_form.py", "build/01_composite.py"]
    barrier_calls: list[str] = []

    class FakeSession:
        def __init__(self) -> None:
            self.artifacts = tmp_path / "blender-artifacts"
            self.artifacts.mkdir()
            self.raw_runs: list[str] = []
            self.renders: list[tuple[int, str, float]] = []
            self.closed = False

        def run(self, code: str) -> None:
            self.raw_runs.append(code)

        def render(self, *, frame: int, mode: str, scale: float) -> None:
            self.renders.append((frame, mode, scale))

        def close(self) -> None:
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr(render_shot, "require_current_accepted_outcome", lambda _shot: None)
    monkeypatch.setattr(render_shot, "_RESET", "reset")
    monkeypatch.setattr(render_shot, "_preamble", lambda _shot: "preamble")
    monkeypatch.setattr(
        render_shot,
        "BlenderSession",
        lambda **_kwargs: SimpleNamespace(start=lambda: session),
    )
    monkeypatch.setattr(
        render_shot,
        "_run_artifact_script",
        lambda active, path, _prepared: (
            pytest.fail("render barrier received another Blender session")
            if active is not session
            else barrier_calls.append(path.relative_to(shot.folder).as_posix())
        ),
    )

    def ffmpeg(args, **_kwargs):
        Path(args[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(render_shot.subprocess, "run", ffmpeg)
    output = tmp_path / "final.mp4"

    result = render_shot.render_mp4(shot, out=output, force=True)

    assert result == output
    assert barrier_calls == expected
    assert session.raw_runs == ["reset", "preamble"]
    assert session.renders == [(1, "eevee", 1.0)]
    assert session.closed is True
