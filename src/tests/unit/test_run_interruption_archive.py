"""The run-owned archive manifest covers exactly the observed sources (HIR-0172)."""

from __future__ import annotations

import copy
import hashlib

import pytest

from tests.unit.test_run_authority_source_closure import _valid_closure
from vfx_harness.domain.run_interruption_archive import (
    ArchivedSourceObject,
    InterruptionArchiveManifest,
    archive_object_locator,
    iter_authority_sources,
)
from vfx_harness.domain.run_interruption_records import derive_transcript_frontier

_RUN = "run-001"
_T1 = "2026-09-01T00:01:00+00:00"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _objects(closure) -> list[ArchivedSourceObject]:
    return [
        ArchivedSourceObject("shot", source.locator, source.byte_count, source.sha256)
        for source in iter_authority_sources(closure)
    ]


def _manifest(closure, objects=None) -> InterruptionArchiveManifest:
    return InterruptionArchiveManifest.mint(
        run_id=_RUN,
        captured_at=_T1,
        authority_digest=closure.digest,
        objects=_objects(closure) if objects is None else objects,
    )


def test_manifest_covers_every_closure_source_and_round_trips() -> None:
    closure = _valid_closure()
    manifest = _manifest(closure)
    manifest.require_covers_closure(closure)
    assert [row.key for row in manifest.objects] == sorted(row.key for row in manifest.objects)
    assert InterruptionArchiveManifest.from_dict(manifest.as_dict()) == manifest
    for row in manifest.objects:
        assert row.archive_locator == archive_object_locator(row.sha256)
        assert ArchivedSourceObject.from_dict(row.as_dict()) == row


def test_manifest_refuses_missing_or_mismatched_coverage() -> None:
    closure = _valid_closure()
    objects = _objects(closure)
    missing = _manifest(closure, objects[1:])
    with pytest.raises(ValueError, match="has no object"):
        missing.require_covers_closure(closure)
    altered = list(objects)
    first = altered[0]
    altered[0] = ArchivedSourceObject(first.namespace, first.locator, first.byte_count + 1, first.sha256)
    with pytest.raises(ValueError, match="does not match the observed bytes"):
        _manifest(closure, altered).require_covers_closure(closure)


def test_mint_deduplicates_exact_rows_and_refuses_conflicts() -> None:
    closure = _valid_closure()
    objects = _objects(closure)
    manifest = _manifest(closure, [*objects, objects[0]])
    assert manifest == _manifest(closure)
    conflicting = ArchivedSourceObject(objects[0].namespace, objects[0].locator, 7, _digest("other bytes"))
    with pytest.raises(ValueError, match="two different byte streams"):
        _manifest(closure, [*objects, conflicting])


def test_wire_tampering_and_namespace_rules_fail_closed() -> None:
    closure = _valid_closure()
    manifest = _manifest(closure)
    stale = copy.deepcopy(manifest.as_dict())
    stale["manifest_digest"] = _digest("stale")
    with pytest.raises(ValueError, match="manifest_digest is stale"):
        InterruptionArchiveManifest.from_dict(stale)
    relocated = copy.deepcopy(manifest.as_dict())
    relocated["objects"][0]["archive_locator"] = "archive/interruption/objects/elsewhere"
    with pytest.raises(ValueError, match="content-addressed"):
        InterruptionArchiveManifest.from_dict(relocated)
    with pytest.raises(ValueError, match="namespace"):
        ArchivedSourceObject("elsewhere", "shot.json", 1, _digest("x"))
    unsorted = tuple(reversed(manifest.objects))
    if len(unsorted) > 1:
        with pytest.raises(ValueError, match="sorted"):
            InterruptionArchiveManifest(_RUN, _T1, closure.digest, unsorted)


def _transcript(*events: tuple[int, str], tail: bytes = b"") -> bytes:
    lines = [f'{{"seq": {seq}, "dt": 0.1, "kind": "{kind}"}}\n'.encode() for seq, kind in events]
    return b"".join(lines) + tail


def test_transcript_frontier_is_a_pure_function_of_bytes() -> None:
    closed = derive_transcript_frontier(
        run_id=_RUN,
        locator="logs/transcripts/plan/session.jsonl",
        payload=_transcript((1, "open"), (2, "prompt"), (3, "close")),
        captured_at=_T1,
    )
    assert (closed.state, closed.last_complete_sequence, closed.last_complete_kind) == ("closed", 3, "close")
    assert closed.truncated_tail is False

    truncated = derive_transcript_frontier(
        run_id=_RUN,
        locator="logs/transcripts/plan/session.jsonl",
        payload=_transcript((1, "open"), (2, "prompt"), tail=b'{"seq": 3, "kind": "mess'),
        captured_at=_T1,
    )
    assert (truncated.state, truncated.last_complete_sequence, truncated.truncated_tail) == ("incomplete", 2, True)

    empty = derive_transcript_frontier(
        run_id=_RUN,
        locator="logs/transcripts/plan/session.jsonl",
        payload=b"",
        captured_at=_T1,
    )
    assert (empty.byte_count, empty.state, empty.last_complete_sequence) == (0, "incomplete", None)

    with pytest.raises(ValueError, match="close"):
        derive_transcript_frontier(
            run_id=_RUN,
            locator="logs/transcripts/plan/session.jsonl",
            payload=_transcript((1, "open"), (2, "close"), tail=b"trailing"),
            captured_at=_T1,
        )
