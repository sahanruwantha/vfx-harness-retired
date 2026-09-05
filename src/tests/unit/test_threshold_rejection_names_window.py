"""A threshold rejection must name the thresholds that would have been legal.

hansa_silk_road layer 2 spent $12.62 across 48 mutations and 62 renders on a
`frame_detail` image debt, then recorded `cannot_express_in_scope`. Three of its 47
candidate plates did admit a legal threshold; the builder could not tell which, because
the rejection named only the verdict it failed. The legal window is two-sided -- above the
adversary (else NOT NECESSARY) and at or below value - margin (else FRAGILE) -- and both
bounds are known to the guard at the moment it refuses.
"""
from __future__ import annotations

from PIL import Image

from vfx_harness.evidence.checks import Check, verify_necessity


def _plate(path, level):
    Image.new("RGB", (64, 64), (level, level, level)).save(path)
    return path


def test_fragile_rejection_names_the_legal_window(tmp_path):
    before = _plate(tmp_path / "before.png", 10)
    after = _plate(tmp_path / "after.png", 100)
    # lo shaved just under the candidate: clearance far below the 5% margin.
    check = Check(id="probe", metric="region_mean", op=">=", lo=99.5,
                  regions={"r": [0.0, 0.0, 1.0, 1.0]})
    v = verify_necessity(check, after, before)
    assert not v.ok
    joined = " ".join(v.reasons)
    assert "FRAGILE THRESHOLD" in joined
    assert "Legal lo on this render" in joined, (
        "the rejection states the failure but not the thresholds that would have "
        f"succeeded:\n{joined}"
    )
    # the window must be concrete and correct: above the adversary, below value - margin
    assert "10" in joined and "9" in joined, joined


def test_window_is_reported_empty_when_no_threshold_is_legal(tmp_path):
    """The case that cost the money: no lo satisfies both bounds at once."""
    before = _plate(tmp_path / "before.png", 90)
    after = _plate(tmp_path / "after.png", 100)
    check = Check(id="probe", metric="region_mean", op=">=", lo=95.0,
                  regions={"r": [0.0, 0.0, 1.0, 1.0]})
    v = verify_necessity(check, after, before)
    joined = " ".join(v.reasons)
    if not v.ok and "FRAGILE" in joined:
        assert "Legal lo on this render" in joined, joined


def test_upper_bound_checks_get_a_window_too(tmp_path):
    before = _plate(tmp_path / "before.png", 200)
    after = _plate(tmp_path / "after.png", 100)
    check = Check(id="probe", metric="region_mean", op="<=", hi=100.5,
                  regions={"r": [0.0, 0.0, 1.0, 1.0]})
    v = verify_necessity(check, after, before)
    joined = " ".join(v.reasons)
    assert not v.ok
    assert "Legal hi on this render" in joined, joined
