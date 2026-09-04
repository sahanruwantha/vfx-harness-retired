"""No blocking gate finding is left with nobody to dispatch it (HIR-0190).

HIR-0189 stopped one layer's finding from blocking another layer's session. That is
right, and it opened a hole: `_drive_layers` skips a layer whose build receipt still
verifies *before* it gates anything, so a finding owned by an already-passed layer was
filtered out of every gate the run did run and dispatched by nobody.

On `artifacts/room_1046_opening` that finding is layer 1's `composition-coverage` — its
camera authors no persistent `bbox_*` row for layer 3's `hero_window.*` at shared judge
frame 200, a rule newer than layer 1's view. The standalone gate exits 3 on it while
`vfx run --from 2` built layer 2 on that same rejected camera.

A build receipt says the layer's work passed. It does not say the layer's authority still
clears the current gate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vfx_harness.application import run_shot
from vfx_harness.evaluation.plan_gate.types import Finding, GateResult


def _gate(*findings: Finding) -> GateResult:
    return GateResult("shot", list(findings), {})


def _owned(layer: str, check: str = "composition-coverage") -> Finding:
    return Finding.in_layer(check, True, layer, "judge f200", "owner owes a row", "author it")


class _Recorder:
    def __init__(self) -> None:
        self.stopped: str | None = None
        self.prepared: object | None = None


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(
        run_shot,
        "publish_global_plan_gate_stop",
        lambda _layout, _result: "envelope:global_plan",
    )
    monkeypatch.setattr(
        run_shot,
        "_stop_after_stage",
        lambda _l, _lease, _c, name, _ctrl=None: setattr(recorder, "stopped", name),
    )
    return recorder


def _layout(recorder: _Recorder):
    return SimpleNamespace(
        write_report=lambda *_a, **_k: None,
        write_stop_envelope=lambda envelope: setattr(recorder, "prepared", envelope),
    )


def _shot():
    return SimpleNamespace(folder=SimpleNamespace(name="shot"), id="shot")


def _drive_lower_check(monkeypatch, recorder, gate, ids):
    monkeypatch.setattr(
        run_shot, "_gate_selected_authority", lambda _layout, _shot: (None, gate)
    )
    run_shot._stop_on_unreachable_owner(_layout(recorder), object(), _shot(), ids, None)


def test_a_finding_owned_outside_the_run_stops_it(monkeypatch, boundary) -> None:
    """`vfx run --from 2` cannot repair layer 1, so it must not silently build on it."""
    _drive_lower_check(monkeypatch, boundary, _gate(_owned("1")), ids=["2", "3", "4"])
    assert boundary.stopped == "layer-1-plan-gate"
    assert boundary.prepared == "envelope:global_plan"


def test_a_finding_owned_inside_the_run_is_left_to_its_own_layer(
    monkeypatch, boundary
) -> None:
    """Layer 2 is in range: its own gate boundary dispatches the amendment."""
    _drive_lower_check(monkeypatch, boundary, _gate(_owned("2")), ids=["2", "3", "4"])
    assert boundary.stopped is None


def test_a_plan_wide_finding_is_not_an_unreachable_owner(monkeypatch, boundary) -> None:
    """A finding nobody owns alone still blocks, but at the layer boundary, not here."""
    _drive_lower_check(
        monkeypatch, boundary, _gate(Finding("authority", True, "p", "stale")), ids=["2"]
    )
    assert boundary.stopped is None


def test_a_clean_gate_starts_the_run(monkeypatch, boundary) -> None:
    _drive_lower_check(monkeypatch, boundary, _gate(), ids=["1", "2"])
    assert boundary.stopped is None


def test_a_full_run_reaches_the_owner_and_does_not_stop_early(
    monkeypatch, boundary
) -> None:
    """With layer 1 in range the run repairs it rather than refusing to start."""
    _drive_lower_check(monkeypatch, boundary, _gate(_owned("1")), ids=["1", "2", "3"])
    assert boundary.stopped is None


def test_a_passed_layer_whose_authority_is_dirty_is_not_skip_authority(
    monkeypatch,
) -> None:
    """The receipt says the work passed; the gate says the authority is rejected."""
    monkeypatch.setattr(
        run_shot,
        "_gate_selected_authority",
        lambda _layout, _shot: (None, _gate(_owned("1"))),
    )
    assert not run_shot._layer_authority_is_clean(_layout(_Recorder()), _shot(), "1")
    assert run_shot._layer_authority_is_clean(_layout(_Recorder()), _shot(), "2")
