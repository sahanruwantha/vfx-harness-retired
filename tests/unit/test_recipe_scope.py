"""find_recipe ranks against the active unit's mutation roles and can abstain (HIR-0033).

Run 20260826T170413Z-ba2b4c pulled camera-parented-spill-lights into cam_spine, then
tried a temp sun that was a scope violation. Keyword hits are not permission.
"""

from __future__ import annotations

from vfx_harness.knowledge.recipes import (
    out_of_scope_hits,
    recipe_in_scope,
    search_recipes,
)


def test_spill_lights_abstain_when_unit_cannot_mutate_lights() -> None:
    roles = ("cam_rig", "cam.blockout_fg", "cam.iris_face")
    hits = search_recipes("camera parented spill lights", mutation_roles=roles)
    assert all(rec["name"] != "camera-parented-spill-lights" for rec in hits)
    blocked = out_of_scope_hits("camera parented spill lights", roles)
    assert any(rec["name"] == "camera-parented-spill-lights" for rec in blocked)


def test_roll_rig_stays_in_scope_for_cam_rig() -> None:
    hits = search_recipes("camera roll rig", mutation_roles=("cam_rig",))
    assert any(rec["name"] == "camera-roll-rig" for rec in hits)


def test_unscoped_search_still_returns_keyword_hits() -> None:
    hits = search_recipes("make the city look real")
    assert any(rec["name"] == "night-city-field" for rec in hits)


def test_api_recipe_is_always_in_scope() -> None:
    rec = {"name": "blender-5-api", "tags": ["api", "blender5"], "requires_roles": []}
    assert recipe_in_scope(rec, ("cam_rig",)) is True
    assert recipe_in_scope(rec, ()) is True
