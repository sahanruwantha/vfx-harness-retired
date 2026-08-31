"""Read-only consumers stay on one verified plan/JIT selection snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from vfx_harness.agents import guardrails
from vfx_harness.agents.builder import critic_focus
from vfx_harness.domain.authority_head_records import (
    OVERLAY_ARTIFACTS,
    canonical_json_bytes,
)
from vfx_harness.domain.brief import Shot
from vfx_harness.evaluation import blank, integrity, variance
from vfx_harness.evidence import claim_evidence
from vfx_harness.orchestration.ledger import Milestone


def _shot(root: Path) -> Shot:
    return Shot(
        folder=root,
        frontmatter={
            "id": "snapshot-consumer",
            "frames": 1,
            "fps": 24,
            "resolution": [16, 16],
            "engine": "BLENDER_EEVEE_NEXT",
        },
        body="fixture",
    )


def _write_contracts(path: Path, key: str, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": 2, key: rows}) + "\n",
        encoding="utf-8",
    )


def test_bounded_context_guard_caches_one_hybrid_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "global.md",
        "layers.json",
        "acceptance.json",
        "critic_axes.json",
        "checks.json",
        "scene_checks.json",
        "requirements.json",
        "obligations.json",
        "assumptions.json",
    )
    bundle = tmp_path / "bundle"
    overlay = tmp_path / "jit-view"
    paths = {
        name: (overlay / name if name in {"layers.json", "checks.json", "scene_checks.json"} else bundle / name)
        for name in names
    }
    selected = SimpleNamespace(plan=object(), artifact_paths=paths)
    resolutions: list[Path] = []

    def resolve_once(root: Path):
        resolutions.append(Path(root))
        if len(resolutions) > 1:
            pytest.fail("bounded context guard re-resolved selected authority")
        return selected

    monkeypatch.setattr(
        guardrails.authority_selection,
        "resolve_selected_authority",
        resolve_once,
    )
    hook = guardrails.bounded_unit_context_guard(
        tmp_path,
        {"mode": "live", "unit_id": "form"},
    ).hooks[0]

    first = anyio.run(
        hook,
        {"tool_name": "Read", "tool_input": {"file_path": str(paths["checks.json"])}},
        "tool-1",
        None,
    )
    second = anyio.run(
        hook,
        {"tool_name": "Read", "tool_input": {"file_path": str(paths["global.md"])}},
        "tool-2",
        None,
    )

    assert first["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert second["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert resolutions == [tmp_path.resolve()]


def test_selected_plan_guard_freezes_the_supplied_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VFXH_RUN_DIR", raising=False)
    selected_bundle = tmp_path / "runs" / "selected" / "checkpoints" / "plans" / "bundle"
    selected_file = selected_bundle / "global.md"
    selected_file.parent.mkdir(parents=True)
    selected_file.write_text("selected\n", encoding="utf-8")
    historical_file = tmp_path / "runs" / "historical" / "plans" / "global.md"
    historical_file.parent.mkdir(parents=True)
    historical_file.write_text("historical\n", encoding="utf-8")
    selected = SimpleNamespace(
        plan=SimpleNamespace(bundle=SimpleNamespace(root=selected_bundle)),
    )
    monkeypatch.setattr(
        guardrails.authority_selection,
        "resolve_selected_authority",
        lambda _root: pytest.fail("supplied read-guard snapshot was re-resolved"),
    )
    hook = guardrails.selected_plan_read_guard(tmp_path, selected).hooks[0]

    allowed = anyio.run(
        hook,
        {"tool_name": "Read", "tool_input": {"file_path": str(selected_file)}},
        "tool-1",
        None,
    )
    denied = anyio.run(
        hook,
        {"tool_name": "Read", "tool_input": {"file_path": str(historical_file)}},
        "tool-2",
        None,
    )

    assert allowed == {}
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_required_focus_reads_checks_from_supplied_hybrid_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    reference = tmp_path / "refs" / "detail.png"
    reference.parent.mkdir()
    reference.write_bytes(b"reference")
    checks = tmp_path / "jit-view" / "checks.json"
    _write_contracts(
        checks,
        "checks",
        [
            {
                "id": "detail-focus",
                "owner_layer": "2",
                "fault_owner": "2",
                "activates_at": "2",
                "lifecycle": "layer",
                "axis": "detail",
                "frame": 1,
                "focus": {
                    "required": True,
                    "crop": [0.1, 0.1, 0.3, 0.3],
                    "reason": "the feature is contract-critical and small",
                },
            }
        ],
    )
    (tmp_path / "checks.json").write_text("not the selected view\n", encoding="utf-8")
    selected = SimpleNamespace(
        plan=object(),
        artifact_paths={"checks.json": checks},
    )
    monkeypatch.setattr(
        critic_focus.authority_selection,
        "resolve_selected_authority",
        lambda _root: pytest.fail("supplied focus snapshot was re-resolved"),
    )

    requests = critic_focus._required_focus_requests(
        shot,
        "2",
        1,
        [("detail", "fine feature")],
        layers={"2": SimpleNamespace(judges=((1, "refs/detail.png"),))},
        selected_authority=selected,
    )

    assert [row["id"] for row in requests] == ["detail-focus"]
    assert requests[0]["reference"] == "refs/detail.png"


def test_claim_context_passes_supplied_snapshot_to_layer_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    selected = SimpleNamespace(plan=object(), artifact_paths={})
    unit = SimpleNamespace(id="form", evaluation=SimpleNamespace(claims=()))
    observed: list[object] = []

    def load_layers(_shot: Shot, *, selected_authority: object):
        observed.append(selected_authority)
        return {"2": SimpleNamespace(stages=(unit,))}

    monkeypatch.setattr(critic_focus, "load_layers", load_layers)
    monkeypatch.setattr(
        critic_focus.authority_selection,
        "resolve_selected_authority",
        lambda _root: pytest.fail("supplied claim-context snapshot was re-resolved"),
    )

    claims, bindings, qualified = critic_focus._claim_context(
        shot,
        Milestone("2", 1, "refs/detail.png", "detail"),
        enabled=True,
        selected_authority=selected,
    )

    assert claims == []
    assert bindings == {}
    assert qualified == set()
    assert observed == [selected]


def test_claim_closure_resolves_once_and_reuses_exact_contract_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scene_checks = tmp_path / "jit-view" / "scene_checks.json"
    image_checks = tmp_path / "jit-view" / "checks.json"
    _write_contracts(scene_checks, "contracts", [])
    _write_contracts(image_checks, "checks", [])
    pointer = tmp_path / "plans" / "current.json"
    pointer.parent.mkdir()
    pointer.write_text("resolved by the test double\n", encoding="utf-8")
    (tmp_path / "scene_checks.json").write_text("mixed generation\n", encoding="utf-8")
    (tmp_path / "checks.json").write_text("mixed generation\n", encoding="utf-8")
    selected = SimpleNamespace(
        plan=object(),
        artifact_paths={
            "scene_checks.json": scene_checks,
            "checks.json": image_checks,
        },
    )
    resolutions: list[Path] = []

    def resolve_once(root: Path):
        resolutions.append(Path(root))
        if len(resolutions) > 1:
            pytest.fail("claim closure re-resolved selected authority")
        return selected

    monkeypatch.setattr(
        guardrails.authority_selection,
        "resolve_selected_authority",
        resolve_once,
    )

    result = claim_evidence.validate_claim_closure(tmp_path, [])

    assert result.clean
    assert resolutions == [tmp_path]


def test_claim_closure_uses_v3_consumer_marker_without_following_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = tmp_path / "shot"
    consumer = tmp_path / "consumer"
    content_hash = hashlib.sha256(b"bundle").hexdigest()
    bundle = shot / "runs" / "plan-run" / "checkpoints" / "plans" / "bundles" / content_hash
    consumer.mkdir()
    for name in OVERLAY_ARTIFACTS:
        payload = (
            {"schema": 2, "contracts": []}
            if name == "scene_checks.json"
            else {"schema": 2, "checks": []}
            if name == "checks.json"
            else {}
        )
        (consumer / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")
    artifact_hashes = {name: hashlib.sha256((consumer / name).read_bytes()).hexdigest() for name in OVERLAY_ARTIFACTS}
    marker = {
        "schema": "vfx-harness.plan-consumer-view/v3",
        "shot": str(shot.resolve()),
        "bundle": str(bundle.resolve()),
        "content_hash": content_hash,
        "base_selection": {
            "schema": "vfx-harness.authority-selection-token/v1",
            "plan_revision": 1,
            "plan_pointer_sha256": hashlib.sha256(b"plan pointer").hexdigest(),
            "jit_revision": 1,
            "jit_pointer_sha256": hashlib.sha256(b"jit pointer").hexdigest(),
        },
        "effective_view": {
            "source": "jit",
            "digest": hashlib.sha256(b"view").hexdigest(),
            "artifact_hashes": artifact_hashes,
        },
        "authored_inputs": {
            "brief.md": hashlib.sha256(b"fixture brief").hexdigest()
        },
        "decision_inputs": {},
    }
    (consumer / ".plan-consumer-view.json").write_bytes(canonical_json_bytes(marker))
    monkeypatch.setattr(
        guardrails.authority_selection,
        "resolve_selected_authority",
        lambda _root: pytest.fail("consumer-view closure followed live shot state"),
    )

    result = claim_evidence.validate_claim_closure(consumer, [])

    assert result.clean

    (consumer / "checks.json").write_text(
        json.dumps({"schema": 2, "checks": [{"id": "tampered"}]}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"checks\.json bytes do not match"):
        claim_evidence.validate_claim_closure(consumer, [])


def test_blank_measure_threads_one_selected_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    reference = tmp_path / "refs" / "M1.png"
    reference.parent.mkdir()
    reference.write_bytes(b"reference")
    selected = SimpleNamespace(plan=object(), artifact_paths={})
    layer = SimpleNamespace(judge_frame=1, reads="subject", judge_ref="refs/M1.png")
    observed: list[tuple[str, object]] = []
    monkeypatch.setattr(blank, "BLANK", tmp_path / "evaluations" / "blank")
    monkeypatch.setattr(
        blank,
        "_black_png",
        lambda _reference, destination: destination,
    )
    monkeypatch.setattr(
        blank.authority_selection,
        "resolve_selected_authority",
        lambda _root: selected,
    )
    monkeypatch.setattr(
        blank,
        "load_axes",
        lambda _shot, authority: observed.append(("axes", authority)) or [("composition", "match")],
    )
    monkeypatch.setattr(
        blank,
        "load_layers",
        lambda _shot, *, selected_authority: observed.append(("layers", selected_authority)) or {"L1": layer},
    )
    monkeypatch.setattr(
        blank,
        "layer_scope",
        lambda _shot, _layer, *, selected_authority: observed.append(("scope", selected_authority)) or "scope",
    )

    async def critique(*_args, **_kwargs):
        observed.append(("critic", _kwargs["selected_authority"]))
        return {"scores": {"composition": 1.0}, "mean": 1.0, "pass": False}

    monkeypatch.setattr(blank, "_critique", critique)

    result = anyio.run(lambda: blank.measure(shot, layer_id="L1"))

    assert result["layer"] == "L1"
    assert observed == [
        ("axes", selected),
        ("layers", selected),
        ("scope", selected),
        ("critic", selected),
    ]


def test_variance_measure_threads_one_selected_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    for relative in ("renders/current.png", "refs/M1.png"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
    selected = SimpleNamespace(plan=object(), artifact_paths={})
    layer = SimpleNamespace(judge_frame=1, reads="subject")
    observed: list[tuple[str, object]] = []
    monkeypatch.setattr(
        variance.authority_selection,
        "resolve_selected_authority",
        lambda _root: selected,
    )
    monkeypatch.setattr(
        variance,
        "load_axes",
        lambda _shot, authority: observed.append(("axes", authority)) or [("composition", "match")],
    )
    monkeypatch.setattr(
        variance,
        "load_layers",
        lambda _shot, *, selected_authority: observed.append(("layers", selected_authority)) or {"L1": layer},
    )
    monkeypatch.setattr(
        variance,
        "layer_scope",
        lambda _shot, _layer, *, selected_authority: observed.append(("scope", selected_authority)) or "scope",
    )

    async def critique(*_args, **_kwargs):
        observed.append(("critic", _kwargs["selected_authority"]))
        return {
            "scores": {"composition": 3.0},
            "mean": 3.0,
            "pass": True,
            "scored_axes": ["composition"],
            "na_axes": [],
        }

    monkeypatch.setattr(variance, "_critique", critique)

    result = anyio.run(
        lambda: variance.measure(
            shot,
            render_rel="renders/current.png",
            ref_rel="refs/M1.png",
            layer_id="L1",
            n=1,
        )
    )

    assert result["layer"] == "L1"
    assert observed == [
        ("axes", selected),
        ("layers", selected),
        ("scope", selected),
        ("critic", selected),
    ]


def test_integrity_threads_one_selected_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shot = _shot(tmp_path)
    (tmp_path / "shot.json").write_text(
        json.dumps({"milestones": {}}) + "\n",
        encoding="utf-8",
    )
    selected = SimpleNamespace(plan=object(), artifact_paths={})
    resolutions: list[Path] = []
    observed: list[object] = []
    monkeypatch.setattr(
        integrity.authority_selection,
        "resolve_selected_authority",
        lambda root: resolutions.append(Path(root)) or selected,
    )
    monkeypatch.setattr(
        integrity,
        "load_layers",
        lambda _shot, *, selected_authority: observed.append(selected_authority) or {},
    )
    monkeypatch.setattr(
        integrity,
        "provenance_check",
        lambda _root: pytest.fail("selected bundle was redundantly checked as legacy provenance"),
    )

    errors, warnings, _data = integrity._problems(shot)

    assert errors == []
    assert warnings == []
    assert resolutions == [shot.folder]
    assert observed == [selected]
