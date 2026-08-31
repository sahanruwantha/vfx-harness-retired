"""Strict semantic selected-authority state contracts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from vfx_harness.domain.stop_transaction_state import (
    SelectedAuthorityAssertionV2,
    SelectedAuthorityBundle,
    SelectedAuthorityView,
    state_assertion_from_dict,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _bundle(
    *,
    digest: str | None = None,
    outcome: str = "clean",
    semantic_manifest_digest: str | None = None,
) -> SelectedAuthorityBundle:
    return SelectedAuthorityBundle(
        digest=digest or _digest("bundle-content"),
        outcome=outcome,
        semantic_manifest_digest=(
            semantic_manifest_digest or _digest("bundle-semantic-manifest")
        ),
    )


def _bundle_backed() -> SelectedAuthorityAssertionV2:
    bundle = _bundle()
    return SelectedAuthorityAssertionV2(
        selection="selected",
        bundle=bundle,
        effective_view=SelectedAuthorityView(
            source="bundle",
            digest=bundle.digest,
            semantic_manifest_digest=_digest("bundle-consumer-view"),
        ),
    )


def test_absent_authority_round_trips_through_the_closed_assertion_parser() -> None:
    assertion = SelectedAuthorityAssertionV2("absent", None, None)

    assert assertion.as_dict() == {
        "schema": "vfx-harness.selected-authority-state/v2",
        "selection": "absent",
        "bundle": None,
        "effective_view": None,
        "assertion_digest": assertion.digest,
    }
    assert SelectedAuthorityAssertionV2.from_dict(assertion.as_dict(), "authority") == assertion
    assert state_assertion_from_dict(assertion.as_dict(), "authority") == assertion


@pytest.mark.parametrize(
    ("selection", "bundle", "view", "message"),
    (
        ("absent", _bundle(), None, "null bundle"),
        (
            "absent",
            None,
            SelectedAuthorityView("jit", _digest("view"), _digest("view-manifest")),
            "null bundle",
        ),
        ("selected", None, None, "requires typed bundle"),
        ("selected", _bundle(), None, "requires typed bundle"),
    ),
)
def test_selection_invariants_fail_closed(
    selection: str,
    bundle: SelectedAuthorityBundle | None,
    view: SelectedAuthorityView | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        SelectedAuthorityAssertionV2(selection, bundle, view)

    with pytest.raises(ValueError, match="must be 'absent' or 'selected'"):
        SelectedAuthorityAssertionV2("unknown", None, None)


def test_bundle_backed_view_must_share_the_bundle_content_digest() -> None:
    bundle = _bundle()
    assertion = _bundle_backed()

    assert SelectedAuthorityAssertionV2.from_dict(assertion.as_dict(), "authority") == assertion
    assert assertion.effective_view is not None
    assert assertion.effective_view.digest == bundle.digest

    with pytest.raises(ValueError, match="must equal the selected bundle digest"):
        SelectedAuthorityAssertionV2(
            "selected",
            bundle,
            SelectedAuthorityView(
                "bundle",
                _digest("different-view"),
                _digest("different-view-manifest"),
            ),
        )


def test_jit_effective_view_has_its_own_verified_identity() -> None:
    assertion = SelectedAuthorityAssertionV2(
        "selected",
        _bundle(outcome="clean_with_deferred"),
        SelectedAuthorityView(
            "jit",
            _digest("jit-view"),
            _digest("jit-view-semantic-manifest"),
        ),
    )

    parsed = state_assertion_from_dict(assertion.as_dict(), "authority")
    assert parsed == assertion
    assert isinstance(parsed, SelectedAuthorityAssertionV2)
    assert parsed.effective_view is not None
    assert parsed.effective_view.source == "jit"


@pytest.mark.parametrize(
    ("path", "operation"),
    (
        ((), lambda row: row.update({"run_id": "run-1"})),
        (("bundle",), lambda row: row.update({"published_at": "later"})),
        (("effective_view",), lambda row: row.pop("semantic_manifest_digest")),
    ),
)
def test_unknown_or_missing_fields_fail_closed(
    path: tuple[str, ...],
    operation: object,
) -> None:
    row = deepcopy(_bundle_backed().as_dict())
    target = row
    for key in path:
        target = target[key]
    assert isinstance(target, dict)
    operation(target)  # type: ignore[operator]

    with pytest.raises(ValueError, match="fields mismatch"):
        SelectedAuthorityAssertionV2.from_dict(row, "authority")


def test_wrong_schema_stale_digest_and_malformed_components_fail_closed() -> None:
    row = _bundle_backed().as_dict()

    wrong_schema = {**row, "schema": "vfx-harness.selected-authority-state/v1"}
    with pytest.raises(ValueError, match="schema"):
        state_assertion_from_dict(wrong_schema, "authority")

    stale = {**row, "assertion_digest": _digest("caller-supplied")}
    with pytest.raises(ValueError, match="stale"):
        SelectedAuthorityAssertionV2.from_dict(stale, "authority")

    malformed = {**row, "bundle": []}
    with pytest.raises(ValueError, match="must be an object"):
        SelectedAuthorityAssertionV2.from_dict(malformed, "authority")


def test_relocation_and_audit_metadata_are_unrepresentable() -> None:
    assertion = _bundle_backed()
    row = assertion.as_dict()

    assert set(row) == {
        "schema",
        "selection",
        "bundle",
        "effective_view",
        "assertion_digest",
    }
    assert set(row["bundle"]) == {"digest", "outcome", "semantic_manifest_digest"}
    assert set(row["effective_view"]) == {
        "source",
        "digest",
        "semantic_manifest_digest",
    }
    with pytest.raises(TypeError):
        SelectedAuthorityBundle(
            digest=_digest("bundle"),
            outcome="clean",
            semantic_manifest_digest=_digest("manifest"),
            run_id="run-1",  # type: ignore[call-arg]
        )


def test_bundle_content_outcome_and_semantic_manifest_change_identity() -> None:
    base = _bundle_backed()
    assert base.bundle is not None
    assert base.effective_view is not None

    changed_outcome = replace(
        base,
        bundle=replace(base.bundle, outcome="clean_with_assumptions"),
    )
    changed_manifest = replace(
        base,
        bundle=replace(
            base.bundle,
            semantic_manifest_digest=_digest("changed-semantic-manifest"),
        ),
    )
    changed_bundle_digest = _digest("changed-bundle-content")
    changed_content = replace(
        base,
        bundle=replace(base.bundle, digest=changed_bundle_digest),
        effective_view=replace(base.effective_view, digest=changed_bundle_digest),
    )

    assert len({base.digest, changed_outcome.digest, changed_manifest.digest, changed_content.digest}) == 4


def test_nested_authority_records_are_frozen_and_use_closed_vocabularies() -> None:
    bundle = _bundle()
    view = SelectedAuthorityView("jit", _digest("view"), _digest("view-manifest"))
    with pytest.raises(FrozenInstanceError):
        bundle.outcome = "clean_with_deferred"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        view.source = "bundle"  # type: ignore[misc]
    with pytest.raises(ValueError, match="outcome must be one of"):
        _bundle(outcome="dirty")
    with pytest.raises(ValueError, match="source must be one of"):
        SelectedAuthorityView("materialized", _digest("view"), _digest("manifest"))
