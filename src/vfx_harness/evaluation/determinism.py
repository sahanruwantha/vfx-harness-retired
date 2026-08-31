"""Regression checks that need no model at all.

These are the ones that will actually get run, because they cost nothing and finish in
seconds-to-minutes. Two defect classes live here, both observed:

  replay equivalence   The accepted chain is supposed to be a deterministic artifact —
                       "run these scripts from an empty scene and you get the shot back"
                       is the pipeline's whole storage model, there is no .blend of
                       record. A script that quietly depends on unseeded randomness, on
                       dict iteration order, or on state left behind by the previous run
                       breaks that contract, and nothing currently notices: the canonical
                       re-render is judged ONCE, so a script that produces a different
                       scene every time passes exactly as readily as one that doesn't.

  metric self-consistency
                       An image compared against ITSELF must score identically whatever
                       resolution it arrived at. It does not. `look_vector` normalises
                       every input to 960px wide, so a render made at the builder's
                       default `scale=0.4` is UPSCALED before measurement, and the
                       interpolated pixels destroy exactly the high-frequency signal that
                       `detail` and `points` count. The consequence is not theoretical:
                       one of today's retracted findings ("detail 46% high") was this
                       artifact, and `guardrails.metrics_feedback` pushes the same
                       artifact into the builder's context after every draft render.
"""

from __future__ import annotations

import re
import statistics
import tempfile
from pathlib import Path

from PIL import Image

from vfx_harness.domain.brief import Shot
from vfx_harness.evidence.metrics import compare, look_pair, look_vector
from vfx_harness.observability.log import log
from vfx_harness.orchestration.ledger import Ledger, load_layers

from ..agents.builder import _RESET, _preamble, _run_artifact_script
from ..blender.session import BlenderError, BlenderSession

# The scales the render tools actually offer. render_frame and compare_frame default to
# 0.4, render_frames to 0.35, _stash_render to 0.5 — so a builder's own measurements and
# the critic's stashed copy are already taken at three different resolutions.
RENDER_SCALES = (1.0, 0.5, 0.4, 0.35, 0.25)


class Result:
    """One check's outcome. `skipped` is a first-class state: a check that could not run
    must not read as a check that passed."""

    def __init__(self, name: str, *, ok: bool | None, detail: str, data: dict | None = None):
        self.name, self.ok, self.detail, self.data = name, ok, detail, data or {}

    @property
    def state(self) -> str:
        return "SKIP" if self.ok is None else ("PASS" if self.ok else "FAIL")

    def __str__(self) -> str:
        mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "–"}[self.state]
        return f"  {mark} {self.name}\n      {self.detail}" if self.detail else f"  {mark} {self.name}"

    def as_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "detail": self.detail, **self.data}


# --------------------------------------------------------------------------- #
# metric self-consistency (pure python, no Blender, no model)                  #
# --------------------------------------------------------------------------- #
def _one_image_scale_sweep(image: Path, scales, tmp: Path, delivery: tuple[int, int] | None) -> dict:
    """scale -> the deltas a PIXEL-PERFECT render at that scale would show against the
    full-resolution plate.

    The geometry has to match the pipeline's or the number is fiction. A render is
    produced at `scale × delivery resolution` and compared against a full-resolution
    reference, so the variant must be `scale × delivery`, not `scale × whatever this file
    happens to be`. Getting that wrong is not hypothetical: the first version of this
    check swept the already-downscaled 960x480 stashed renders down by a further 0.25 and
    reported "11 blocking failures", which is a scale of 0.125 the pipeline never uses.
    An eval that overstates its finding is the same defect as one that misses it.
    """
    im = Image.open(image).convert("RGB")
    base_w, base_h = delivery or im.size
    out = {}
    for s in scales:
        if s >= 1.0 and (im.width, im.height) == (base_w, base_h):
            path = image
        else:
            path = tmp / f"{image.stem}_s{s}.png"
            im.resize((max(1, round(base_w * s)), max(1, round(base_h * s))),
                      Image.LANCZOS).save(path)
        # look_PAIR, not look_vector. Measuring each image independently is what produced
        # the asymmetry in the first place: look_vector normalises everything to width
        # 960, which UPSCALES a sub-960 render while the full-res plate downscales. That
        # is defensible for one image on its own and wrong for a comparison, which is the
        # only way the pipeline uses it. look_pair measures both at a common width no
        # larger than either input, so neither is ever upscaled. This check exists to
        # police the COMPARISON path, because that is the one every verdict rides on.
        out[s] = compare(*look_pair(str(path), str(image)))
    return out


