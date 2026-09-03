"""A namespace object_count cannot exclude the descendants sibling rows require (HIR-0178)."""

from __future__ import annotations

from vfx_harness.evidence.scene_checks import namespace_count_contradictions, validate_row_set


def _base(id: str, kind: str, roles: list[str], **extra) -> dict:
    return {
        "id": id,
        "kind": kind,
        "roles": roles,
        "owner_layer": "1",
        "fault_owner": "1",
        "activates_at": "1",
        "lifecycle": "layer",
        "axis": "window_alignment",
        **extra,
    }


def _count(id: str, roles: list[str], op: str = "eq", **bound) -> dict:
    return _base(id, "object_count", roles, op=op, **bound)


def _schedule(id: str, role: str, **extra) -> dict:
    return _base(
        id,
        "keyframe_schedule",
        [role],
        op="min",
        lo=0,
        samples=[{"frame": 1, "values": {"location.0": 1.0}}, {"frame": 213, "values": {"location.0": 1.0}}],
        **extra,
    )


def test_literal_namespace_count_below_required_descendants_is_refused() -> None:
    rows = [
        _count("parent-count", ["camera.targets"], value=1),
        _schedule("window-fixed", "camera.targets.window_target"),
        _schedule("door-fixed", "camera.targets.door_target"),
        _base("door-x", "projected_origin_x", ["camera.targets.door_target"], frame=213, op="band", lo=0.4, hi=0.6),
    ]
    findings = namespace_count_contradictions(rows)
    assert [f["id"] for f in findings] == ["parent-count"]
    assert findings[0]["descendants"] == ["camera.targets.door_target", "camera.targets.window_target"]
    assert findings[0]["minimum"] == 2
    message = findings[0]["message"]
    assert "object_count eq 1 over roles ['camera.targets'] can never hold" in message
    assert "matches its dotted descendants (HIR-0147)" in message
    assert "camera.targets.door_target required by ['door-fixed', 'door-x']" in message
    assert "'camera.targets.root'" in message
    assert message in validate_row_set(rows)


def test_leaf_role_raised_bound_or_unbounded_counts_pass() -> None:
    siblings = [
        _schedule("window-fixed", "camera.targets.window_target"),
        _schedule("door-fixed", "camera.targets.door_target"),
    ]
    assert namespace_count_contradictions([_count("root", ["camera.targets.root"], value=1), *siblings]) == []
    assert namespace_count_contradictions([_count("all", ["camera.targets"], value=3), *siblings]) == []
    assert namespace_count_contradictions([_count("all", ["camera.targets"], value=2), *siblings]) == []
    assert namespace_count_contradictions([_count("floor", ["camera.targets"], op="min", lo=1), *siblings]) == []
    assert namespace_count_contradictions([_count("band", ["camera.targets"], op="band", lo=1, hi=1), *siblings]) != []
    # A deeper role satisfies its ancestor's row with the same host.
    nested = [
        _schedule("arm", "camera.targets.arm"),
        _schedule("arm-tip", "camera.targets.arm.tip"),
    ]
    assert namespace_count_contradictions([_count("one", ["camera.targets"], value=1), *nested]) == []


def test_other_layer_descendants_bind_only_a_persistent_count() -> None:
    later = [
        _schedule("later-a", "camera.targets.later_a", owner_layer="3", activates_at="3"),
        _schedule("later-b", "camera.targets.later_b", owner_layer="3", activates_at="3"),
    ]
    assert namespace_count_contradictions([_count("layer-count", ["camera.targets"], value=1), *later]) == []
    persistent = _count("persistent-count", ["camera.targets"], value=1, lifecycle="persistent")
    findings = namespace_count_contradictions([persistent, *later])
    assert [f["id"] for f in findings] == ["persistent-count"]
    assert findings[0]["minimum"] == 2
    # One required descendant satisfies a count of one by itself: no parent host is owed.
    assert namespace_count_contradictions([persistent, later[0]]) == []
