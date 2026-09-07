"""Composed groups share a picture only when everything that made it is the same.

hansa_silk_road produced three byte-identical Workbench-solid captures in one layer
finalization: one group per judgment debt, each replaying the same scripts and rendering
the same frame in the same mode. The plates were the waste; the judgments were not
duplicates and must stay separate (HIR-0249).

The two directions are tested with equal weight. A missed reuse costs a render. A false
reuse hands one group another group's picture, which is a wrong verdict on real evidence,
so every component of the key gets a test that changing it alone forces a fresh render.
"""

from __future__ import annotations

import hashlib

import pytest

from vfx_harness.agents.builder.capture_cache import CaptureCache
from vfx_harness.domain.capture_equivalence import capture_equivalence_key
from vfx_harness.orchestration.unit_replay_inputs import (
    ExecutedReplayDependency,
    ExecutedReplayInput,
)

# The production types, never a double. The first version of these tests defined a
# `_Dependency` with `script_path`/`script_sha256` -- the *input* type's field names -- and
# passed, while the code under test read those same wrong names off a real
# `ExecutedReplayDependency` (which carries kind/path/sha256) and got empty strings. The
# double was shaped to the assumption rather than to the type, so the test confirmed the
# belief instead of the behaviour, and a changed asset digest keyed identically.


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _dependency(sha: str) -> ExecutedReplayDependency:
    return ExecutedReplayDependency(
        kind="asset",
        path="build/construction/tower.glb",
        sha256=sha,
        source_binding=None,
    )


def _inputs(
    *, body: str = "layer-2-body", asset: str = "asset-v1"
) -> tuple[ExecutedReplayInput, ...]:
    return (
        ExecutedReplayInput("build/01_camera.py", _digest("camera"), None, ()),
        ExecutedReplayInput(
            "build/02_tower.py",
            _digest(body),
            None,
            (_dependency(_digest(asset)),),
        ),
    )


def _key(**overrides):
    kwargs = {"replay_inputs": _inputs(), "frame": 51, "mode": "solid", "scale": 0.5}
    kwargs.update(overrides)
    return capture_equivalence_key(**kwargs)


def _cache_with_plate(tmp_path, *, frame=51, mode="solid", scale=0.5):
    renders = tmp_path / "runs" / "r1" / "evidence" / "renders"
    renders.mkdir(parents=True)
    plate = renders / "L2@g0_finalization_group_0_canonical_f51.png"
    plate.write_bytes(b"the-rendered-plate")
    receipt = {
        "frame": frame,
        "mode": mode,
        "scale": scale,
        "png_sha256": hashlib.sha256(b"the-rendered-plate").hexdigest(),
    }
    cache = CaptureCache(tmp_path)
    cache.record(_key(), plate.relative_to(tmp_path).as_posix(), receipt)
    return cache, plate


def test_a_second_group_on_the_same_scene_reuses_the_plate_under_its_own_name(tmp_path):
    cache, plate = _cache_with_plate(tmp_path)

    reused = cache.reuse(
        _key(),
        destination_tag="finalization_group_1_canonical_f51",
        milestone_id="L2@g1",
        frame=51,
        mode="solid",
        scale=0.5,
    )

    assert reused is not None
    render_rel, receipt = reused
    # Its own locator, so a reader still finds the file the group's receipt names.
    assert render_rel.endswith("L2@g1_finalization_group_1_canonical_f51.png")
    assert (tmp_path / render_rel).read_bytes() == plate.read_bytes()
    assert receipt["png_sha256"] == hashlib.sha256(b"the-rendered-plate").hexdigest()
    assert cache.hits == 1


@pytest.mark.parametrize(
    "label,overrides",
    [
        ("a different judge frame", {"frame": 151}),
        ("a different render mode", {"mode": "eevee"}),
        ("a different render scale", {"scale": 1.0}),
        ("a changed script body", {"replay_inputs": _inputs(body="layer-2-repaired")}),
    ],
)
def test_any_input_that_moves_a_pixel_forces_a_fresh_render(tmp_path, label, overrides):
    cache, _plate = _cache_with_plate(tmp_path)

    assert cache.reuse(
        _key(**overrides),
        destination_tag="finalization_group_1_canonical_f51",
        milestone_id="L2@g1",
        frame=int(overrides.get("frame", 51)),
        mode=str(overrides.get("mode", "solid")),
        scale=float(overrides.get("scale", 0.5)),
    ) is None, f"{label} must not reuse a plate"
    assert cache.hits == 0
    # Positive control on the same cache. Without it this test passes identically when
    # reuse is broken or absent, which makes it a check that could not have failed.
    assert cache.reuse(
        _key(),
        destination_tag="control",
        milestone_id="L2@g1",
        frame=51,
        mode="solid",
        scale=0.5,
    ) is not None, "the cache must be live, or the refusal above proves nothing"


def test_a_changed_asset_digest_alone_forces_a_fresh_render(tmp_path):
    """A script's pixels depend on the assets it imports, not only on its own bytes.

    This is the case that silently passed: a promoted `build/construction/<sha>.glb` swapped
    for different geometry, with every script byte identical.
    """
    cache, _plate = _cache_with_plate(tmp_path)
    moved = _inputs(asset="asset-v2")

    assert cache.reuse(
        _key(replay_inputs=moved),
        destination_tag="t",
        milestone_id="L2@g1",
        frame=51,
        mode="solid",
        scale=0.5,
    ) is None
    assert cache.reuse(
        _key(), destination_tag="control", milestone_id="L2@g1", frame=51, mode="solid", scale=0.5
    ) is not None, "the cache must be live, or the refusal above proves nothing"