def metric_scale_consistency(images: list[Path], scales=RENDER_SCALES,
                             delivery: tuple[int, int] | None = None) -> Result:
    """The same picture, delivered at different resolutions, must measure the same.

    Written against the INTENDED behaviour, which the code does not fully have. A failure
    here is the finding, not a broken test: it says part of every metric delta the
    pipeline reports on a sub-full-resolution render is resampling rather than the scene.

    Deliberately swept over SEVERAL images. A single frame is not a test of this: a dark,
    low-detail plate passes trivially because every gap falls under the absolute floors
    in metrics._FLOOR, while a dense city frame at the same scale fails hard. Checking
    one image and reporting "scale-invariant" would be its own false negative.

    Only full-resolution plates qualify. An image already smaller than the delivery
    resolution cannot stand in for "the same picture at full quality" — upscaling it to
    build the baseline would put the artifact under test into the control.
    """
    images = [p for p in images if p.is_file()]
    if not images:
        return Result("metric self-consistency", ok=None,
                      detail="no images to check (no refs, no judged renders)")
    skipped: list[str] = []
    if delivery:
        keep = []
        for p in images:
            w, h = Image.open(p).size
            (keep if (w, h) >= delivery else skipped).append(
                p if (w, h) >= delivery else f"{p.name} ({w}x{h})")
        images = keep
        if not images:
            return Result("metric self-consistency", ok=None,
                          detail=f"no image is at the delivery resolution "
                                 f"{delivery[0]}x{delivery[1]}, so none can serve as the "
                                 f"full-quality control. Skipped: {', '.join(skipped)}",
                          data={"skipped": skipped, "per_image": {}})
    tmp = Path(tempfile.mkdtemp(prefix="bvfx-eval-scale-"))
    per_image: dict[str, dict] = {}
    failures: list[str] = []
    for image in images:
        sweep = _one_image_scale_sweep(image, scales, tmp, delivery)
        per_image[image.name] = {str(s): [str(d) for d in ds] for s, ds in sweep.items()}
        bad = {s: ds for s, ds in sweep.items() if ds}
        if not bad:
            continue
        # Lead with a BLOCKING delta when there is one. A blocking metric is the only
        # kind that decides anything — accept_agent skips the critic outright when one
        # trips — so quoting the largest non-blocking halation ratio instead would bury
        # the part that changes an outcome.
        flat = [(s, d) for s, ds in bad.items() for d in ds]
        blocking = [(s, d) for s, d in flat if d.blocking]
        worst_s, top = max(blocking or flat, key=lambda sd: abs(sd[1].rel))
        failures.append(f"{image.name}: out of tolerance at scale(s) "
                        f"{sorted(bad, reverse=True)} ({len(blocking)} blocking); "
                        f"worst{' blocking' if blocking else ''} is scale {worst_s} → {top}")
        log(f"{image.name}: scale-dependent at {sorted(bad, reverse=True)}", 1)

    skip_note = (f"\n      Excluded (below the delivery resolution, so they cannot serve "
                 f"as a full-quality control): {', '.join(skipped)}" if skipped else "")
    if not failures:
        return Result("metric self-consistency", ok=True,
                      detail=f"{len(images)} full-resolution plate(s) measure identically "
                             f"at scales {list(scales)}" + skip_note,
                      data={"per_image": per_image, "skipped": skipped})
    readings = sum(len(strs) for per in per_image.values() for strs in per.values())
    n_block = sum(1 for f in failures for _ in [f] if "(0 blocking)" not in f)
    severity = (
        "NONE of them is a BLOCKING metric, so no verdict currently turns on this — it "
        "is noise in the advice the builder is given, not a false rejection."
        if n_block == 0 else
        f"{n_block} plate(s) show a BLOCKING metric drifting, which CAN flip an "
        f"acceptance verdict: accept_agent skips the critic entirely when a blocking "
        f"metric trips.")
    # The contract is about BLOCKING metrics. Comparison now goes through look_pair, so
    # neither image is ever upscaled and the systematic asymmetry is gone — but hot_core and
    # points_* count pixels above brightness thresholds, so ANY resampling filter moves them,
    # and no common width makes that invariant. (halation_* used to be the example here. It
    # is quieter now not because it became stable but because metrics._HOT_FLOOR_PPM stops
    # it reporting where it never could: the sweep's remaining threshold-counting drift now
    # surfaces as hot_core, which is the honest name for what was always moving.)
    # Failing forever on a property we have accepted is
    # how a check gets ignored; a blocking metric drifting is the thing that can actually
    # flip a verdict, because accept_agent skips the critic outright when one trips.
    return Result(
        "metric self-consistency", ok=(n_block == 0),
        detail=(f"{len(failures)} of {len(images)} full-resolution plate(s) show "
                f"scale-dependent readings when compared against THEMSELVES"
                + (" — but no BLOCKING metric among them, which is the bar: "
                   "threshold-counting metrics like hot_core and points_* move under any "
                   "resampling filter and cannot be made invariant."
                   if n_block == 0 else "") + "\n      "
                f"SEVERITY: {severity}\n      "
                + "\n      ".join(failures)
                + f"\n      ({readings} out-of-tolerance readings in total across the "
                  f"sweep.)"
                + "\n      Every comparison path now goes through metrics.look_pair(), "
                  "which measures both images at a common width no larger than either, so "
                  "nothing is upscaled: guardrails.metrics_feedback, accept_agent, "
                  "build_agent._metric_report and the signed-gap block in "
                  "blender/tools._compare_image. What remains is each metric's own "
                  "sensitivity to resolution, which is a property of the picture, not of "
                  "how it was measured. _ablate still calls look_vector directly, and "
                  "correctly so — it compares two renders made at the SAME scale."
                + skip_note),
        data={"per_image": per_image, "skipped": skipped})


