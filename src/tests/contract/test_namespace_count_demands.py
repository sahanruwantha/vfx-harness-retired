"""A sibling object_count is evidence of descendant population (HIR-0195).

Run 20260904T…-room layer 2 authored, in one candidate:

    facade-object-count         object_count eq 1  over exterior.facade
    facade-window-object-count  object_count min 12 over exterior.facade.window

A literal selector matches its dotted descendants (HIR-0147), so the first row reads at
least 13 in any scene satisfying the second. The builder measured that directly and
abstained after a full session:

    facade-object-count requires object_count(roles=["exterior.facade"]) eq 1 ... this
    literal selector matches its exact tag AND every dotted descendant

`namespace_count_contradictions` exists to refuse exactly this (HIR-0178) and missed it
twice over: it skipped every sibling whose kind was `object_count`, and it counted each
descendant namespace as one host rather than as the number its own row demands.
"""

from __future__ import annotations

import pytest

from vfx_harness.evidence.scene_checks.row_sets import namespace_count_contradictions


def _count(row_id: str, roles: list[str], **bound) -> dict:
    return {
        "id": row_id,
        "kind": "object_count",
        "roles": roles,
        "owner_layer": "2",
        "lifecycle": "layer",
        **bound,
    }


def _observed() -> list[dict]:
    return [
        _count("facade-object-count", ["exterior.facade"], op="eq", value=1),
        _count("facade-window-object-count", ["exterior.facade.window"], op="min", lo=12),
    ]


def test_the_observed_pair_is_refused() -> None:
    [finding] = namespace_count_contradictions(_observed())
    assert finding["id"] == "facade-object-count"
    assert finding["minimum"] == 12
    assert "exterior.facade.window" in finding["message"]


def test_the_message_names_both_legal_repairs() -> None:
    [finding] = namespace_count_contradictions(_observed())
    assert "leaf role" in finding["message"]
    assert "raise the bound" in finding["message"]


def test_counting_a_leaf_role_clears_it() -> None:
    rows = _observed()
    rows[0]["roles"] = ["exterior.facade.root"]
    assert not namespace_count_contradictions(rows)


def test_raising_the_bound_above_the_demand_clears_it() -> None:
    rows = _observed()
    rows[0] = _count("facade-object-count", ["exterior.facade"], op="min", lo=13)
    assert not namespace_count_contradictions(rows)


def test_a_bound_that_already_admits_the_demand_is_clean() -> None:
    """The check must not fire on a pair that is satisfiable."""
    rows = [
        _count("parent", ["a.b"], op="max", hi=20),
        _count("child", ["a.b.c"], op="min", lo=12),
    ]
    assert not namespace_count_contradictions(rows)


@pytest.mark.parametrize(("op", "bound"), [("max", {"hi": 5}), ("eq", {"value": 3})])
def test_the_demanded_count_drives_the_minimum_for_every_upper_bound(op, bound) -> None:
    rows = [
        _count("parent", ["a.b"], op=op, **bound),
        _count("child", ["a.b.c"], op="min", lo=12),
    ]
    [finding] = namespace_count_contradictions(rows)
    assert finding["minimum"] == 12


def test_a_non_count_sibling_still_demands_one_host() -> None:
    """The pre-existing behaviour is preserved: a non-count row needs one host."""
    rows = [
        _count("parent", ["a.b"], op="eq", value=0),
        {"id": "kf", "kind": "keyframe_schedule", "roles": ["a.b.c"], "owner_layer": "2", "lifecycle": "layer"},
    ]
    [finding] = namespace_count_contradictions(rows)
    assert finding["minimum"] == 1


def test_two_descendant_namespaces_sum_their_demands() -> None:
    rows = [
        _count("parent", ["a.b"], op="eq", value=1),
        _count("c1", ["a.b.c"], op="min", lo=4),
        _count("c2", ["a.b.d"], op="min", lo=6),
    ]
    [finding] = namespace_count_contradictions(rows)
    assert finding["minimum"] == 10


def test_a_row_never_contradicts_itself() -> None:
    assert not namespace_count_contradictions([_count("solo", ["a.b"], op="min", lo=3)])


def test_another_layers_row_does_not_bind_a_layer_scoped_count() -> None:
    rows = _observed()
    rows[1]["owner_layer"] = "3"
    assert not namespace_count_contradictions(rows)


def test_a_persistent_count_is_bound_by_every_layer() -> None:
    rows = _observed()
    rows[0]["lifecycle"] = "persistent"
    rows[1]["owner_layer"] = "3"
    assert namespace_count_contradictions(rows)