def test_a_hit_is_verified_against_the_bytes_not_trusted_from_the_lookup(tmp_path):
    """A dictionary lookup is not evidence about a file."""
    cache, plate = _cache_with_plate(tmp_path)
    plate.write_bytes(b"something-else-entirely")

    assert cache.reuse(
        _key(),
        destination_tag="t",
        milestone_id="L2@g1",
        frame=51,
        mode="solid",
        scale=0.5,
    ) is None
    # The stale entry is dropped, so the next group renders rather than retrying it.
    assert cache._entries == {}


def test_a_vanished_plate_is_a_miss_not_a_crash(tmp_path):
    cache, plate = _cache_with_plate(tmp_path)
    plate.unlink()

    assert cache.reuse(
        _key(), destination_tag="t", milestone_id="L2@g1", frame=51, mode="solid", scale=0.5
    ) is None


def test_the_key_holds_pixels_and_refuses_to_guess(tmp_path):
    """An unusable key is a miss, never a finalization failure: this is an optimisation."""
    cache = CaptureCache(tmp_path)

    assert cache.key(replay_inputs=(), frame=1, mode="solid", scale=0.5) is None
    # A script with no digest cannot prove the scene is unchanged.
    blank_script = (ExecutedReplayInput("build/01.py", "", None, ()),)
    assert cache.key(replay_inputs=blank_script, frame=1, mode="solid", scale=0.5) is None
    # A dependency read as blank is indistinguishable from one that did not change, which
    # is the shape that made a changed asset digest key identically.
    blank_dependency = (
        ExecutedReplayInput("build/01.py", _digest("s"), None, (_dependency(""),)),
    )
    assert cache.key(
        replay_inputs=blank_dependency, frame=1, mode="solid", scale=0.5
    ) is None
    # A type this does not recognise raises rather than keying on what it managed to read,
    # and the cache turns that into a miss.
    assert cache.key(
        replay_inputs=(object(),), frame=1, mode="solid", scale=0.5
    ) is None
    with pytest.raises(ValueError, match="cannot identify the scene"):
        capture_equivalence_key(replay_inputs=(), frame=1, mode="solid", scale=0.5)
    with pytest.raises(ValueError, match="dependency read as blank"):
        capture_equivalence_key(
            replay_inputs=blank_dependency, frame=1, mode="solid", scale=0.5
        )
    with pytest.raises(AttributeError):
        capture_equivalence_key(
            replay_inputs=(object(),), frame=1, mode="solid", scale=0.5
        )


def test_judgment_is_not_part_of_the_key():
    """Two groups paying different debts look at one picture and owe two answers.

    If the debt entered the key, every group would miss and the duplication this exists to
    remove would come back silently.
    """
    assert _key() == _key()
    # The key is computed from replay/frame/mode/scale alone -- there is no debt, claim,
    # judge-point or verdict parameter to pass, and that absence is the property.
    assert set(capture_equivalence_key.__kwdefaults__ or {}) == set()


def test_groups_whose_units_differ_only_in_judgment_share_one_scene():
    """hansa_silk_road's discriminating case: different mutation scopes, same picture.

    The empirical evidence from that shot is that two groups paying different debts drew
    on the same two units, so it could not distinguish "the composition unit cannot touch
    the scene" from "neither of mine did". Read from `_verify_script` instead: before the
    render, `active_unit` reaches exactly two things -- the scoped-mutation check, which
    compares object manifests and mutates nothing, and `_unit_raster_mode`, which selects
    the render mode and is itself in the key.

    So two composition units differing in mutation scope, claims or judge points key
    identically, because the scene is produced by the replay inputs and the only pixel
    effect a unit carries is its medium.
    """
    shared = _inputs()

    narrow = capture_equivalence_key(
        replay_inputs=shared, frame=51, mode="solid", scale=0.5
    )
    broad = capture_equivalence_key(
        replay_inputs=shared, frame=51, mode="solid", scale=0.5
    )

    assert narrow == broad
    # And the medium, which a unit does decide, still separates them.
    assert (
        capture_equivalence_key(replay_inputs=shared, frame=51, mode="eevee", scale=0.5)
        != narrow
    )


def test_pixel_identical_plates_do_not_share_bytes_so_output_hashing_would_never_hit():
    """The refactor this design must survive, pinned as a fact about the data.

    hansa_silk_road's attempt 10 rendered nine plates. Groups 0 and 1 were pixel-identical
    at every judge frame (diff bbox None, max delta 0) and no two of the nine shared a
    SHA-256, because PNG output carries per-write metadata. Keying on the produced file
    instead of its inputs yields a cache that never fires and looks like it works.
    """
    from vfx_harness.domain.capture_equivalence import capture_equivalence_key as key_fn

    source = key_fn.__doc__ or ""
    module = __import__(
        "vfx_harness.domain.capture_equivalence", fromlist=["capture_equivalence_key"]
    )
    assert "never fire" in (module.__doc__ or ""), (
        "the output-hash trap must stay documented where the next reader will refactor"
    )
    # The key takes inputs, never a rendered artifact: no path, no bytes, no digest of one.
    import inspect

    parameters = set(inspect.signature(key_fn).parameters)
    assert parameters == {"replay_inputs", "frame", "mode", "scale"}, parameters
    assert "png" not in source.lower()
