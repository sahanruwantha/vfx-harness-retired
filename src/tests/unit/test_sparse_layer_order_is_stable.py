"""Authored order survives when it is already a valid topological order (HIR-0221).

hansa_silk_road's build died on `selected authority capsules do not preserve the stable
topological layer order`. Both sides were derived from the same DAG, and the authored
order was itself topologically valid — the sorter simply chose a different valid order,
so the capsule set and the sorter disagreed and judgment replay capture refused the shot.

The cause was that newly-unlocked layers were appended to the ready queue and only the
new batch was sorted, so a layer unlocked early sat ahead of a lower-authored layer
unlocked later. The docstring already claimed authored position as the tie-break; the
implementation applied it per batch rather than across the queue.
"""

from __future__ import annotations

import pytest

from vfx_harness.domain.work_units.capabilities import (
    strict_topological_sparse_layer_ids,
    topological_sparse_layer_ids,
)


def _layers(edges: dict[str, list[str]]) -> list[dict]:
    return [{"id": key, "jit": {"depends_on_layers": value}} for key, value in edges.items()]


HANSA = {
    "1": [],
    "2": ["1"],
    "3": ["1", "2"],
    "4": ["1"],
    "5": ["1", "2"],
    "6": ["1", "2", "3", "5"],
    "7": ["1", "2", "3", "4", "6"],
}


def test_authored_order_is_kept_when_it_is_already_topological() -> None:
    """The shot that found it: 4 unlocks with 1, 3 only with 2, authored says 3 first."""
    assert topological_sparse_layer_ids(_layers(HANSA)) == ("1", "2", "3", "4", "5", "6", "7")
    assert strict_topological_sparse_layer_ids(_layers(HANSA)) == (
        "1", "2", "3", "4", "5", "6", "7",
    )


def test_a_layer_unlocked_early_does_not_overtake_a_lower_authored_one() -> None:
    """The minimal shape, independent of the shot that exposed it.

    `b` unlocks as soon as `a` completes; `c` only once `x` does. Authored order puts
    `c` first, and both orders are topologically valid, so the tie-break decides.
    """
    edges = {"a": [], "x": ["a"], "c": ["a", "x"], "b": ["a"]}
    order = topological_sparse_layer_ids(_layers(edges))
    assert order.index("c") < order.index("b"), order
    assert order == ("a", "x", "c", "b"), order


def test_dependencies_still_precede_dependants_when_authored_order_is_wrong() -> None:
    """Stability is a tie-break, never a licence to violate an edge."""
    edges = {"late": [], "early": ["late"]}
    assert topological_sparse_layer_ids(_layers(edges)) == ("late", "early")

    reversed_authoring = [
        {"id": "early", "jit": {"depends_on_layers": ["late"]}},
        {"id": "late", "jit": {"depends_on_layers": []}},
    ]
    assert topological_sparse_layer_ids(reversed_authoring) == ("late", "early")


def test_a_cyclic_dag_is_still_refused() -> None:
    cyclic = _layers({"a": ["b"], "b": ["a"]})
    with pytest.raises(ValueError, match="cyclic or not topologically executable"):
        strict_topological_sparse_layer_ids(cyclic)