# --------------------------------------------------------------------------- #
# replay equivalence (needs Blender, no model)                                 #
# --------------------------------------------------------------------------- #
def _scene_manifest(session) -> dict:
    """A deterministic, comparable description of the scene.

    `inspect("all")` is used rather than a bespoke dump so this measures the same surface
    the builder and the tools see. Object order comes from bpy's own collection order,
    which is itself part of what we are testing for stability.
    """
    text = session.inspect("all")
    stats = session.call("run", code="RESULT = None")  # scene-delta rides on every run
    return {"inspect": text, "scene": stats.get("scene", {})}


def accepted_prefix(shot: Shot) -> tuple[list, int]:
    """The longest run of layers, from the first, that the ledger records as passed.

    Not `render_shot._chain_scripts`, which refuses anything short of a complete accepted
    chain — correct for producing a deliverable, useless for measuring determinism. A
    half-built shot is the normal state during development and is exactly when a
    non-deterministic script is cheapest to find. Returns (layers, total) so the caller
    can say how much of the chain it actually exercised.
    """

    def num(script: str) -> int:
        m = re.match(r"(\d+)", Path(script).name)
        return int(m.group(1)) if m else 10_000

    layers = sorted(load_layers(shot).values(), key=lambda L: num(L.script))
    ledger = Ledger(shot)
    prefix = []
    for L in layers:
        if ledger.status(L.as_milestone()) != "passed" or not (shot.folder / L.script).is_file():
            break
        prefix.append(L)
    return prefix, len(layers)


