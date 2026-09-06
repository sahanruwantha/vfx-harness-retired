"""A row whose activation begins at this layer is not testable at its start (HIR-0236).

room_1046_opening run 20260905T233904Z-f95efe deadlocked:

    layer 2 cannot start: prior interface revalidation failed:
      cam-clearance-building=None target >= 0.5 (repair layer 1)

The row, from the selected view:

    {"id": "cam-clearance-building", "kind": "path_clearance_min", "op": "min", "lo": 0.5,
     "frames": [151, 176], "activates_at": "2", "owner_layer": "1", "fault_owner": "1",
     "roles": ["camera.rig"], "compare_roles": ["building.*"], "lifecycle": "persistent"}

`activates_at: "2"`, evaluated at layer 2's START. `compare_roles: ["building.*"]`
resolves to nothing before layer 2 builds, so the metric is `None` and `None >= 0.5`
scores as a failure. Layer 1 cannot satisfy it -- deferring it was correct -- and layer 2
cannot start to build the subject that would. No operator transaction clears that.

HIR-0134 already excluded this for camera-owned `bbox_*` rows, via a helper that filters
on `BBOX_KINDS`. `activates_at` is a general lifecycle field, so a `path_clearance_min`
row carrying it fell straight through.
"""

from __future__ import annotations

from vfx_harness.evidence.scene_checks.functional import prior_interface_rows

DEFERRED = {
    "id": "cam-clearance-building", "kind": "path_clearance_min", "op": "min", "lo": 0.5,
    "frame": 151, "activates_at": "2", "owner_layer": "1", "fault_owner": "1",
    "roles": ["camera.rig"], "compare_roles": ["building.*"], "lifecycle": "persistent",
}
PLAIN = {
    "id": "cam-lens", "kind": "object_property", "op": "eq", "value": 35, "tol": 0.1,
    "frame": 1, "activates_at": "1", "owner_layer": "1", "fault_owner": "1",
    "roles": ["camera.rig"], "property": "data.lens", "lifecycle": "persistent",
}


def test_the_deferred_row_is_not_evaluated_at_the_layer_it_activates_on() -> None:
    selected = [r["id"] for r in prior_interface_rows([DEFERRED, PLAIN], 2)]
    assert "cam-clearance-building" not in selected, selected


def test_it_is_enforced_once_the_subject_exists() -> None:
    """Skipping at the activation layer must not retire the row."""
    selected = [r["id"] for r in prior_interface_rows([DEFERRED, PLAIN], 3)]
    assert "cam-clearance-building" in selected, selected


def test_an_ordinary_earlier_layer_row_is_still_revalidated() -> None:
    """The exclusion must not widen into 'skip prior interfaces'."""
    for layer in (2, 3):
        selected = [r["id"] for r in prior_interface_rows([DEFERRED, PLAIN], layer)]
        assert "cam-lens" in selected, (layer, selected)
