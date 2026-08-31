"""Atomic resolution-ledger publication at unit and acceptance boundaries."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread, current_thread
from typing import Any

import pytest

from vfx_harness.orchestration import plan_due
from vfx_harness.orchestration.plan_authority import PlanBundle


def _write_bundle(
    root: Path,
    *,
    digest: str,
    obligations: list[dict[str, Any]],
) -> PlanBundle:
    root.mkdir(parents=True)
    (root / "obligations.json").write_text(
        json.dumps(
            {
                "schema": "vfx-harness.obligations/v1",
                "obligations": obligations,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "assumptions.json").write_text(
        json.dumps(
            {
                "schema": "vfx-harness.assumptions/v1",
                "assumptions": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return PlanBundle(
        shot=root.parent,
        run_id="plan-run",
        root=root,
        content_hash=digest,
        artifacts=(),
        outcome="clean_with_deferred",
    )


def _obligation(
    identifier: str,
    *,
    evidence_id: str,
    due: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": identifier,
        "statement": f"prove {identifier}",
        "requirement_ids": [f"R-{identifier}"],
        "owner": "1.lock",
        "due": due,
        "evidence": [{"kind": "scene_contract", "id": evidence_id}],
    }


def _configure_selected_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    obligations: list[dict[str, Any]],
) -> tuple[PlanBundle, Path]:
    bundle = _write_bundle(
        tmp_path / "bundle",
        digest="a" * 64,
        obligations=obligations,
    )
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(plan_due, "resolve_current", lambda _shot: bundle)
    monkeypatch.setattr(plan_due, "shot_state_dir", lambda _shot: state)
    return bundle, state / plan_due.RESOLUTIONS


def _run_with_unit_holding_lock_first(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unit_writer: Callable[[], tuple[str, ...]],
    acceptance_writer: Callable[[], tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """Start both writers while deterministically forcing the unit lock first."""

    original_lock = plan_due._locked_resolutions
    unit_acquired = Event()
    acceptance_attempted = Event()
    acceptance_acquired = Event()
    release_unit = Event()

    @contextmanager
    def observed_lock(path: Path):
        name = current_thread().name
        if name == "acceptance-writer":
            acceptance_attempted.set()
        with original_lock(path):
            if name == "unit-writer":
                unit_acquired.set()
                if not release_unit.wait(timeout=5):
                    raise AssertionError("test did not release the unit ledger lock")
            elif name == "acceptance-writer":
                acceptance_acquired.set()
            yield

    monkeypatch.setattr(plan_due, "_locked_resolutions", observed_lock)
    results: dict[str, tuple[str, ...]] = {}
    errors: list[tuple[str, BaseException]] = []

    def invoke(name: str, writer: Callable[[], tuple[str, ...]]) -> None:
        try:
            results[name] = writer()
        except BaseException as exc:  # pragma: no cover - assertion reports exact thread error
            errors.append((name, exc))

    unit_thread = Thread(
        target=invoke,
        args=("unit", unit_writer),
        name="unit-writer",
    )
    acceptance_thread = Thread(
        target=invoke,
        args=("acceptance", acceptance_writer),
        name="acceptance-writer",
    )
    unit_thread.start()
    assert unit_acquired.wait(timeout=5)
    acceptance_thread.start()
    assert acceptance_attempted.wait(timeout=5)
    assert not acceptance_acquired.is_set()
    release_unit.set()
    unit_thread.join(timeout=5)
    acceptance_thread.join(timeout=5)
    assert not unit_thread.is_alive()
    assert not acceptance_thread.is_alive()
    assert acceptance_acquired.is_set()
    assert errors == []
    return results


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_concurrent_unit_and_acceptance_writers_preserve_distinct_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, ledger = _configure_selected_bundle(
        tmp_path,
        monkeypatch,
        obligations=[
            _obligation(
                "O-unit",
                evidence_id="unit-proof",
                due={"kind": "unit_completion", "layer": "1", "unit": "lock"},
            ),
            _obligation(
                "O-acceptance",
                evidence_id="acceptance-proof",
                due={"kind": "before_acceptance"},
            ),
        ],
    )

    results = _run_with_unit_holding_lock_first(
        monkeypatch,
        unit_writer=lambda: plan_due.resolve_unit_completion(
            tmp_path,
            layer="1",
            unit="lock",
            passed_evidence={("scene_contract", "unit-proof")},
        ),
        acceptance_writer=lambda: plan_due.resolve_acceptance_completion(
            tmp_path,
            passed_evidence={("scene_contract", "acceptance-proof")},
            expected_bundle_digest=bundle.content_hash,
        ),
    )

    assert results == {
        "unit": ("O-unit",),
        "acceptance": ("O-acceptance",),
    }
    assert [row["id"] for row in _ledger_rows(ledger)] == [
        "O-unit",
        "O-acceptance",
    ]


def test_concurrent_writers_reread_under_lock_and_emit_no_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, ledger = _configure_selected_bundle(
        tmp_path,
        monkeypatch,
        obligations=[
            _obligation(
                "O-shared",
                evidence_id="shared-proof",
                due={"kind": "before_acceptance"},
            )
        ],
    )

    results = _run_with_unit_holding_lock_first(
        monkeypatch,
        unit_writer=lambda: plan_due.resolve_unit_completion(
            tmp_path,
            layer="1",
            unit="lock",
            passed_evidence={("scene_contract", "shared-proof")},
        ),
        acceptance_writer=lambda: plan_due.resolve_acceptance_completion(
            tmp_path,
            passed_evidence={("scene_contract", "shared-proof")},
            expected_bundle_digest=bundle.content_hash,
        ),
    )

    assert results == {"unit": ("O-shared",), "acceptance": ()}
    assert [row["id"] for row in _ledger_rows(ledger)] == ["O-shared"]


@pytest.mark.parametrize("boundary", ["unit", "acceptance"])
def test_bundle_change_at_final_compare_prevents_resolution_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    first, ledger = _configure_selected_bundle(
        tmp_path,
        monkeypatch,
        obligations=[
            _obligation(
                "O-proof",
                evidence_id="proof",
                due=(
                    {"kind": "unit_completion", "layer": "1", "unit": "lock"}
                    if boundary == "unit"
                    else {"kind": "before_acceptance"}
                ),
            )
        ],
    )
    changed = _write_bundle(
        tmp_path / "changed-bundle",
        digest="b" * 64,
        obligations=[],
    )
    selected = iter((first, first, changed))
    monkeypatch.setattr(plan_due, "resolve_current", lambda _shot: next(selected))

    with pytest.raises(
        ValueError,
        match=r"changed before .*resolutions could be published",
    ):
        if boundary == "unit":
            plan_due.resolve_unit_completion(
                tmp_path,
                layer="1",
                unit="lock",
                passed_evidence={("scene_contract", "proof")},
            )
        else:
            plan_due.resolve_acceptance_completion(
                tmp_path,
                passed_evidence={("scene_contract", "proof")},
                expected_bundle_digest=first.content_hash,
            )

    assert not ledger.exists()