def replay_equivalence(shot: Shot, *, blender: str = "blender", passes: int = 2,
                       frame: int | None = None, scale: float = 0.5,
                       mode: str = "eevee") -> Result:
    """Run the accepted chain from empty `passes` times; require the same scene and the
    same look vector every time."""

    try:
        layers, total = accepted_prefix(shot)
    except Exception as e:
        return Result("replay equivalence", ok=None,
                      detail=f"cannot determine the accepted chain: {str(e)[:200]}")
    if not layers:
        return Result("replay equivalence", ok=None,
                      detail="no layer is recorded 'passed' with a script on disk — "
                             "there is no accepted chain to replay yet")
    scripts = [shot.folder / L.script for L in layers]
    partial = ("" if len(layers) == total else
               f"PARTIAL CHAIN: {len(layers)} of {total} layers are accepted, so this "
               f"exercises the accepted prefix only. ")

    if frame is None:
        # The last accepted layer's own judge frame: the frame the pipeline already
        # decided is worth looking at for the deepest layer we can replay, so a
        # difference here is a difference someone already cares about.
        frame = layers[-1].judge_frame

    manifests, vectors = [], []
    tmp = Path(tempfile.mkdtemp(prefix="bvfx-eval-replay-"))
    try:
        with BlenderSession(blender=blender, cwd=shot.folder,
                            assets_dir=shot.folder / "assets") as session:
            for i in range(passes):
                log(f"replay pass {i + 1}/{passes}: "
                    f"{len(scripts)} script(s) from an empty scene", 1)
                session.run(_RESET)
                session.run(_preamble(shot))
                for p in scripts:
                    _run_artifact_script(session, p)
                manifests.append(_scene_manifest(session))
                out = tmp / f"pass{i}.png"
                out.write_bytes(Path(session.render(frame=frame, mode=mode,
                                                    scale=scale)).read_bytes())
                vectors.append(look_vector(str(out)))
    except BlenderError as e:
        return Result("replay equivalence", ok=None,
                      detail=f"Blender could not replay the chain: {str(e)[:300]}")

    problems, data = [], {"frame": frame, "scripts": [p.name for p in scripts],
                          "passes": passes, "layers_replayed": len(layers),
                          "layers_total": total}
    base = manifests[0]
    for i, man in enumerate(manifests[1:], start=2):
        if man["scene"] != base["scene"]:
            problems.append(f"pass {i} scene stats differ: {base['scene']} vs {man['scene']}")
        if man["inspect"] != base["inspect"]:
            diff = _first_diff(base["inspect"], man["inspect"])
            problems.append(f"pass {i} scene manifest differs — first difference: {diff}")
    # The look vector is judged by the SAME tolerance the pipeline uses to decide a
    # render matches a reference. A replay that would be flagged as "off-reference"
    # against its own twin is non-deterministic by the pipeline's own standard.
    for i, v in enumerate(vectors[1:], start=2):
        deltas = compare(v, vectors[0])
        if deltas:
            problems.append(f"pass {i} look vector out of tolerance vs pass 1: "
                            + "; ".join(str(d) for d in deltas[:3]))
    data["max_metric_drift"] = _max_drift(vectors)

    if problems:
        return Result("replay equivalence", ok=False,
                      detail=partial + "\n      ".join(problems), data=data)
    return Result("replay equivalence", ok=True,
                  detail=(partial
                          + f"{len(scripts)} script(s), {passes} passes from empty: "
                          f"identical scene manifest and look vector at f{frame} "
                          f"(worst metric drift {data['max_metric_drift']:.4g}, "
                          f"tolerance-clean).\n      "
                          f"Scope note: all passes ran in ONE Blender process with a "
                          f"factory reset between them, so this proves script-level "
                          f"determinism, not process-level — a script that depends on "
                          f"module state surviving `read_factory_settings` would still "
                          f"pass here."),
                  data=data)


def _first_diff(a: str, b: str) -> str:
    for la, lb in zip(a.splitlines(), b.splitlines(), strict=False):
        if la != lb:
            return f"{la!r} vs {lb!r}"
    return f"length {len(a.splitlines())} vs {len(b.splitlines())} lines"


def _max_drift(vectors: list[dict]) -> float:
    if len(vectors) < 2:
        return 0.0
    worst = 0.0
    for key in vectors[0]:
        vals = [v.get(key, 0.0) for v in vectors]
        base = max(abs(statistics.fmean(vals)), 1e-6)
        worst = max(worst, (max(vals) - min(vals)) / base)
    return worst
