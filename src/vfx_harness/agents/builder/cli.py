"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import argparse

import anyio

from vfx_harness.agents.builder.models import (
    BuildTruncated,
    BuildUnpassed,
    LayerVerdictFailed,
    UnpassedPrior,
    builder_model,
    critic_model,
)
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import ChainBroken
from vfx_harness.agents.shot_context import clear_layer_context
from vfx_harness.application.preflight import warn_if_broken
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import load_shot
from vfx_harness.infrastructure.config import load_environment
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.observability.provenance import check as provenance_check
from vfx_harness.orchestration.escalate import unanswered_for_layer
from vfx_harness.orchestration.ledger import load_layers
from vfx_harness.orchestration.plan_authority import POINTER, resolve_current
from vfx_harness.orchestration.plan_due import require_due_clear
from vfx_harness.orchestration.unit_state import load as load_unit_state


async def _run(
    folder: str, layer_id: str, rounds: int, blender: str, resume_ok: bool = False, force: bool = False
) -> None:
    # Cheapest possible check, first: a credential in a variable nothing reads costs a
    # whole layer to discover otherwise, and it does not fail loudly when it happens.
    warn_if_broken()
    shot = load_shot(folder)
    # Is the plan still a plan for THIS brief? Editing brief.md leaves the plan stale with
    # nothing recording the divergence, and every layer below is then built to a spec that
    # no longer exists. An edited INPUT refuses; an edited artifact only warns, since
    # hand-tuning layers.json is a legitimate thing to do mid-build.

    if (shot.folder / POINTER).exists():
        resolve_current(shot.folder)
        stale = []
    else:
        stale = provenance_check(shot.folder)
    for s in stale:
        log(f"! plan provenance: {s}")
    if any("CHANGED since the plan" in s for s in stale) and not force:
        log("   re-plan with `python -m vfx_harness.agents.planner <folder>`, or --force")
        raise SystemExit(8)

    # Questions are asked at PLAN time and must be settled BEFORE any layer runs. Building
    # on an unanswered assumption is how barrel_roll ended up 16:9 against 2:1 references
    # — by the time a later layer could notice, the camera had been committed three layers
    # earlier and every composition score was measured against the wrong crop.
    layers = load_layers(shot)
    g = layers.get(layer_id) or layers.get(layer_id.upper())
    if g is None:
        raise SystemExit(f"unknown layer {layer_id!r}; known: {', '.join(layers)}")

    require_due_clear(shot.folder, layer=str(g.id))
    unanswered = unanswered_for_layer(shot.folder, g)
    if unanswered and not force:
        log(
            f"✗ {len(unanswered)} unanswered question(s) from the plan — answer them "
            f"before building (or pass --force to build on the assumptions):"
        )
        for q in unanswered:
            log(f"   Q{q['id']}: {q['question']}", 1)
            log(f"        assuming: {q['assumption']}", 1)
        log(f'   answer with: vfx escalate {folder} --answer <id> "..."')
        raise SystemExit(5)
    if unanswered:
        log(f"! building with {len(unanswered)} question(s) unanswered (--force)")
    session = BlenderSession(
        blender=blender, blend_file=None, assets_dir=shot.folder / "assets", cwd=shot.folder
    ).start()
    try:
        # Name EVERY judge frame. The banner used to print only the first, while the very
        # next line said "answers for 4 frames" — and single-frame judging is precisely
        # the bug that let a blacked-out stretch of barrel_roll through, so a banner that
        # under-reports the judge list is the wrong thing to get wrong.
        judged = " · ".join(f"f{f} vs {r}" for f, r in g.judges) or "no judge frame"
        log(
            f"build agent: shot '{shot.id}' LAYER {g.id} — {g.title} "
            f"(judges: {judged}) → {g.script}, builder {builder_model()}, critic {critic_model()}"
        )
        ledger = await builder_package().build_layer(
            shot, g, session, rounds=rounds, resume_ok=resume_ok, force=force
        )
        status = ledger.status(g.as_milestone())
        log(f"{g.id}: {status}  →  {ledger.path}")

        unit_state = load_unit_state(shot.folder, str(g.id))
        unpassed = [
            f"{uid}={row.get('status')}"
            for uid, row in (unit_state.get("units") or {}).items()
            if row.get("status") != "passed"
        ]
        if unpassed:
            raise BuildUnpassed(
                f"layer {g.id} did not accept every work unit: {', '.join(unpassed)}"
            )
        if status not in {"passed", "reproduced"}:
            raise LayerVerdictFailed(
                f"layer {g.id} units passed but the composed verdict is {status!r}"
            )
    finally:
        session.close()
        # This was imported and never called. write_layer_context() overwrites CLAUDE.md
        # per layer, so nothing leaked BETWEEN layers — but the last layer's contract was
        # left behind in the shot folder, where any later project-scoped session would
        # silently load it as if it were current. Generated context should not outlive
        # the layer that generated it. (Only removes a file it wrote; never a hand-written
        # CLAUDE.md.)
        clear_layer_context(shot)


def main() -> None:
    load_environment()
    ap = argparse.ArgumentParser(description="Build one plan layer with the critic loop.")
    ap.add_argument("folder", help="shot folder (contains brief.md + refs/)")
    ap.add_argument("--layer", required=True, help="layer id from layers.json — 1, 2, 3 …")
    ap.add_argument("--rounds", type=int, default=2, help="max build↔critic rounds")
    ap.add_argument("--blender", default="blender", help="blender executable")
    ap.add_argument(
        "--force",
        action="store_true",
        help="override the pre-build refusals: unanswered plan questions "
        "(uses the assumptions) and unaccepted prior layers. Debugging "
        "only — anything built this way rests on unreviewed work.",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="continue a crashed/truncated run: restore its scene checkpoint, "
        "replay the journal, and resume the same SDK session",
    )
    args = ap.parse_args()
    shot = load_shot(args.folder)
    with run_artifacts.invocation(shot.folder, "build", shot_id=shot.id,
                                  parameters={"layer": args.layer, "rounds": args.rounds}):
        try:
            anyio.run(_run, args.folder, args.layer, args.rounds, args.blender,
                      args.resume, args.force)
        # `from None` on all three: the handler has already logged a message written for a
        # human, and the exit code carries the meaning for the driver. Chaining the original
        # traceback on top would bury both under a stack nobody needs.
        except BuildTruncated as e:
            log(f"BUILD TRUNCATED — {e}")
            raise run_artifacts.RequestedExit(
                3,
                f"BUILD TRUNCATED — {e}",
                terminal_cause=e.terminal_cause,
            ) from None
        except BuildUnpassed as e:
            log(f"BUILD UNPASSED — {e}")
            raise run_artifacts.RequestedExit(7, f"INCOMPLETE CHAIN — {e}") from None
        except LayerVerdictFailed as e:
            log(f"LAYER VERDICT — {e}")
            raise run_artifacts.RequestedExit(9, str(e)) from None
        except ChainBroken as e:
            log(f"CHAIN BROKEN — {e}")
            raise run_artifacts.RequestedExit(4, f"CHAIN BROKEN — {e}") from None
        except UnpassedPrior as e:
            log(f"UNACCEPTED PRIOR — {e}")
            raise run_artifacts.RequestedExit(6, f"UNACCEPTED PRIOR — {e}") from None
