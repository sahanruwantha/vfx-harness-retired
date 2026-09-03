"""Interruption capture binds authored intent and selected witnesses (HIR-0172 step 4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.integration.test_judgment_debt_public_pipeline import _public_fixture_root
from vfx_harness.domain.construction import selected_witness_tokens
from vfx_harness.domain.refobs import REFOBS_SCHEMA
from vfx_harness.domain.run_authority_source_closure import (
    AuthoredInputsSourceClosure,
    InterruptionAuthoritySourceClosure,
    RefobsWitnessSource,
)
from vfx_harness.domain.run_authority_source_identity import AuthoritySourceIdentity
from vfx_harness.domain.run_authority_source_records import classify_authority_source
from vfx_harness.domain.run_interruption_archive import iter_authority_sources
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration import run_interruption_capture as capture
from vfx_harness.orchestration.plan_authority import publish_current
from vfx_harness.orchestration.shot_authority_capture import shot_authority_writer_fence


def _shot(tmp_path: Path) -> Path:
    root = _public_fixture_root(tmp_path)
    publish_current(root, run_artifacts.create(root, "authored-inputs-fixture"), outcome="clean_with_deferred")
    return root


def _capture(root: Path, run_id: str = "authored-inputs-run"):
    run_root = run_artifacts.create(root, run_id).root
    with shot_authority_writer_fence(root) as capability:
        return capture.capture_run_authority(
            root,
            run_root,
            run_id=run_id,
            writer_capability=capability,
            captured_at="2026-09-03T00:00:00+00:00",
        )


def _opaque(kind: str, locator: str, payload: bytes) -> AuthoritySourceIdentity:
    return classify_authority_source(kind, locator, payload)


def _registration(token: str, crop: bytes) -> bytes:
    record = {
        "schema": REFOBS_SCHEMA,
        "id": token,
        "source": "refs/reference.png",
        "box": [0.1, 0.1, 0.4, 0.4],
        "sha256": hashlib.sha256(crop).hexdigest(),
        "width": 8,
        "height": 8,
    }
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")


def test_selected_witness_tokens_are_sorted_unique_and_typed() -> None:
    document = {
        "layers": [
            {"stages": [{"construction": {"route": "generate", "witnesses": ["refobs-b", "refobs-a"]}}]},
            {"stages": [{"construction": {"route": "procedural"}}, {"construction": {"witnesses": ["refobs-a"]}}]},
        ]
    }
    assert selected_witness_tokens(document) == ("refobs-a", "refobs-b")
    with pytest.raises(ValueError, match="non-refobs construction witness"):
        selected_witness_tokens({"layers": [{"stages": [{"construction": {"witnesses": ["frame.png"]}}]}]})


def test_capture_binds_brief_and_admissible_reference_stills(tmp_path: Path) -> None:
    root = _shot(tmp_path)
    (root / "refs" / "notes.txt").write_text("not a still\n", encoding="utf-8")
    (root / "refs" / "extra.JPG").write_bytes(b"jpeg-bytes")

    captured = _capture(root)
    authored = captured.snapshot.source_closure.authored_inputs

    assert authored.state == "present_valid"
    assert authored.brief is not None and authored.brief.locator == "brief.md"
    assert authored.brief.sha256 == hashlib.sha256((root / "brief.md").read_bytes()).hexdigest()
    assert [row.locator for row in authored.references] == ["refs/extra.JPG", "refs/reference.png"]
    assert authored.refobs == ()
    archived = {item.locator for item in captured.objects}
    assert {"brief.md", "refs/reference.png", "refs/extra.JPG"} <= archived
    assert "refs/notes.txt" not in archived
    assert {row.locator for row in authored.sources()} <= {
        row.locator for row in iter_authority_sources(captured.snapshot.source_closure)
    }


def test_capture_refuses_a_symlinked_reference(tmp_path: Path) -> None:
    root = _shot(tmp_path)
    (root / "refs" / "alias.png").symlink_to(root / "refs" / "reference.png")

    with pytest.raises(capture.RunInterruptionCaptureError, match="symlink"):
        _capture(root)


def test_capture_binds_the_registry_pair_of_every_selected_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _shot(tmp_path)
    registry = root / "state" / "refobs"
    registry.mkdir(parents=True, exist_ok=True)
    crop = b"\x89PNG-crop-bytes"
    (registry / "refobs-hero.png").write_bytes(crop)
    (registry / "refobs-hero.json").write_bytes(_registration("refobs-hero", crop))
    (registry / "refobs-unselected.png").write_bytes(crop)
    (registry / "refobs-unselected.json").write_bytes(_registration("refobs-unselected", crop))
    monkeypatch.setattr(
        capture._Capturer, "_selected_witness_tokens", lambda self, _plan: ("refobs-hero",)
    )

    captured = _capture(root)
    authored = captured.snapshot.source_closure.authored_inputs

    assert [row.token for row in authored.refobs] == ["refobs-hero"]
    witness = authored.refobs[0]
    assert witness.registration.source_state == "record_valid"
    assert witness.registration.record_schema == REFOBS_SCHEMA
    assert witness.crop.source_state == "opaque_valid"
    assert witness.crop.sha256 == hashlib.sha256(crop).hexdigest()
    archived = {item.locator for item in captured.objects}
    assert "state/refobs/refobs-hero.json" in archived and "state/refobs/refobs-hero.png" in archived
    assert "state/refobs/refobs-unselected.json" not in archived

    (registry / "refobs-hero.png").unlink()
    with pytest.raises(capture.RunInterruptionCaptureError, match="no complete registry pair"):
        _capture(root, run_id="authored-inputs-run-2")


def test_authored_inputs_closure_round_trips_and_pins_locators() -> None:
    brief = _opaque("brief", "brief.md", b"---\nid: x\n---\n")
    still = _opaque("reference_still", "refs/frame.png", b"png")
    crop = b"crop"
    witness = RefobsWitnessSource(
        "refobs-a",
        classify_authority_source("refobs_registration", "state/refobs/refobs-a.json", _registration("refobs-a", crop)),
        _opaque("refobs_crop", "state/refobs/refobs-a.png", crop),
    )
    closure = AuthoredInputsSourceClosure.mint(brief=brief, references=[still], refobs=[witness])
    assert closure.state == "present_valid"
    assert AuthoredInputsSourceClosure.from_dict(closure.as_dict()) == closure
    assert AuthoredInputsSourceClosure.absent().state == "absent"
    assert AuthoredInputsSourceClosure.absent().sources() == ()

    with pytest.raises(ValueError, match=r"direct refs/ members"):
        AuthoredInputsSourceClosure.mint(
            brief=None, references=[_opaque("reference_still", "refs/sub/frame.png", b"x")], refobs=[]
        )
    with pytest.raises(ValueError, match=r"shot-root brief\.md"):
        AuthoredInputsSourceClosure.mint(brief=_opaque("brief", "notes/brief.md", b"x"), references=[], refobs=[])
    with pytest.raises(ValueError, match="registry pair"):
        RefobsWitnessSource(
            "refobs-a",
            witness.registration,
            _opaque("refobs_crop", "state/refobs/refobs-b.png", crop),
        )
    stale = closure.as_dict()
    stale["closure_digest"] = "0" * 64
    with pytest.raises(ValueError, match="stale"):
        AuthoredInputsSourceClosure.from_dict(stale)
    assert InterruptionAuthoritySourceClosure.SCHEMA.endswith("/v2")
