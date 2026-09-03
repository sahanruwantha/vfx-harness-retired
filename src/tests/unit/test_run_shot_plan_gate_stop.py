"""The per-layer plan gate stops with typed transaction authority (HIR-0187).

Run 20260903T171506Z-4eb421 on a real shot rejected the selected layer-2 view with
three precise, deterministic findings and then died as
``harness_defect: The 'layer-2-plan-gate' boundary returned without typed stop
authority``.  The gate had named the unit, the contract and the legal repair; the
driver read only the subprocess exit code, so no transaction existed and every
later invocation repeated the same rejection for no progress.

Scope follows ownership: a finding another layer owns belongs to that layer's own
transaction, and a plan-wide finding stays a reviewed global amendment.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vfx_harness.application import run_shot
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult


def _gate(*findings: Finding) -> GateResult:
    return GateResult("shot", list(findings), {})


def _owned(layer: str, check: str = "data-block-carrier") -> Finding:
    return Finding.in_layer(check, True, layer, "unit u contract c", "unpayable row", "add a carrier")


def _plan_wide(check: str = "authority") -> Finding:
    return Finding(check, True, "plans/current.json", "authority is stale", "republish")


_MISSING = object()


class _Recorder:
    """Captures which scope the boundary published, instead of writing a real stop."""

    def __init__(self) -> None:
        self.scope: str | None = None
        self.layer_id: str | None = None
        self.stopped: str | None = None


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()

    def fake_layer_stop(_layout, _result, *, layer_id):
        recorder.scope, recorder.layer_id = "layer_view", layer_id

    def fake_global_stop(_layout, _result):
        recorder.scope, recorder.layer_id = "global_plan", None

    def fake_stop_after_stage(_layout, _lease, _code, boundary_name, _controller=None):
        recorder.stopped = boundary_name

    monkeypatch.setattr(run_shot, "publish_layer_plan_gate_stop", fake_layer_stop)
    monkeypatch.setattr(run_shot, "publish_global_plan_gate_stop", fake_global_stop)
    monkeypatch.setattr(run_shot, "_stop_after_stage", fake_stop_after_stage)
    return recorder


def _selected():
    """Selected authority a layer-view amendment can actually amend."""
    return SimpleNamespace(
        assertion=SimpleNamespace(selection="selected", effective_view=object()),
        artifact_paths={},
    )


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    gate: GateResult,
    layer: str = "2",
    selected=_MISSING,
):
    if selected is _MISSING:
        selected = _selected()
    layout = SimpleNamespace(write_report=lambda *_a, **_k: None)
    shot = SimpleNamespace(folder=SimpleNamespace(name="shot"), id="shot")
    monkeypatch.setattr(
        run_shot, "_gate_selected_authority", lambda _layout, _shot: (selected, gate)
    )
    run_shot._gate_layer_authority(layout, object(), shot, layer, None)


def test_a_clean_gate_lets_the_layer_build(monkeypatch, boundary) -> None:
    _drive(monkeypatch, _gate())
    assert boundary.scope is None and boundary.stopped is None


def test_findings_this_layer_owns_become_a_dispatchable_layer_amendment(
    monkeypatch, boundary
) -> None:
    """The exact shape of run 20260903T171506Z-4eb421's layer-2 rejection."""
    _drive(monkeypatch, _gate(_owned("2"), _owned("2")))
    assert (boundary.scope, boundary.layer_id) == ("layer_view", "2")
    assert boundary.stopped == "layer-2-plan-gate"


def test_another_layers_finding_does_not_block_this_layer(monkeypatch, boundary) -> None:
    """composition-coverage on layer 1 belongs to layer 1's own transaction."""
    _drive(monkeypatch, _gate(_owned("1", "composition-coverage")))
    assert boundary.scope is None and boundary.stopped is None


def test_a_plan_wide_finding_stays_a_reviewed_global_amendment(
    monkeypatch, boundary
) -> None:
    _drive(monkeypatch, _gate(_plan_wide()))
    assert (boundary.scope, boundary.layer_id) == ("global_plan", None)


def test_a_mixed_rejection_escalates_to_the_global_scope(monkeypatch, boundary) -> None:
    """One unowned blocker means this layer's amendment cannot be the whole repair."""
    _drive(monkeypatch, _gate(_owned("2"), _plan_wide()))
    assert boundary.scope == "global_plan"


def test_the_layer_scope_survives_a_mix_of_owners(monkeypatch, boundary) -> None:
    """Layer 1's finding is filtered out; only layer 2's remain, so layer 2 is the owner."""
    _drive(monkeypatch, _gate(_owned("1", "composition-coverage"), _owned("2")))
    assert (boundary.scope, boundary.layer_id) == ("layer_view", "2")


def test_without_selected_authority_there_is_no_layer_view_to_amend(
    monkeypatch, boundary
) -> None:
    """The domain refuses a layer amendment with no selected view; the scope says so."""
    _drive(monkeypatch, _gate(_owned("2")), selected=None)
    assert boundary.scope == "global_plan"
