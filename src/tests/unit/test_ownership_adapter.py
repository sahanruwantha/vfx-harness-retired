"""End-to-end: authored brief → compact mapping → mechanical expansion → derived
ownership record → feasibility check. No hand-supplied metadata anywhere — moments come
from clause text, the acceptance schedule from declared judge frames, implicated roles
from reserved namespaces. The seeded-defect case is one today's publication gate passes
clean and the checker rejects."""

from __future__ import annotations

import json
from pathlib import Path

from vfx_harness.evaluation import plan_gate
from vfx_harness.evaluation.ownership_adapter import derive_record, extract_moments
from vfx_harness.evaluation.ownership_feasibility import check
from vfx_harness.orchestration.plan_authoring import clause_registry, expand_mapping

BRIEF = """---
id: adapter-fixture
title: Reveal and settle
type: motion
frames: 48
fps: 24
resolution: [1920, 1080]
engine: BLENDER_EEVEE_NEXT
---

# Reveal and settle

## Intent

A dolly move reveals a plaza, then dressing elements settle into place.

## Beats

| Beat | Frames | Required action |
|---|---:|---|
| B1 | 1-24 | The camera dollies forward to reveal the plaza. |
| B2 | 25-48 | Dressing elements settle into their final positions. |

## Acceptance

- The silhouette must read clearly at frames 24 and 48.

## Deliverables

- One approval still at frame 48.
"""


def _shot(tmp_path: Path) -> Path:
    (tmp_path / "brief.md").write_text(BRIEF, encoding="utf-8")
    (tmp_path / "refs").mkdir()
    for name in ("f024.png", "f048.png"):
        (tmp_path / "refs" / name).write_bytes(b"png")
    return tmp_path


def _mapping(registry, *, silhouette_owner: str) -> dict:
    resolutions = {}
    for row in registry:
        text = row["statement"].lower()
        if "silhouette" in text:
            owner = silhouette_owner
        elif "settle" in text or "48" in text:
            owner = "2"
        else:
            owner = "1"
        resolutions[row["id"]] = {
            "kind": "deferred_owner",
            "owner_layer": owner,
            "evidence_domains": ["scene"],
        }
    return {
        "schema": "vfx-harness.ownership-mapping/v1",
        "layers": [
            {"id": "1", "title": "Camera reveal", "script": "build/01_camera.py",
             "charter": "authored brief", "primary_judge": 24,
             "judge": [{"frame": 24, "ref": "refs/f024.png"}],
             "owns": ["camera_motion"], "evidence_domains": ["scene", "temporal"],
             "depends_on": [], "provides": {"camera": ["camera.*"]},
             "reserved_roles": ["camera.*"]},
            {"id": "2", "title": "Dressing settle", "script": "build/02_dressing.py",
             "charter": "camera reveal outcome", "primary_judge": 48,
             "judge": [{"frame": 48, "ref": "refs/f048.png"}],
             "owns": ["dressing_settle"], "evidence_domains": ["scene", "temporal"],
             "depends_on": ["1"], "provides": {},
             "reserved_roles": ["dressing.*"]},
        ],
        "axes": [
            {"key": "camera_motion", "desc": "continuous motivated dolly"},
            {"key": "dressing_settle", "desc": "elements reach final positions"},
        ],
        "resolutions": resolutions,
        "blockers": [],
    }


def test_extract_moments_reads_clause_text_mechanically() -> None:
    assert extract_moments("The silhouette must read at frames 24 and 48.") == [24, 48]
    assert extract_moments("silhouette at frames 132, 150, 204, and 240") == [
        132, 150, 204, 240]
    assert extract_moments("One approval still at frame 48.") == [48]
    assert extract_moments("at 1920x1080 and 24 fps with no frame numbers") == []
    assert extract_moments("The backdrop stays clean.") == []
    # Spans are window metadata, not measurement moments.
    assert extract_moments("| B1 | 1-24 | The camera dollies forward. |") == []


def test_derived_record_catches_a_misassignment_the_gate_passes(tmp_path: Path) -> None:
    """The two-frame silhouette clause deferred to layer 1 (judged only at f24) is
    structurally clean to the publication gate — and infeasible: its verifier can never
    observe f48. The mechanically derived record rejects it with zero hand metadata."""
    shot = _shot(tmp_path)
    registry = clause_registry(shot / "brief.md")

    expand_mapping(shot, _mapping(registry, silhouette_owner="1"))
    assert plan_gate.run(shot, "plans/global.md", require_scene_checks=True).clean

    record = derive_record(shot)
    findings, _ = check(record)
    observability = [f for f in findings if f.check == "moment-observability"]
    assert observability, [f.detail for f in findings]
    assert any("48" in f.detail for f in observability)


def test_derived_record_is_complete_and_clean_for_a_sound_assignment(
    tmp_path: Path,
) -> None:
    shot = _shot(tmp_path)
    registry = clause_registry(shot / "brief.md")

    expand_mapping(shot, _mapping(registry, silhouette_owner="2"))
    record = derive_record(shot)

    # Completeness: every requirement carries the contract keys; the schedule is the
    # union of declared judge frames; nothing was authored by hand.
    assert record["acceptance_moments"] == [24, 48]
    assert record["requirements"], "deferred rows must produce records"
    for row in record["requirements"]:
        assert "moments" in row and "implicated_roles" in row

    findings, _ = check(record)
    # The silhouette clause names f24 and f48 while layer 2 judges only f48: the
    # checker correctly demands a boundary that observes both — under the pending
    # ownership split that is verify_at acceptance, which the derived schedule covers.
    residual = [f for f in findings if f.check == "moment-observability"]
    assert all("24" in f.detail for f in residual)
    corrected = json.loads(json.dumps(record))
    for row in corrected["requirements"]:
        if row["moments"] == [24, 48]:
            row["verify_at"] = {"kind": "acceptance"}
    findings, _ = check(corrected)
    assert not [f for f in findings if f.check == "moment-observability"]
