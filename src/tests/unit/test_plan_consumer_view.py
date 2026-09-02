"""One-token run-scoped plan consumer snapshots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from vfx_harness.evaluation.plan_gate.types import _materialized_view
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import (
    authority_selection,
    authority_state_transaction,
    plan_authority,
    plan_consumer_view_projection,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionToken,
)
from vfx_harness.orchestration.jit_materialization.view_pointer import canonical_view_hash
from vfx_harness.orchestration.plan_consumer_view import (
    CONSUMER_VIEW_SCHEMA,
    OVERLAY_ARTIFACTS,
    PlanConsumerViewMarker,
)
from vfx_harness.orchestration.plan_inputs import (
    exact_planning_input_identity,
    require_exact_planning_input_identity,
)


@pytest.fixture(autouse=True)
def _below_plan_gate_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """These layer documents sit below the plan gate, so the accepted-build projection
    that republication derives from gate-valid layer authority is stubbed here; the
    public pipeline fixtures cover it (HIR-0172)."""

    monkeypatch.setattr(
        authority_state_transaction,
        "republish_accepted_build_index",
        lambda *_args, **_kwargs: "current",
    )



def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _documents() -> dict[str, object]:
    return {
        "layers.json": {
            "schema": 5,
            "layers": [
                {"id": "camera", "execution": "jit_deferred"},
                {"id": "form", "execution": "ready"},
            ],
        },
        "scene_checks.json": {"schema": 2, "contracts": []},
        "checks.json": {"schema": 2, "checks": []},
        "requirements.json": {"schema": 2, "requirements": []},
        "acceptance.json": [],
    }


def _marker(root: Path, *, source: str = "jit") -> PlanConsumerViewMarker:
    content_hash = _digest("bundle")
    documents = _documents()
    for name, document in documents.items():
        (root / name).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return PlanConsumerViewMarker.from_dict(
        {
            "schema": CONSUMER_VIEW_SCHEMA,
            "shot": str(root),
            "bundle": str(
                root
                / "runs"
                / "publisher"
                / "checkpoints"
                / "plans"
                / "bundles"
                / content_hash
            ),
            "content_hash": content_hash,
            "base_selection": AuthoritySelectionToken(
                plan_revision=3,
                plan_pointer_sha256=_digest("plan pointer"),
                jit_revision=2,
                jit_pointer_sha256=_digest("jit pointer"),
            ).to_dict(),
            "effective_view": {
                "source": source,
                "digest": (
                    canonical_view_hash(documents)
                    if source == "jit"
                    else content_hash
                ),
                "artifact_hashes": {
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in OVERLAY_ARTIFACTS
                },
            },
            "authored_inputs": {"brief.md": _digest("brief")},
            "decision_inputs": {
                "state/plan-resolutions.jsonl": {
                    "size": 12,
                    "sha256": _digest("decisions"),
                }
            },
        }
    )


def test_strict_marker_round_trip_binds_both_revisioned_heads(tmp_path: Path) -> None:
    marker = _marker(tmp_path)

    assert PlanConsumerViewMarker.from_dict(marker.to_dict()) == marker
    assert marker.bundle_run_id == "publisher"
    assert marker.base_selection.plan_revision == 3
    assert marker.base_selection.jit_revision == 2

    legacy = marker.to_dict()
    legacy["schema"] = "vfx-harness.plan-consumer-view/v2"
    with pytest.raises(ValueError, match="schema is unsupported"):
        PlanConsumerViewMarker.from_dict(legacy)

    incomplete = marker.to_dict()
    incomplete.pop("decision_inputs")
    with pytest.raises(ValueError, match="fields do not match the v3 schema"):
        PlanConsumerViewMarker.from_dict(incomplete)


def test_gate_reads_materialization_from_sealed_snapshot_not_live_pointer(
    tmp_path: Path,
) -> None:
    marker = _marker(tmp_path)
    (tmp_path / ".plan-consumer-view.json").write_text(
        json.dumps(marker.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    materialized, artifacts = _materialized_view(tmp_path)

    assert materialized == {"form"}
    assert artifacts == set(OVERLAY_ARTIFACTS)
    assert not (tmp_path / "state" / "jit-layers" / "current.json").exists()

    (tmp_path / "checks.json").write_text("{}\n", encoding="utf-8")
    assert _materialized_view(tmp_path) == (set(), set())


def test_bundle_snapshot_never_claims_jit_materialization(tmp_path: Path) -> None:
    marker = _marker(tmp_path, source="bundle")
    (tmp_path / ".plan-consumer-view.json").write_text(
        json.dumps(marker.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert _materialized_view(tmp_path) == (set(), set())


def _write_plan_workspace(root: Path) -> None:
    (root / "brief.md").write_text("# original brief\n", encoding="utf-8")
    (root / "refs").mkdir()
    (root / "refs" / "hero.txt").write_text("original ref\n", encoding="utf-8")
    (root / "plans").mkdir()
    (root / "plans" / "global.md").write_text("# global plan\n", encoding="utf-8")
    layer = {
        "id": "camera",
        "script": "build/camera.py",
        "title": "Camera",
        "primary_judge": 1,
        "judge": [{"frame": 1, "ref": "refs/hero.txt"}],
        "owns": ["camera"],
        "evidence_domains": ["scene"],
        "reads": "Fixture camera authority",
        "execution": "jit_deferred",
        "stages": [],
        "jit": {
            "depends_on_layers": [],
            "required_outcomes": [],
            "provides": {"camera": ["camera.rig"]},
            "reserved_roles": ["camera.rig"],
            "owned_requirements": [],
        },
    }
    documents: dict[str, object] = {
        "layers.json": {"schema": 5, "layers": [layer]},
        "acceptance.json": [],
        "critic_axes.json": [],
        "checks.json": {"schema": 2, "checks": []},
        "scene_checks.json": {"schema": 2, "contracts": []},
        "requirements.json": {
            "schema": "vfx-harness.requirements/v2",
            "judgment_debt_definitions": [],
            "judgment_debt_activations": [],
            "requirements": [],
        },
        "obligations.json": {
            "schema": "vfx-harness.obligations/v1",
            "obligations": [],
        },
        "assumptions.json": {
            "schema": "vfx-harness.assumptions/v1",
            "assumptions": [],
        },
    }
    for name, document in documents.items():
        (root / name).write_text(json.dumps(document) + "\n", encoding="utf-8")


def test_consumer_view_copies_and_binds_exact_planning_inputs(tmp_path: Path) -> None:
    _write_plan_workspace(tmp_path)
    amendments = b'{"id":"amendment-1"}\n'
    resolutions = b'{"id":"decision-1"}\n'
    (tmp_path / "plan_amendments.jsonl").write_bytes(amendments)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "plan-resolutions.jsonl").write_bytes(resolutions)
    (tmp_path / "state" / "work-units").mkdir()
    planning = run_artifacts.create(tmp_path, "plan")
    plan_authority.publish_current(tmp_path, planning, outcome="clean_with_deferred")
    consumer = run_artifacts.create(tmp_path, "consumer")

    view = plan_authority.prepare_consumer_view(consumer)
    marker = PlanConsumerViewMarker.from_bytes(
        (view / ".plan-consumer-view.json").read_bytes()
    )

    assert not (view / "brief.md").is_symlink()
    assert not (view / "refs").is_symlink()
    assert not (view / "plan_amendments.jsonl").is_symlink()
    assert not (view / "state").is_symlink()
    assert not (view / "state" / "plan-resolutions.jsonl").is_symlink()
    assert (view / "state" / "work-units").is_dir()
    assert not (view / "state" / "work-units").is_symlink()
    assert marker.authored_inputs == {
        "brief.md": hashlib.sha256(b"# original brief\n").hexdigest(),
        "refs/hero.txt": hashlib.sha256(b"original ref\n").hexdigest(),
    }
    assert marker.decision_inputs == {
        "plan_amendments.jsonl": {
            "size": len(amendments),
            "sha256": hashlib.sha256(amendments).hexdigest(),
        },
        "state/plan-resolutions.jsonl": {
            "size": len(resolutions),
            "sha256": hashlib.sha256(resolutions).hexdigest(),
        },
    }

    (tmp_path / "brief.md").write_text("# changed brief\n", encoding="utf-8")
    (tmp_path / "refs" / "hero.txt").write_text("changed ref\n", encoding="utf-8")
    (tmp_path / "plan_amendments.jsonl").write_text("changed\n", encoding="utf-8")
    (tmp_path / "state" / "plan-resolutions.jsonl").write_text(
        "changed\n",
        encoding="utf-8",
    )

    assert (view / "brief.md").read_text(encoding="utf-8") == "# original brief\n"
    assert (view / "refs" / "hero.txt").read_text(encoding="utf-8") == "original ref\n"
    assert (view / "plan_amendments.jsonl").read_bytes() == amendments
    assert (view / "state" / "plan-resolutions.jsonl").read_bytes() == resolutions


def test_consumer_view_ledger_snapshot_is_descriptor_created_and_distinct(
    tmp_path: Path,
) -> None:
    _write_plan_workspace(tmp_path)
    ledger_payload = b'{"schema":"fixture-ledger","milestones":{}}\n'
    canonical_ledger = tmp_path / "shot.json"
    canonical_ledger.write_bytes(ledger_payload)
    canonical_identity = (
        canonical_ledger.stat().st_dev,
        canonical_ledger.stat().st_ino,
    )
    planning = run_artifacts.create(tmp_path, "plan")
    plan_authority.publish_current(
        tmp_path,
        planning,
        outcome="clean_with_deferred",
    )
    consumer = run_artifacts.create(tmp_path, "consumer")

    view = plan_authority.prepare_consumer_view(consumer)
    projected_ledger = view / "shot.json"

    assert projected_ledger.read_bytes() == ledger_payload
    assert not projected_ledger.is_symlink()
    assert (
        projected_ledger.stat().st_dev,
        projected_ledger.stat().st_ino,
    ) != canonical_identity
    assert canonical_ledger.read_bytes() == ledger_payload
    assert (
        canonical_ledger.stat().st_dev,
        canonical_ledger.stat().st_ino,
    ) == canonical_identity


def test_consumer_view_refuses_authored_capture_that_no_longer_matches_selected_plan(
    tmp_path: Path,
) -> None:
    _write_plan_workspace(tmp_path)
    planning = run_artifacts.create(tmp_path, "plan")
    plan_authority.publish_current(tmp_path, planning, outcome="clean_with_deferred")
    selected = authority_selection.resolve_selected_authority(tmp_path)
    consumer = run_artifacts.create(tmp_path, "consumer")
    (tmp_path / "brief.md").write_text("# changed brief\n", encoding="utf-8")

    with pytest.raises(
        plan_authority.PlanPublicationError,
        match="different authored inputs",
    ):
        plan_authority.prepare_consumer_view(
            consumer,
            selected_authority=selected,
        )


def test_prepare_consumer_view_refuses_final_destination_equal_to_live_shot(
    tmp_path: Path,
) -> None:
    hostile_root = tmp_path / "hostile-run"
    shot = hostile_root / "scratch" / "plan-consumer-view"
    shot.mkdir(parents=True)
    _write_plan_workspace(shot)
    sentinel = shot / "canonical-sentinel.txt"
    sentinel.write_text("preserve live shot\n", encoding="utf-8")
    planning = run_artifacts.create(shot, "plan")
    plan_authority.publish_current(
        shot,
        planning,
        outcome="clean_with_deferred",
    )
    before = {
        path.relative_to(shot).as_posix(): path.read_bytes()
        for path in shot.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    hostile_layout = run_artifacts.RunLayout(
        shot=shot,
        run_id="hostile",
        root=hostile_root,
    )

    with pytest.raises(
        plan_authority.PlanPublicationError,
        match="exact canonical",
    ):
        plan_authority.prepare_consumer_view(hostile_layout)

    after = {
        path.relative_to(shot).as_posix(): path.read_bytes()
        for path in shot.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert after == before
    assert sentinel.read_text(encoding="utf-8") == "preserve live shot\n"


def test_prepare_consumer_view_refuses_foreign_shot_at_computed_destination(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "shot-a"
    shot.mkdir()
    _write_plan_workspace(shot)
    planning = run_artifacts.create(shot, "plan")
    plan_authority.publish_current(
        shot,
        planning,
        outcome="clean_with_deferred",
    )

    foreign_root = tmp_path / "forged-run-root"
    foreign_shot = foreign_root / "scratch" / "plan-consumer-view"
    foreign_shot.mkdir(parents=True)
    (foreign_shot / "shot.json").write_bytes(
        b'{"shot":"foreign","milestones":{}}\n'
    )
    (foreign_shot / "sentinel.bin").write_bytes(b"foreign-shot-must-survive\x00")
    before = {
        path.relative_to(foreign_shot).as_posix(): path.read_bytes()
        for path in foreign_shot.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    forged = run_artifacts.RunLayout(
        shot=shot,
        run_id="forged",
        root=foreign_root,
    )

    with pytest.raises(
        plan_authority.PlanPublicationError,
        match="exact canonical",
    ):
        plan_authority.prepare_consumer_view(forged)

    after = {
        path.relative_to(foreign_shot).as_posix(): path.read_bytes()
        for path in foreign_shot.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert after == before
    assert foreign_shot.is_dir()


def test_population_rejects_temp_rebound_to_canonical_shot_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_plan_workspace(tmp_path)
    planning = run_artifacts.create(tmp_path, "plan")
    plan_authority.publish_current(
        tmp_path,
        planning,
        outcome="clean_with_deferred",
    )
    consumer = run_artifacts.create(tmp_path, "consumer")
    protected_files = (
        tmp_path / "brief.md",
        tmp_path / "refs" / "hero.txt",
        tmp_path / "plans" / "global.md",
        tmp_path / "layers.json",
    )
    protected_directories = (
        tmp_path / "refs",
        tmp_path / "plans",
    )
    before_bytes = {path: path.read_bytes() for path in protected_files}
    before_directories = {
        path: (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
        )
        for path in protected_directories
    }
    original = plan_consumer_view_projection.ensure_directory
    attacked = False
    detached = consumer.scratch / "detached-consumer-view"

    def rebind_before_first_population_write(
        capability: object,
        relative: str | Path,
    ) -> None:
        nonlocal attacked
        if not attacked:
            temporaries = tuple(
                consumer.scratch.glob(".plan-consumer-view.tmp-*")
            )
            assert len(temporaries) == 1
            temporary = temporaries[0]
            os.rename(temporary, detached)
            temporary.symlink_to(tmp_path, target_is_directory=True)
            attacked = True
        original(capability, relative)  # type: ignore[arg-type]

    monkeypatch.setattr(
        plan_consumer_view_projection,
        "ensure_directory",
        rebind_before_first_population_write,
    )

    with pytest.raises(plan_authority.PlanPublicationError):
        plan_authority.prepare_consumer_view(consumer)

    assert attacked
    assert detached.is_dir()
    assert (detached / ".plan-consumer-view.json").is_file()
    assert {path: path.read_bytes() for path in protected_files} == before_bytes
    assert {
        path: (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
        )
        for path in protected_directories
    } == before_directories


def test_exact_planning_input_identity_rejects_full_decision_suffix_drift(
    tmp_path: Path,
) -> None:
    (tmp_path / "brief.md").write_text("# brief\n", encoding="utf-8")
    (tmp_path / "refs").mkdir()
    (tmp_path / "state").mkdir()
    decisions = tmp_path / "state" / "plan-resolutions.jsonl"
    decisions.write_text('{"id":"one"}\n', encoding="utf-8")
    authored, decision_identity = exact_planning_input_identity(tmp_path)

    require_exact_planning_input_identity(
        tmp_path,
        expected_authored_inputs=authored,
        expected_decision_inputs=decision_identity,
    )
    with decisions.open("a", encoding="utf-8") as handle:
        handle.write('{"id":"two"}\n')

    with pytest.raises(
        plan_authority.PlanPublicationError,
        match="decision inputs changed",
    ):
        require_exact_planning_input_identity(
            tmp_path,
            expected_authored_inputs=authored,
            expected_decision_inputs=decision_identity,
        )
