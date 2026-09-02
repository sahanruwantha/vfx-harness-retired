"""Installed-view file replacement proves the published inode, not a rename-sensitive stat."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.plan_consumer_view_fixtures import registered_consumer_view
from vfx_harness.orchestration import plan_consumer_installed_projection as projection
from vfx_harness.orchestration.plan_consumer_view_mutation import (
    PlanConsumerViewMutationConflict,
    mutating_plan_consumer_view,
)


def test_replace_regular_file_creates_then_replaces_through_the_publishing_rename(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)

    with mutating_plan_consumer_view(shot, view, marker) as capability:
        first = projection.replace_regular_file(capability, "member.json", b"{}\n")
        assert (view / "member.json").read_bytes() == b"{}\n"
        second = projection.replace_regular_file(
            capability,
            "member.json",
            b'{"generation": 2}\n',
        )
        assert projection.read_regular_file(capability, "member.json") == (
            b'{"generation": 2}\n'
        )

    assert first != second
    assert (view / "member.json").read_bytes() == b'{"generation": 2}\n'
    assert not list(view.glob(".plan-consumer-file.tmp-*"))


def test_symlinked_member_is_refused_until_its_verified_link_is_removed(
    tmp_path: Path,
) -> None:
    shot, view, marker = registered_consumer_view(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside\n")
    (view / "linked.json").symlink_to(outside)

    with mutating_plan_consumer_view(shot, view, marker) as capability:
        assert projection.member_kind(capability, "linked.json") == "symlink"
        with pytest.raises(
            PlanConsumerViewMutationConflict,
            match="absent or a real regular file",
        ):
            projection.replace_regular_file(capability, "linked.json", b"replaced\n")
        assert outside.read_bytes() == b"outside\n"
        projection.remove_symlink(capability, "linked.json")
        projection.replace_regular_file(capability, "linked.json", b"replaced\n")

    assert outside.read_bytes() == b"outside\n"
    assert not (view / "linked.json").is_symlink()
    assert (view / "linked.json").read_bytes() == b"replaced\n"
