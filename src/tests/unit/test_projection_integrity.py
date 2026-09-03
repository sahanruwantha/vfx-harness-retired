"""Instruments must not read a frame or a frustum they were never pointed at.

Run 20260824T103842Z-afec73 sealed a work unit on two impossible readings:

- `keyframe_schedule` excursed through its 16 sample frames and parked the scene at
  f240, so the `bbox_height` rows that followed measured frame 240 while their identity
  said f1 and f36 — identically, which is how "identical value at two frames the camera
  travels between" appeared in an accepted report.
- The projection accepted off-frustum vertices, so a camera facing AWAY from its subject
  read a "normalized" height of 1132.53608 and passed a `>= 0.25` target.

The mechanisms: every probe row establishes its own declared frame and depsgraph, and
projection clips geometry to the camera frustum before the perspective divide, so every
coordinate is inside [0, 1] by construction and no-intersection is an absent reading
(fail closed), never a comparable number.
"""

from __future__ import annotations

import inspect

from vfx_harness.blender.geom import BOX_EDGES, frustum_union_ndc, project_clip_point
from vfx_harness.blender.tools import _check_args_error, _check_report
from vfx_harness.evidence import scene_checks


def _probe_source() -> str:
    return inspect.getsource(scene_checks._blender_probe)


# --------------------------------------------------------------------------- #
# frustum_union_ndc — the one projection implementation                        #
# --------------------------------------------------------------------------- #
def test_inside_points_map_to_top_left_screen_bbox() -> None:
    rec = frustum_union_ndc([(-0.2, 0.4, 0.0, 1.0), (0.2, -0.2, 0.0, 1.0)])
    assert rec is not None
    assert rec["bbox"] == [0.4, 0.3, 0.6, 0.6]
    assert rec["points_inside"] == 2 and rec["points_total"] == 2


def test_point_projection_preserves_off_frame_coordinates_without_marker_geometry() -> None:
    assert project_clip_point((0.0, 0.0, 0.0, 1.0)) == {
        "screen": [0.5, 0.5],
        "in_front": True,
        "in_frustum": True,
        "clip_w": 1.0,
    }
    assert project_clip_point((3.0, 0.0, 0.0, 1.0)) == {
        "screen": [2.0, 0.5],
        "in_front": True,
        "in_frustum": False,
        "clip_w": 1.0,
    }
    assert project_clip_point((0.0, 0.0, 0.0, -1.0))["screen"] is None


def test_projection_check_is_selector_free_and_teaches_read_only_probe() -> None:
    args = {"frame": 39, "points": [[0, 8, 0], [0, 8, 26]]}
    assert _check_args_error("projection", args) is None
    assert "exactly [x, y, z]" in _check_args_error(
        "projection", {"frame": 39, "points": [[0, 8]]}
    )
    report = _check_report(
        "projection",
        {
            "ok": True,
            "frame": 39,
            "camera": "camera",
            "points": [
                {
                    "world": [0.0, 8.0, 0.0],
                    "screen": None,
                    "in_front": False,
                    "in_frustum": False,
                    "clip_w": -1.0,
                }
            ],
        },
    )
    assert "do not create marker meshes" in report


def test_grazing_segment_is_clipped_to_the_frame_never_a_blowup() -> None:
    # One endpoint a hair in front of the lens plane, far off-axis: raw projection of
    # this class produced the 1132.53608 "normalized" height that sealed the unit.
    rec = frustum_union_ndc([(0.0, 0.0, 0.0, 1.0), (0.0, 900.0, 0.0005, 0.0005)], [(0, 1)])
    assert rec is not None
    assert 0.0 <= rec["height"] <= 1.0
    assert all(0.0 <= c <= 1.0 for c in rec["bbox"])


def test_segment_leaving_the_frame_contributes_its_visible_portion_only() -> None:
    rec = frustum_union_ndc([(0.0, 0.0, 0.0, 1.0), (5.0, 0.0, 0.0, 1.0)], [(0, 1)])
    assert rec is not None
    assert rec["bbox"] == [0.5, 0.5, 1.0, 0.5]


def test_no_frustum_intersection_is_an_absent_reading() -> None:
    # fully off-frame (right of the frustum), connected by an edge
    assert frustum_union_ndc([(2.6, 0.4, 0.0, 1.0), (3.0, 0.7, 0.0, 1.0)], [(0, 1)]) is None
    # entirely behind the camera (w < 0)
    assert frustum_union_ndc([(0.0, 0.0, -3.0, -1.0), (0.5, 0.0, -3.0, -1.0)], [(0, 1)]) is None
    assert frustum_union_ndc([]) is None


def test_segment_crossing_from_behind_the_camera_is_clipped_at_the_near_plane() -> None:
    # front endpoint at screen centre; back endpoint behind the camera. The visible
    # portion must stay bounded even though the raw back endpoint has w < 0.
    rec = frustum_union_ndc([(0.0, 0.0, 0.0, 1.0), (0.0, -2.0, -3.0, -1.0)], [(0, 1)])
    assert rec is not None
    assert all(0.0 <= c <= 1.0 for c in rec["bbox"])


def test_beyond_far_plane_is_not_visible() -> None:
    assert frustum_union_ndc([(0.0, 0.0, 5.0, 1.0)]) is None


def test_box_edges_cover_every_bound_box_corner() -> None:
    assert len(BOX_EDGES) == 12
    assert {i for pair in BOX_EDGES for i in pair} == set(range(8))


# --------------------------------------------------------------------------- #
# probe row isolation — each row measures its declared frame                   #
# --------------------------------------------------------------------------- #
def test_every_probe_row_reestablishes_frame_and_depsgraph() -> None:
    probe = _probe_source()
    loop = probe.index("for row in _rows:")
    frame_reset = probe.index("_scene.frame_set(_FRAME)", loop)
    row_dg = probe.index("_row_dg=bpy.context.evaluated_depsgraph_get()", loop)
    dispatch = probe.index("if kind=='object_count'", loop)
    assert frame_reset < dispatch and row_dg < dispatch


def test_probe_has_no_ambient_batch_depsgraph() -> None:
    # The pre-fix probe captured one depsgraph for the whole batch; temporal kinds then
    # re-framed the scene and every later row measured stale state through it.
    probe = _probe_source()
    header = probe[: probe.index("for row in _rows:")]
    assert "_dg=bpy.context.evaluated_depsgraph_get()" not in header


def test_projection_error_names_the_declared_frame() -> None:
    probe = _probe_source()
    assert "intersects the camera frustum" in probe
    assert "at frame {{_FRAME}}" in probe  # doubled braces: probe source is an f-string template
    # the no-camera refusal lives in the shared matrix builder now
    from vfx_harness.blender import checks

    assert "scene has no active camera to project through" in inspect.getsource(checks.camera_clip_matrix)


def test_temporal_kinds_still_excurse_but_cannot_reframe_siblings() -> None:
    # keyframe_schedule legitimately visits its sample frames; the guarantee is the
    # NEXT row's prologue, not a ban on excursions.
    probe = _probe_source()
    schedule = probe.index("kind=='keyframe_schedule'")
    assert "frame_set(int(sample['frame']))" in probe[schedule:]


def test_no_camera_instruments_teach_the_camera_provider_rule() -> None:
    """A pre-camera producer must learn where camera-relative evidence is due, not a stack."""
    from vfx_harness.blender import checks

    assert "camera-providing unit" in checks.NO_CAMERA_RULE
    assert "owes no render or projection" in checks.NO_CAMERA_RULE
    assert "NO_CAMERA_RULE" in inspect.getsource(checks.camera_clip_matrix)
