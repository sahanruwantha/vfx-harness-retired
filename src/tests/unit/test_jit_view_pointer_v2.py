"""Strict contract tests for the selected JIT view v2 pointer."""

from __future__ import annotations

from typing import Any

import pytest

from vfx_harness.orchestration.jit_materialization.schema import (
    OVERLAY_ARTIFACTS,
    VIEW_SCHEMA,
)
from vfx_harness.orchestration.jit_materialization.view_pointer import (
    JitViewPointerError,
    parse_jit_view_pointer,
    require_live_jit_artifact_locators,
)


def _pointer() -> dict[str, Any]:
    return {
        "schema": VIEW_SCHEMA,
        "revision": 7,
        "plan_revision": 3,
        "bundle_hash": "a" * 64,
        "view_hash": "b" * 64,
        "materialized_layers": ["form", "look"],
        "artifacts": {
            name: f"state/jit-layers/views/{'b' * 64}/{name}"
            for name in OVERLAY_ARTIFACTS
        },
        "hashes": dict.fromkeys(OVERLAY_ARTIFACTS, "c" * 64),
    }


def test_v2_pointer_exposes_monotone_selection_revisions() -> None:
    pointer = _pointer()

    selected = parse_jit_view_pointer(pointer)

    assert selected.revision == 7
    assert selected.plan_revision == 3
    assert selected.bundle_hash == "a" * 64
    assert selected.view_hash == "b" * 64
    assert selected.materialized_layers == ("form", "look")
    assert selected.artifacts == pointer["artifacts"]
    assert selected.hashes == pointer["hashes"]


@pytest.mark.parametrize("field", ["revision", "plan_revision"])
def test_v2_pointer_requires_both_revision_fields(field: str) -> None:
    pointer = _pointer()
    pointer.pop(field)

    with pytest.raises(JitViewPointerError, match="v2 producer schema") as caught:
        parse_jit_view_pointer(pointer)

    assert caught.value.code == "shape"


def test_v2_pointer_rejects_unexpected_fields() -> None:
    pointer = _pointer()
    pointer["selection_epoch"] = 9

    with pytest.raises(JitViewPointerError, match="v2 producer schema") as caught:
        parse_jit_view_pointer(pointer)

    assert caught.value.code == "shape"


@pytest.mark.parametrize("field", ["revision", "plan_revision"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1", None])
def test_v2_pointer_revisions_are_positive_non_bool_integers(
    field: str,
    value: object,
) -> None:
    pointer = _pointer()
    pointer[field] = value

    with pytest.raises(JitViewPointerError, match="positive integer") as caught:
        parse_jit_view_pointer(pointer)

    assert caught.value.code == field


def test_v1_pointer_is_strictly_rejected() -> None:
    pointer = _pointer()
    pointer["schema"] = "vfx-harness.jit-layer-view/v1"

    with pytest.raises(JitViewPointerError, match="unsupported schema") as caught:
        parse_jit_view_pointer(pointer)

    assert caught.value.code == "schema"


def test_v2_pointer_retains_locator_hash_and_layer_checks() -> None:
    unsafe_locator = _pointer()
    unsafe_locator["artifacts"] = {
        **unsafe_locator["artifacts"],
        "layers.json": "../layers.json",
    }
    with pytest.raises(JitViewPointerError) as locator_error:
        parse_jit_view_pointer(unsafe_locator)
    assert locator_error.value.code == "artifact_locator"

    invalid_digest = _pointer()
    invalid_digest["hashes"] = {
        **invalid_digest["hashes"],
        "checks.json": "A" * 64,
    }
    with pytest.raises(JitViewPointerError) as digest_error:
        parse_jit_view_pointer(invalid_digest)
    assert digest_error.value.code == "artifact_digest"

    unsorted_layers = _pointer()
    unsorted_layers["materialized_layers"] = ["look", "form"]
    with pytest.raises(JitViewPointerError) as layer_error:
        parse_jit_view_pointer(unsorted_layers)
    assert layer_error.value.code == "materialized_layers"


def test_synthetic_candidate_locator_is_parseable_but_not_a_live_head() -> None:
    candidate = _pointer()
    candidate["artifacts"] = {name: name for name in OVERLAY_ARTIFACTS}

    parsed = parse_jit_view_pointer(candidate)

    with pytest.raises(JitViewPointerError, match="exact producer locator") as caught:
        require_live_jit_artifact_locators(parsed)
    assert caught.value.code == "artifact_locator"
