"""Public orchestration lifecycle for a deferred judgment debt (HIR-0163)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import anyio
import pytest
from PIL import Image

from tests.layer_outcome_fixtures import write_test_layer_outcome
from tests.materialization_support import attest_exact_materialization_view
from tests.unit.test_judgment_debt_materialization import (
    _camera_payload,
    _fixture_root,
    _form_payload,
    _write,
    _write_payload,
)
from tests.unit_attempt_fixtures import (
    claim_for_build,
    executed_replay_input,
    freeze_unit,
    publish_passed_evaluation,
)
from vfx_harness.agents.builder import critic as critic_runtime
from vfx_harness.agents.builder import layer as layer_runtime
from vfx_harness.agents.builder import prior as prior_runtime
from vfx_harness.agents.builder import verify as verify_runtime
from vfx_harness.blender.observation_environment import (
    SCHEMA as OBSERVATION_ENVIRONMENT_SCHEMA,
)
from vfx_harness.blender.observation_environment import (
    canonical_observation_environment,
)
from vfx_harness.domain.brief import load_shot
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import unit_state_claims
from vfx_harness.orchestration.authority_selection import resolve_selected_authority
from vfx_harness.orchestration.jit_materialization import (
    MATERIALIZATION_SCHEMA,
    publish_materialization,
)
from vfx_harness.orchestration.judgment_debt_state import (
    current_judgment_debt_states,
    mark_judgment_debt_due,
    replay_prefix_unit_digests,
    require_judgment_debts_satisfied,
)
from vfx_harness.orchestration.judgment_payment_attempts import (
    EVENTS as PAYMENT_ATTEMPT_EVENTS,
)
from vfx_harness.orchestration.ledger import load_layers_from_path
from vfx_harness.orchestration.plan_authority import (
    publish_current,
    resolve_current,
    selected_artifact_path,
)
from vfx_harness.orchestration.unit_state import initialize, transition, unit_digest
from vfx_harness.orchestration.unit_state import load as load_unit_state


def _public_fixture_root(tmp_path: Path) -> Path:
    """Add the complete public-plan surface to the focused JIT fixture."""
    root = _fixture_root(tmp_path)
    (root / "brief.md").write_text(
        "---\nid: judgment-debt-public-pipeline\nframes: 1\nfps: 24\n"
        "type: still\n---\nThe camera frames the later rendered subject.\n",
        encoding="utf-8",
    )
    (root / "refs" / "reference.png").write_bytes(b"judgment-debt-reference")
    (root / "plans").mkdir(exist_ok=True)
    (root / "plans" / "global.md").write_text(
        "# Public judgment debt pipeline fixture\n", encoding="utf-8"
    )
    _write(root / "critic_axes.json", [
        {"key": "camera_alignment", "desc": "camera framing"},
        {"key": "form", "desc": "rendered form"},
    ])
    _write(root / "obligations.json", {
        "schema": "vfx-harness.obligations/v1", "obligations": []
    })
    _write(root / "assumptions.json", {
        "schema": "vfx-harness.assumptions/v1", "assumptions": []
    })
    return root


def _payload_for_bundle(root: Path, name: str, payload: dict, bundle_hash: str) -> Path:
    document = json.loads(json.dumps(payload))
    document["schema"] = MATERIALIZATION_SCHEMA
    document["bundle_hash"] = bundle_hash
    document["base_selection"] = resolve_selected_authority(
        root
    ).selection_token.to_dict()
    return _write_payload(root, name, document)


def _publish_payload(root: Path, payload: Path) -> Path:
    attest_exact_materialization_view(root, payload)
    return publish_materialization(root, payload)


def _pass_layer_unit(root: Path, layer_id: str, unit_id: str) -> None:
    layers_path = selected_artifact_path(root, "layers.json")
    layer = load_layers_from_path(layers_path)[layer_id]
    unit = next(unit for unit in layer.stages if unit.id == unit_id)
    artifact = root / unit.mutates.script_spans[0]
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(f"# deterministic fixture unit {layer_id}:{unit_id}\npass\n", encoding="utf-8")
    plan_hash = hashlib.sha256(layers_path.read_bytes()).hexdigest()
    initialize(root, layer_id, layer.stages, plan_hash=plan_hash)
    selection_token = resolve_selected_authority(root).selection_token
    attempt = claim_for_build(
        root,
        layer_id,
        layer.stages,
        unit_id,
        plan_hash=plan_hash,
        selection_token=selection_token,
    )
    freeze_unit(
        root,
        layer_id,
        unit,
        attempt,
        active_contract_ids=(),
        candidate_hash="missing",
        settings_hash=hashlib.sha256(b"deterministic-settings").hexdigest(),
        script_hash=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        input_hash=plan_hash,
        selection_token=selection_token,
    )
    transition(
        root,
        layer_id,
        unit_id,
        "evaluating",
        reason="public-pipeline-fixture",
        attempt=attempt,
        selection_token=selection_token,
    )
    publish_passed_evaluation(
        root,
        layer_id,
        unit,
        attempt,
    )
    unit_state_claims.complete_unit_attempt(
        root,
        layer_id,
        unit_id,
        layer.stages,
        attempt,
        expected_plan_hash=plan_hash,
        selection_token=selection_token,
        reason="public-pipeline-fixture",
        evidence=["fixture:public-pipeline-pass"],
    )


def _stash_fixture_capture(
    _session,
    shot,
    milestone,
    tag,
    scale: float = 0.5,
    *,
    mode: str = "eevee",
) -> tuple[str, dict]:
    dest = run_artifacts.renders_dir(shot.folder) / f"{milestone.id}_{tag}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color=(0, 0, 0)).save(dest)
    payload = {
        "schema": "vfx-harness.canonical-render-capture/v1",
        "frame": int(milestone.frame),
        "mode": mode,
        "scale": float(scale),
        "resolution": [32, 32, 50],
        "render_state": {"fixture": "public-pipeline"},
        "warnings": [],
        "png_sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return (
        dest.relative_to(shot.folder).as_posix(),
        {**payload, "capture_digest": hashlib.sha256(encoded).hexdigest()},
    )


class _ReplaySession:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.environment_revision = "base"

    def run(
        self,
        source: str,
        *,
        journal: bool = True,
        execution_policy: str | None = None,
    ) -> dict:
        assert execution_policy in {None, "artifact"}
        self.executed.append(source)
        return {
            "ok": True,
            "journal": journal,
            "execution_policy": execution_policy,
        }

    def canonical_observation_environment(
        self,
        *,
        frame: int,
        subject_roles,
        carrier_families,
        observation_medium: str,
    ) -> dict:
        return canonical_observation_environment(
            {
                "schema": OBSERVATION_ENVIRONMENT_SCHEMA,
                "frame": int(frame),
                "observation_medium": str(observation_medium),
                "subject_roles": [str(role) for role in subject_roles],
                "carrier_families": [str(family) for family in carrier_families],
                "fixture": "public-pipeline",
                "environment_revision": self.environment_revision,
            }
        )


async def _build_prepassed_layer(root: Path, layer_id: str, session: _ReplaySession) -> None:
    shot = load_shot(root)
    layer = load_layers_from_path(selected_artifact_path(root, "layers.json"))[layer_id]
    await layer_runtime.build_layer(shot, layer, session, verbose=False)


def _fixture_executable_evidence(_shot, layer, *_args, **_kwargs) -> list[dict]:
    """Give executable-only fixture canonicals a real typed replay observation."""
    layer_id = str(layer.id)
    return [
        {
            "id": f"{layer_id}-executable-replay",
            "metric": "object_property",
            "value": 1.0,
            "target": ">= 1",
            "pass": True,
            "source": "builder_state",
            "authoritative": True,
            "owner_layer": layer_id,
            "fault_owner": layer_id,
            "activates_at": layer_id,
            "lifecycle": "layer",
        }
    ]


def _publish_captured_outcome(root: Path, layer_id: str, captured: dict) -> Path:
    layer = load_layers_from_path(selected_artifact_path(root, "layers.json"))[layer_id]
    values = dict(captured)
    values["attempt"] = values.pop("ledger_attempt")
    return write_test_layer_outcome(root, layer, **values)


def test_public_jit_pipeline_defers_then_settles_matching_form_debt_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the matching later form unit can activate and settle camera judgment debt."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    debt_judgments: list[str] = []
    outcomes: list[dict] = []

    async def axes(*_args):
        return [("camera_alignment", "camera framing"), ("form", "rendered form")]

    async def deterministic_judge(
        _shot,
        _milestone,
        _render,
        axes,
        *_args,
        active_unit=None,
        **kwargs,
    ) -> dict:
        debt_ids = tuple(getattr(active_unit, "provisional_debt_ids", ()) or ())
        debt_judgments.extend(debt_ids)
        return {
            "scores": {key: 5 for key, _description in axes},
            "mean": 5.0,
            "pass": True,
            "issues": [],
            "scored_axes": [key for key, _description in axes],
            "na_axes": [],
            "observations": [],
            "observation_reconciliation": [],
            "contract_gap": False,
            "judge_conflict": False,
            "decided_by": "deterministic-fixture",
            "evidence": list(kwargs.get("evidence") or []),
        }

    builder = verify_runtime.builder_package()
    monkeypatch.setattr(layer_runtime, "ensure_axes", axes)
    monkeypatch.setattr(
        layer_runtime,
        "publish_composed_layer_outcome",
        lambda _folder, _layer, **kwargs: outcomes.append(dict(kwargs)),
    )
    monkeypatch.setattr(layer_runtime, "_blender_version", lambda _session: "fixture")
    monkeypatch.setattr(layer_runtime.costlog, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(layer_runtime.costlog, "unbind", lambda: None)
    monkeypatch.setattr(layer_runtime.transcript, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(layer_runtime.transcript, "unbind", lambda: None)
    monkeypatch.setattr(prior_runtime.generate_construction, "pin_for_script", lambda *_args: None)
    monkeypatch.setattr(builder, "_scene_object_manifest", lambda _session: {})
    monkeypatch.setattr(builder, "_stash_render", lambda *_args, **_kwargs: "fixture-render.png")
    monkeypatch.setattr(builder, "_stash_render_with_receipt", _stash_fixture_capture)
    monkeypatch.setattr(builder, "_render_evidence", _fixture_executable_evidence)
    monkeypatch.setattr(verify_runtime, "_persist_contract_gaps", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify_runtime, "_judge_unit_or_layer", deterministic_judge)

    root = _public_fixture_root(tmp_path)
    bundle = publish_current(
        root,
        run_artifacts.create(root, "judgment-debt-public-pipeline"),
        outcome="clean_with_deferred",
    )

    _publish_payload(
        root,
        _payload_for_bundle(root, "camera-jit.json", _camera_payload(), bundle.content_hash),
    )
    _pass_layer_unit(root, "1", "camera")
    session = _ReplaySession()
    anyio.run(_build_prepassed_layer, root, "1", session)
    definition, activation, state = current_judgment_debt_states(root)[0]
    assert activation is None
    assert state.status == "pending_not_due"

    _publish_captured_outcome(root, "1", outcomes[-1])
    _publish_payload(
        root,
        _payload_for_bundle(root, "form-jit.json", _form_payload(), bundle.content_hash),
    )
    _pass_layer_unit(root, "2", "hall_form")
    definition, activation, state = current_judgment_debt_states(root)[0]
    assert activation is not None
    assert activation.payer_layer == "2"
    form_unit = load_layers_from_path(selected_artifact_path(root, "layers.json"))["2"].stages[0]
    assert activation.payer_unit_digests == (("2:hall_form", unit_digest(form_unit)),)
    assert state.status == "pending_not_due"

    camera_only_receipt = replay_prefix_unit_digests(
        root,
        replayed_layer_scripts=(root / "build" / "01_camera.py",),
        replay_inputs=(
            executed_replay_input(root, "build/01_camera.py"),
        ),
    )
    with pytest.raises(ValueError, match=r"exact payer units.*missing=2:hall_form"):
        mark_judgment_debt_due(
            root,
            definition.digest,
            layer_id="2",
            replayed_unit_digests=camera_only_receipt,
        )
    assert current_judgment_debt_states(root)[0][2].status == "pending_not_due"

    with pytest.raises(ValueError, match="unresolved current judgment debt"):
        require_judgment_debts_satisfied(root)

    anyio.run(_build_prepassed_layer, root, "2", session)
    assert current_judgment_debt_states(root)[0][2].status == "satisfied"
    assert debt_judgments == [definition.debt_id]
    payment_verdict = outcomes[-1]["canonical"][0][1]
    assert payment_verdict["judgment_observation"]["request"]["definition_digest"] == definition.digest
    assert payment_verdict["judgment_observation"]["candidate_capture"]["png_sha256"]
    event_path = root / "state" / "judgment-debts.jsonl"
    assert len(event_path.read_text().splitlines()) == 2

    # A direct builder restart rebuilds canonical composition but the terminal debt is
    # absent from its schedule, so it cannot buy or append another debt judgment.
    anyio.run(_build_prepassed_layer, root, "2", session)
    assert debt_judgments == [definition.debt_id]
    assert len(event_path.read_text().splitlines()) == 2
    require_judgment_debts_satisfied(root)
    assert resolve_current(root).content_hash == bundle.content_hash
    assert load_unit_state(root, "1").get("replans") == []
    assert load_unit_state(root, "2").get("replans") == []


def test_public_pipeline_suppresses_unchanged_no_signal_payment_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due debt gets one raster per exact observation identity and no critic call."""
    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    renders: list[str] = []
    critic_calls: list[str] = []
    outcomes: list[dict] = []

    async def axes(*_args):
        return [("camera_alignment", "camera framing"), ("form", "rendered form")]

    async def fail_if_critic_called(*_args, **_kwargs):
        critic_calls.append("called")
        raise AssertionError("a no-signal plate must not call the visual critic")

    async def deterministic_or_signal_judge(
        shot,
        milestone,
        render,
        owned_axes,
        session,
        verbose,
        scope=None,
        *,
        active_unit=None,
        **kwargs,
    ):
        if tuple(getattr(active_unit, "provisional_debt_ids", ()) or ()):
            return await critic_runtime._judge(
                shot,
                milestone,
                render,
                owned_axes,
                session,
                verbose,
                scope,
                active_unit=active_unit,
                **kwargs,
            )
        return {
            "scores": {key: 5 for key, _description in owned_axes},
            "mean": 5.0,
            "pass": True,
            "issues": [],
            "scored_axes": [key for key, _description in owned_axes],
            "na_axes": [],
            "observations": [],
            "observation_reconciliation": [],
            "contract_gap": False,
            "judge_conflict": False,
            "decided_by": "deterministic-executable-fixture",
            "evidence": list(kwargs.get("evidence") or []),
        }

    def stash_black(*args, **kwargs) -> tuple[str, dict]:
        render_rel, receipt = _stash_fixture_capture(*args, **kwargs)
        renders.append(Path(render_rel).name)
        return render_rel, receipt

    def capture_outcome(_folder, _layer, **kwargs) -> None:
        outcomes.append(dict(kwargs))

    builder = verify_runtime.builder_package()
    monkeypatch.setattr(layer_runtime, "ensure_axes", axes)
    monkeypatch.setattr(
        layer_runtime,
        "publish_composed_layer_outcome",
        capture_outcome,
    )
    monkeypatch.setattr(layer_runtime, "_blender_version", lambda _session: "fixture")
    monkeypatch.setattr(layer_runtime.costlog, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(layer_runtime.costlog, "unbind", lambda: None)
    monkeypatch.setattr(layer_runtime.transcript, "bind", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(layer_runtime.transcript, "unbind", lambda: None)
    monkeypatch.setattr(prior_runtime.generate_construction, "pin_for_script", lambda *_args: None)
    monkeypatch.setattr(builder, "_scene_object_manifest", lambda _session: {})
    monkeypatch.setattr(builder, "_stash_render_with_receipt", stash_black)
    monkeypatch.setattr(builder, "_render_evidence", _fixture_executable_evidence)
    monkeypatch.setattr(verify_runtime, "_persist_contract_gaps", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify_runtime, "_judge_unit_or_layer", deterministic_or_signal_judge)
    monkeypatch.setattr(critic_runtime, "_critique", fail_if_critic_called)

    root = _public_fixture_root(tmp_path)
    bundle = publish_current(
        root,
        run_artifacts.create(root, "judgment-debt-no-signal-ratchet"),
        outcome="clean_with_deferred",
    )
    _publish_payload(
        root,
        _payload_for_bundle(root, "camera-jit.json", _camera_payload(), bundle.content_hash),
    )
    _pass_layer_unit(root, "1", "camera")
    session = _ReplaySession()
    anyio.run(_build_prepassed_layer, root, "1", session)
    _publish_captured_outcome(root, "1", outcomes[-1])
    _publish_payload(
        root,
        _payload_for_bundle(root, "form-jit.json", _form_payload(), bundle.content_hash),
    )
    _pass_layer_unit(root, "2", "hall_form")

    anyio.run(_build_prepassed_layer, root, "2", session)
    definition, _activation, state = current_judgment_debt_states(root)[0]
    assert state.status == "due"
    assert renders == ["2_canonical.png"]
    assert critic_calls == []
    attempts = root / "state" / PAYMENT_ATTEMPT_EVENTS
    assert len(attempts.read_text(encoding="utf-8").splitlines()) == 1
    assert outcomes[-1]["canonical"][0][1]["decided_by"] == "no_optical_signal"

    # Exact restart: replay is still re-proven, but raster and critic both stay idle.
    anyio.run(_build_prepassed_layer, root, "2", session)
    assert renders == ["2_canonical.png"]
    assert critic_calls == []
    assert len(attempts.read_text(encoding="utf-8").splitlines()) == 1
    cached = outcomes[-1]["canonical"][0][1]
    assert cached["decided_by"] == "unchanged_payment_attempt"
    assert cached["payment_attempt"]["suppressed"] is True
    assert current_judgment_debt_states(root)[0][2].status == "due"

    # A changed typed render environment is a changed observation input: exactly one new
    # raster is legal, then its new no-signal result ratchets as well.
    session.environment_revision = "different-render-environment"
    anyio.run(_build_prepassed_layer, root, "2", session)
    assert len(renders) == 2
    assert len(attempts.read_text(encoding="utf-8").splitlines()) == 2
    anyio.run(_build_prepassed_layer, root, "2", session)
    assert len(renders) == 2
    assert len(attempts.read_text(encoding="utf-8").splitlines()) == 2
    assert critic_calls == []
    assert resolve_current(root).content_hash == definition.seed.bundle_digest
