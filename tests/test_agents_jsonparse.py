"""The shared lenient JSON loader — strict first, string-aware repair only on failure."""

from __future__ import annotations

import pytest

from agents.jsonparse import loads_json


def test_parses_clean_json():
    assert loads_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_extracts_object_from_surrounding_prose():
    assert loads_json('sure —\n{"kind": "3D"}\nthanks')["kind"] == "3D"


def test_keeps_nested_objects_intact():
    # greedy outermost braces — a nested object (e.g. a critic's "dimensions") must survive
    assert loads_json('{"a": 1, "dims": {"x": true, "y": false}}')["dims"] == {"x": True, "y": False}


def test_reads_a_fenced_block_with_a_nested_object():
    fenced = "```json\n{\"score\": 0.7, \"dims\": {\"x\": true}}\n```"
    assert loads_json(fenced) == {"score": 0.7, "dims": {"x": True}}


def test_repairs_a_stray_semicolon_separator():
    # the real supervisor failure: a ';' where a ',' belongs between two members
    data = loads_json('{"rationale": "build it."; "kind": "3D"}')
    assert data == {"rationale": "build it.", "kind": "3D"}


def test_repair_leaves_semicolons_inside_strings_alone():
    data = loads_json('{"note": "shafts; drift"; "n": 1}')  # inner ';' kept, separator ';' fixed
    assert data == {"note": "shafts; drift", "n": 1}


def test_repairs_trailing_commas():
    assert loads_json('{"xs": [1, 2,], "y": 3,}') == {"xs": [1, 2], "y": 3}


def test_no_object_raises():
    with pytest.raises(ValueError):
        loads_json("no json here at all")


def test_unrepairable_raises():
    with pytest.raises(ValueError):
        loads_json('{"a": : :}')
