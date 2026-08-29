"""Builder image debts require immutable, run-owned adversary provenance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from vfx_harness.blender.tools import (
    _candidate_for_proposed_check,
    _payment_eligible_candidate,
)
from vfx_harness.evidence.checks import (
    IMAGE_PAYMENT_SCHEMA,
    Check,
    load_image_contract_payment_rows,
    runtime_image_payment_error,
    verify_necessity,
)


def test_only_fixed_canonical_render_is_advertised_as_payment_candidate() -> None:
    assert _payment_eligible_candidate({"mode": "eevee", "scale": 0.5})
    assert not _payment_eligible_candidate({"mode": "solid", "scale": 0.5})
    assert not _payment_eligible_candidate({"mode": "draft", "scale": 0.5})
    assert not _payment_eligible_candidate({"mode": "eevee", "scale": 0.4})


def test_each_proposed_check_can_bind_its_own_frame_local_handle() -> None:
    registry = {
        "image:f72": {"role": "live_candidate", "frame": 72},
        "image:f150": {"role": "live_candidate", "frame": 150},
    }
    handle72, record72, error72 = _candidate_for_proposed_check(
        {"after_handle": "image:f72"}, "image:f150", registry
    )
    handle150, record150, error150 = _candidate_for_proposed_check(
        {}, "image:f150", registry
    )
    assert (handle72, record72["frame"], error72) == ("image:f72", 72, "")
    assert (handle150, record150["frame"], error150) == ("image:f150", 150, "")
    assert _candidate_for_proposed_check({}, None, registry)[2].startswith(
        "after_handle is required"
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payment_fixture(root: Path) -> tuple[dict, Path]:
    run_id = "20260827T155157Z-test"
    render_root = root / "runs" / run_id / "evidence" / "renders"
    render_root.mkdir(parents=True)
    (root / "runs" / run_id / "manifest.json").write_text(
        json.dumps({"schema": "vfx-harness.run/v1", "run_id": run_id}),
        encoding="utf-8",
    )
    candidate = render_root / "unit_candidate_f0040.png"
    adversary = render_root / "unit_adversary_f0040.png"
    Image.new("RGB", (64, 36), (80, 80, 80)).save(candidate)
    Image.new("RGB", (64, 36), (20, 20, 20)).save(adversary)
    parent_hash = hashlib.sha256(b"prior-chain").hexdigest()
    settings = {"frame": 40, "mode": "eevee", "scale": 0.5, "resolution": [64, 36, 100]}
    row = {
        "id": "form-look-f40",
        "origin": "builder",
        "layer": "2",
        "axis": "form",
        "frame": 40,
        "metric": "region_mean",
        "payment": {
            "schema": IMAGE_PAYMENT_SCHEMA,
            "run_id": run_id,
            "unit_id": "form",
            "unit_hash": hashlib.sha256(b"unit").hexdigest(),
            "parent_chain_hash": parent_hash,
            "candidate": {
                **settings,
                "path": candidate.relative_to(root).as_posix(),
                "sha256": _sha(candidate),
            },
            "adversary": {
                **settings,
                "path": adversary.relative_to(root).as_posix(),
                "sha256": _sha(adversary),
                "parent_chain_hash": parent_hash,
            },
        },
    }
    return row, candidate


def test_legacy_shot_path_cannot_pay_an_image_debt(tmp_path: Path) -> None:
    legacy = tmp_path / "renders" / "old.png"
    legacy.parent.mkdir()
    Image.new("RGB", (16, 9), (80, 80, 80)).save(legacy)
    row = {
        "id": "form-look-f40",
        "origin": "builder",
        "layer": "2",
        "axis": "form",
        "frame": 40,
        "metric": "region_mean",
        "proof": {"on": "renders/old.png"},
    }
    (tmp_path / "runtime_checks.json").write_text(json.dumps([row]), encoding="utf-8")

    assert "missing payment schema" in str(runtime_image_payment_error(tmp_path, row))
    assert load_image_contract_payment_rows(tmp_path) == []


def test_hash_pinned_run_artifacts_pay_and_tampering_revokes(tmp_path: Path) -> None:
    row, candidate = _payment_fixture(tmp_path)
    (tmp_path / "runtime_checks.json").write_text(json.dumps([row]), encoding="utf-8")

    assert runtime_image_payment_error(tmp_path, row) is None
    assert [item["id"] for item in load_image_contract_payment_rows(tmp_path)] == [
        "form-look-f40"
    ]

    Image.new("RGB", (64, 36), (81, 81, 81)).save(candidate)
    assert "sha256" in str(runtime_image_payment_error(tmp_path, row))
    assert load_image_contract_payment_rows(tmp_path) == []


def test_model_selected_adversary_outside_run_is_rejected(tmp_path: Path) -> None:
    row, _candidate = _payment_fixture(tmp_path)
    legacy = tmp_path / "renders" / "guessed-before.png"
    legacy.parent.mkdir()
    Image.new("RGB", (64, 36), (20, 20, 20)).save(legacy)
    row["payment"]["adversary"]["path"] = legacy.relative_to(tmp_path).as_posix()
    row["payment"]["adversary"]["sha256"] = _sha(legacy)

    assert "not owned by run" in str(runtime_image_payment_error(tmp_path, row))


def test_one_shot_threshold_shaving_is_not_a_durable_payment(
    tmp_path: Path, monkeypatch
) -> None:
    """The motivating f150 check cleared >=48 by 1.28 then failed canonical replay."""
    after = tmp_path / "after.png"
    before = tmp_path / "before.png"
    after.write_bytes(b"after")
    before.write_bytes(b"before")
    check = Check("img-geo-f150", "region_sigma", ">=", 48, float("inf"), regions={"r": (0, 0, 1, 1)})
    monkeypatch.setattr(
        "vfx_harness.evidence.checks.evaluate",
        lambda _check, path: 49.28 if Path(path) == after else 46.78,
    )
    monkeypatch.setattr("vfx_harness.evidence.checks.noise_floor", lambda *_args, **_kw: 0.1)

    verdict = verify_necessity(check, after, before)

    assert not verdict.ok
    assert any("FRAGILE THRESHOLD" in reason for reason in verdict.reasons)
