"""Unit coverage for the activation-window helper itself (HIR-0236).

Kept apart from `test_activation_layer_start_exclusion.py` deliberately: that file
imports only `prior_interface_rows`, which already exists, so on the pre-fix tree it
fails on the row being evaluated rather than on a missing symbol.
"""

from __future__ import annotations

from vfx_harness.domain.contracts import activation_begins_at

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


def test_the_window_begins_only_at_the_activation_layer() -> None:
    """The defect: HIR-0134's helper filters on BBOX_KINDS, `activates_at` does not."""
    assert DEFERRED["kind"] == "path_clearance_min"
    assert activation_begins_at(DEFERRED, 2) is True
    assert activation_begins_at(DEFERRED, 3) is False
    assert activation_begins_at(PLAIN, 2) is False


def test_a_malformed_row_is_not_silently_treated_as_deferred() -> None:
    """Failing lifecycle validation must not become a free pass out of revalidation."""
    broken = dict(DEFERRED)
    broken.pop("fault_owner")
    assert activation_begins_at(broken, 2) is False
