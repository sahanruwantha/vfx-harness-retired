"""Drain the recipe-distillation queue after a run.

Distillation harvests reusable recipes from a layer that passed and from the API errors
the builder worked around. It used to run INLINE at the end of every layer, so a run sat
waiting for a model to finish writing prose before the next layer could start — for work
whose only consumer is a FUTURE layer. Nothing downstream in the current run reads it.

    python -m vfx_harness.agents.distill <shot-folder> [--keep]

Each queued request is removed as it succeeds, so re-running after a failure retries only
what is left. --keep leaves the queue in place for inspection.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import anyio

from vfx_harness.agents.builder import distill_recipe
from vfx_harness.domain.brief import load_shot
from vfx_harness.infrastructure.config import load_environment
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.orchestration.ledger import Milestone, load_layers

QUEUE = "distill_queue.jsonl"


def _queue_path(folder: Path) -> Path:
    layout = run_artifacts.select(folder)
    if not layout:
        raise FileNotFoundError(f"no structured run under {folder / 'runs'}")
    return layout.logs / QUEUE


def _pending(folder: Path) -> list[dict]:
    q = _queue_path(folder)
    if not q.is_file():
        return []
    out = []
    for line in q.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            log(f"! skipping unparseable queue line ({e}): {line[:80]}")
    return out


async def _run(folder: str, keep: bool) -> None:
    shot = load_shot(folder)
    reqs = _pending(shot.folder)
    if not reqs:
        log(f"nothing queued in {_queue_path(shot.folder)}")
        return
    log(f"draining {len(reqs)} distillation request(s) for {shot.id}")
    layers = load_layers(shot)
    done, failed = [], []
    for r in reqs:
        mid = r.get("milestone")
        g = layers.get(mid)
        m = g.as_milestone() if g else Milestone(
            id=mid, frame=1, ref="", reads="(queued distillation)")
        try:
            await distill_recipe(shot, m, True, script_rel=r.get("script_rel"),
                                 errors=r.get("errors") or [])
            done.append(r)
        except Exception as e:
            log(f"! {mid} failed to distil: {str(e)[:100]}")
            failed.append(r)
    if not keep:
        q = _queue_path(shot.folder)
        if failed:
            q.write_text("".join(json.dumps(r) + "\n" for r in failed), encoding="utf-8")
        else:
            q.unlink(missing_ok=True)
    log(f"distilled {len(done)}/{len(reqs)}"
        + (f"; {len(failed)} left queued for a retry" if failed and not keep else ""))


def main() -> None:
    load_environment()
    ap = argparse.ArgumentParser(description="Drain the recipe-distillation queue.")
    ap.add_argument("folder", help="shot folder")
    ap.add_argument("--keep", action="store_true",
                    help="do not remove drained requests from the queue")
    args = ap.parse_args()
    anyio.run(_run, args.folder, args.keep)


if __name__ == "__main__":
    main()
