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

import statistics
import tempfile
from pathlib import Path

from ..brief import Shot
from ..log import log
from ..metrics import compare, look_vector

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
def _one_image_scale_sweep(image: Path, scales, tmp: Path) -> dict:
    """scale -> the deltas a perfect render at that scale would show against the full-res
    original. Downscaling the image IS the model of a perfect render: same content, fewer
    pixels, which is exactly what `scale` buys you from the render tools."""
    from PIL import Image

    im = Image.open(image).convert("RGB")
    full = look_vector(str(image))
    out = {}
    for s in scales:
        if s >= 1.0:
            path = image
        else:
            path = tmp / f"{image.stem}_s{s}.png"
            im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                      Image.LANCZOS).save(path)
        out[s] = compare(look_vector(str(path)), full)
    return out


def metric_scale_consistency(images: list[Path], scales=RENDER_SCALES) -> Result:
    """The same picture, delivered at different resolutions, must measure the same.

    Written against the INTENDED behaviour, which the code does not yet have. A failure
    here is the finding, not a broken test: it says every metric delta the pipeline
    reports on a sub-full-resolution render is contaminated by resampling, and the
    contamination lands on `detail`, `points` and `detail_bot` — three of the five
    BLOCKING metrics in metrics.SPEC.

    Deliberately swept over SEVERAL images. A single frame is not a test of this: a dark,
    low-detail plate passes trivially because every gap falls under the absolute floors
    in metrics._FLOOR, while a dense city frame at the same scale fails hard. Checking
    one image and reporting "scale-invariant" would be its own false negative.
    """
    images = [p for p in images if p.is_file()]
    if not images:
        return Result("metric self-consistency", ok=None,
                      detail="no images to check (no refs, no judged renders)")
    tmp = Path(tempfile.mkdtemp(prefix="bvfx-eval-scale-"))
    per_image: dict[str, dict] = {}
    failures: list[str] = []
    for image in images:
        sweep = _one_image_scale_sweep(image, scales, tmp)
        per_image[image.name] = {str(s): [str(d) for d in ds] for s, ds in sweep.items()}
        bad = {s: ds for s, ds in sweep.items() if ds}
        if not bad:
            continue
        worst_s, worst_ds = max(bad.items(), key=lambda kv: max(abs(d.rel) for d in kv[1]))
        top = max(worst_ds, key=lambda d: abs(d.rel))
        n_block = sum(1 for ds in bad.values() for d in ds if d.blocking)
        failures.append(f"{image.name}: out of tolerance at scale(s) "
                        f"{sorted(bad, reverse=True)} ({n_block} blocking); worst is "
                        f"scale {worst_s} → {top}")
        log(f"{image.name}: scale-dependent at {sorted(bad, reverse=True)}", 1)

    if not failures:
        return Result("metric self-consistency", ok=True,
                      detail=f"{len(images)} image(s) measure identically at scales "
                             f"{list(scales)}", data={"per_image": per_image})
    return Result(
        "metric self-consistency", ok=False,
        detail=(f"{len(failures)} of {len(images)} image(s) compared against THEMSELVES "
                f"are not scale-invariant. look_vector() resamples every input to 960px "
                f"wide, so a render taken below that is upscaled, and the interpolated "
                f"pixels destroy exactly the high-frequency signal `detail` and `points` "
                f"count.\n      "
                + "\n      ".join(failures)
                + "\n      Consequence: guardrails.metrics_feedback compares the "
                  "builder's scale=0.4 draft against a full-res reference after every "
                  "render, so part of every gap it pushes into the builder's context is "
                  "resampling, not the scene. `detail`, `points` and `detail_bot` are "
                  "BLOCKING metrics, and accept_agent skips the critic entirely when a "
                  "blocking metric trips."),
        data={"per_image": per_image})


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


def replay_equivalence(shot: Shot, *, blender: str = "blender", passes: int = 2,
                       frame: int | None = None, scale: float = 0.5,
                       mode: str = "eevee", force: bool = False) -> Result:
    """Run the accepted chain from empty `passes` times; require the same scene and the
    same look vector every time."""
    from ..blender.session import BlenderError, BlenderSession
    from ..build_agent import _RESET, _preamble
    from ..render_shot import IncompleteRender, _chain_scripts

    try:
        scripts = _chain_scripts(shot, force=force)
    except (IncompleteRender, FileNotFoundError) as e:
        return Result("replay equivalence", ok=None,
                      detail=f"no accepted chain to replay — {str(e)[:200]}")

    if frame is None:
        # The first accepted layer's own judge frame: the frame the pipeline already
        # decided is worth looking at, so a difference here is a difference that matters.
        try:
            from ..ledger import load_layers
            frame = min(L.judge_frame for L in load_layers(shot).values())
        except Exception:       # noqa: BLE001 — reported via the detail string below
            frame = 1

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
                    session.run(p.read_text(encoding="utf-8"))
                manifests.append(_scene_manifest(session))
                out = tmp / f"pass{i}.png"
                out.write_bytes(Path(session.render(frame=frame, mode=mode,
                                                    scale=scale)).read_bytes())
                vectors.append(look_vector(str(out)))
    except BlenderError as e:
        return Result("replay equivalence", ok=None,
                      detail=f"Blender could not replay the chain: {str(e)[:300]}")

    problems, data = [], {"frame": frame, "scripts": [p.name for p in scripts],
                          "passes": passes}
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
                      detail="\n      ".join(problems), data=data)
    return Result("replay equivalence", ok=True,
                  detail=(f"{len(scripts)} script(s), {passes} passes from empty: "
                          f"identical scene manifest and look vector at f{frame} "
                          f"(worst metric drift {data['max_metric_drift']:.4g}, "
                          f"tolerance-clean).\n      "
                          f"Scope note: all passes ran in ONE Blender process with a "
                          f"factory reset between them, so this proves script-level "
                          f"determinism, not process-level."),
                  data=data)


def _first_diff(a: str, b: str) -> str:
    for la, lb in zip(a.splitlines(), b.splitlines()):
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
