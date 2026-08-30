"""find_recipe ranks against the active unit's mutation roles and can abstain (HIR-0033).

Run 20260826T170413Z-ba2b4c pulled camera-parented-spill-lights into cam_spine, then
tried a temp sun that was a scope violation. Keyword hits are not permission.
"""

from __future__ import annotations

from vfx_harness.knowledge.recipes import (
    RecipeContextBudget,
    out_of_scope_hits,
    recipe_in_scope,
    recipe_search_response,
    recipe_sections,
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


def test_recipe_retrieval_requires_section_or_explicit_full_body() -> None:
    hits = [{
        "name": "blender-5-api",
        "when": "Blender API mismatch",
        "body": "Overview notes.\n\n```python\n# compositor sockets\nLARGE BODY\n```",
    }]

    fuzzy, fuzzy_used = recipe_search_response("compositor vector blur node", hits)
    exact, exact_used = recipe_search_response("blender 5 api", hits)
    section, section_used = recipe_search_response("blender-5-api#snippet-1", hits)
    full, full_used = recipe_search_response("blender-5-api#full", hits)

    assert "LARGE BODY" not in fuzzy
    assert "blender-5-api" in fuzzy
    assert fuzzy_used == []
    assert "LARGE BODY" not in exact
    assert "snippet-1" in exact
    assert exact_used == []
    assert "LARGE BODY" in section
    assert section_used == ["blender-5-api"]
    assert "LARGE BODY" in full
    assert full_used == ["blender-5-api"]


def test_recipe_sections_are_stable_across_prose_and_code_blocks() -> None:
    sections = recipe_sections("First notes.\n```python\n# one\nx = 1\n```\nSecond notes.")
    assert [row["id"] for row in sections] == ["notes-1", "snippet-1", "notes-2"]


def test_recipe_body_budget_refuses_rereads_and_a_fourth_fragment() -> None:
    budget = RecipeContextBudget(max_bodies=3, max_chars=100)
    assert budget.admit("volume#notes-1", 20) == ""
    assert "already loaded" in budget.admit("volume#notes-1", 20)
    assert budget.admit("volume#snippet-1", 20) == ""
    assert budget.admit("volume#notes-2", 20) == ""
    assert "BUDGET EXHAUSTED" in budget.admit("dark#notes-1", 20)


def test_recipe_body_budget_also_bounds_total_replayed_characters() -> None:
    budget = RecipeContextBudget(max_bodies=3, max_chars=50)
    assert budget.admit("a#notes-1", 30) == ""
    assert "BUDGET EXHAUSTED" in budget.admit("b#notes-1", 30)
