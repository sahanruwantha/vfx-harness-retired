from __future__ import annotations

import pytest

from vfx_harness.domain.json_pointer import encode, format_finding, get, set_at, split


def test_encode_joins_and_escapes_tokens() -> None:
    assert encode("scene_contracts", 2, "owner_layer") == "/scene_contracts/2/owner_layer"
    assert encode("a/b", "c~d") == "/a~1b/c~0d"
    assert encode() == ""


def test_split_unescapes() -> None:
    assert split("/a~1b/c~0d") == ["a/b", "c~d"]
    assert split("") == []
    with pytest.raises(ValueError, match="must start with '/'"):
        split("scene_contracts/0")


def test_get_and_set_walk_objects_and_lists() -> None:
    document = {
        "scene_contracts": [{"id": "vis-f239", "owner_layer": "9"}],
        "layer": {"stages": [{"id": "polish"}]},
    }
    assert get(document, "/scene_contracts/0/owner_layer") == "9"
    set_at(document, "/scene_contracts/0/owner_layer", "1")
    assert get(document, "/scene_contracts/0/owner_layer") == "1"
    set_at(document, "/layer/stages/0/look_capabilities", [])
    assert get(document, "/layer/stages/0/look_capabilities") == []


def test_set_rejects_root_replace_and_list_holes() -> None:
    document = {"scene_contracts": []}
    with pytest.raises(ValueError, match="cannot replace the document root"):
        set_at(document, "", {"schema": "nope"})
    with pytest.raises(ValueError, match="out of range"):
        set_at(document, "/scene_contracts/3", {})


def test_format_finding_prefixes_the_pointer() -> None:
    assert (
        format_finding("/scene_contracts/2/owner_layer", 'owner_layer must be "1"')
        == '/scene_contracts/2/owner_layer: owner_layer must be "1"'
    )
