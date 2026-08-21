"""Stage 2 — the PLAN harness.

Standard flow is TWO-PASS, an A/B-tested division of labour:

  pass 1  DRAFT   (default claude-sonnet-5) — from-scratch forensics: deep scene
          read, research, spikes. Empirically the stronger cold-start discoverer.
  pass 2  VERIFY  (default claude-sonnet-5) — adversarial audit of the draft:
          frame claims re-derived, still↔source twins metric-matched, spike
          citations evidence-checked, gaps measured, missed prior work salvaged.
          Empirically the stronger reviewer. Writes the superseding plans/global.md.

  loop    REPAIR (--until-clean)              — the two passes are both model passes,
          so "solid" was the model's own opinion of its own work. --until-clean runs
          a DETERMINISTIC gate (eval.plan_gate) over the artifacts on disk after the
          verify pass and feeds its findings back as a narrow repair brief, until the
          gate clears, stops moving, or hits --max-rounds.

Why the loop is gated on a machine and not on another critique pass: every mechanism
built to make the planner self-correcting is self-certified, and measured across both
shots all of them read zero. `[unknown]` — which is what gates web research — was used
0 times in all four plan documents. `ask_supervisor` never fired. No plan contains a
single source URL, though step 6 requires them. The verify pass, whose instruction #2
is "measure-check every approval still", shipped 7 fingerprints that do not reproduce.
Meanwhile spike citations are precise and TRUE. The difference is that `spike` writes a
file to the lab and `WebSearch` writes nothing: evidence a tool physically deposits
survives, evidence the model is merely asked to record does not.

The draft is kept alongside (`plans/global.draft.md` + its lab dir) as the audit trail,
and each repair round snapshots its input as `plans/global.roundN.md`.
`--single` runs one from-scratch pass (the pre-two-pass behavior);
`--verify-only` skips pass 1 and audits an existing draft.

Both passes run with the same tool surface the build harness deliberately lacks:
still forensics (measure_ref), a Blender spike lab, the cookbook, the open
web, and the one-shot headless Blender spike lab.

Usage:
    python -m bambi_vfx.agents.planner <shot-folder>                     # two-pass
    python -m bambi_vfx.agents.planner <shot-folder> --until-clean       # + repair loop
    python -m bambi_vfx.agents.planner <shot-folder> --single [--model M]
    python -m bambi_vfx.agents.planner <shot-folder> --verify-only
    common flags: [--draft-model M] [--verify-model M] [--blender BIN]
                  [--max-turns N] [--tag T] [--max-rounds N]
    python -m bambi_vfx.agents.planner <shot-folder> --layer 2  # JIT layer plan

    bambi evals plan <shot-folder>        # run the gate alone — free, no model
    bambi evals plan --feedback           # the repair brief a round would receive
"""

from __future__ import annotations

import argparse
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, query

from .. import costlog, transcript
from ..brief import load_shot
from ..config import DEFAULT_EXECUTION_MODEL, Settings
from ..layer_plans import (
    amendment_block,
    contract_gaps_block,
    global_plan_path,
    prior_outcomes_block,
    work_unit_plan_path,
)
from ..ledger import load_layers
from ..log import log, log_message
from ..plan_tools import build_plan_tools
from ..prompts import (
    LAYER_PLANNER_ADDENDUM,
    PLANNER_SYSTEM,
    REPAIR_ADDENDUM,
    VERIFIER_ADDENDUM,
    layer_user_prompt,
    planner_user_prompt,
    repair_user_prompt,
    verifier_user_prompt,
)
from ..recipes import build_recipe_tools
from ..resilience import run_session
from .builder import _one_user_message

DRAFT_MODEL = DEFAULT_EXECUTION_MODEL
# Draft and verify deliberately share the configured planner model. Their independence
# comes from distinct sessions and an adversarial contract, not from pretending two calls
# to one model are statistically independent. CLI flags can still create a mixed-model
# lane when an experiment needs it.
VERIFY_MODEL = DEFAULT_EXECUTION_MODEL
MODEL = VERIFY_MODEL  # single-pass default


def _planner_tool_policy(repair: bool) -> tuple[list[str], list[str]]:
    """Repair patches directly; draft/verify may explore but cannot mutate in place."""
    allowed = ["Edit"] if repair else []
    denied = ["Bash", *(["Task", "Agent"] if repair else ["Edit"])]
    return allowed, denied


# The reference board is the visual half of the brief, and the harness used to hand over
# a list of FILENAMES. `_refs_block`'s own docstring said "stills are the ONLY visual
# input" and then emitted ten lines of text. Nothing else supplied a picture either:
# measure_ref returned numbers, and contact_sheet/extract_frames — the tools that DO
# return images — were defined but never registered, so they were uncallable on every
# shot (now fixed, and registered when the shot has video). Whether the planner ever saw
# the thing it was planning came down to whether it happened to try Read on a .jpg.
#
# It did, in the run that produced barrel_roll — all ten. And the plan it wrote still
# scored 26 mentions of `halation` against ZERO for camera angle, shadow side or solid
# form. So attaching these is not sufficient on its own; it is the half that can be
# guaranteed, and an unguaranteed input is the wrong thing to be debugging around later.
# The critic has had this guarantee for a while — it "cannot score a frame it never saw"
# — and there is no argument for holding the stage that WRITES the targets to a lower bar
# than the stage that checks them.
_KICKOFF_MAX_PX = 1568  # same budget the critic uses; ~1600 tokens per still


def _kickoff_blocks(text: str, shot) -> list[dict]:
    """The kickoff prose followed by every reference still, in shot order."""
    from .builder import _image_block

    blocks: list[dict] = [{"type": "text", "text": text}]
    for p in shot.refs:
        try:
            blocks.append(_image_block(p, _KICKOFF_MAX_PX))
        except Exception as e:  # a corrupt plate must not cost the whole pass
            log(f"! could not attach {p.name}: {str(e)[:120]}", 1)
    return blocks


async def generate_plan(
    folder: str | Path,
    *,
    model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_draft: str | None = None,
    repair: tuple[str, int] | None = None,
) -> Path:
    """Run ONE global planning session. With `tag`, outputs are isolated:
    plans/global.md → plans/global.<tag>.md, lab artifacts → logs/plan_lab_<tag>/.
    With `verify_draft`, the session runs in VERIFY MODE against that draft file.
    With `repair=(findings, round)`, it runs in REPAIR MODE against `verify_draft`."""
    shot = load_shot(folder)
    model = model or Settings.from_environment(load_dotenv_file=False).planner_model
    plan_path = global_plan_path(shot.folder)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    lab_dir = shot.folder / "logs" / (f"plan_lab_{tag}" if tag else "plan_lab")

    pserver, pnames = build_plan_tools(shot.folder, blender=blender, lab_dir=lab_dir)
    rserver, rnames = build_recipe_tools()

    if repair:
        findings, rnd = repair
        system = PLANNER_SYSTEM + REPAIR_ADDENDUM.format(draft=verify_draft, findings=findings)
        kickoff = repair_user_prompt(shot, verify_draft, rnd)
        mode = f"REPAIR round {rnd} (against {verify_draft})"
    elif verify_draft:
        system = PLANNER_SYSTEM + VERIFIER_ADDENDUM.format(draft=verify_draft)
        kickoff = verifier_user_prompt(shot, verify_draft)
        mode = f"VERIFY (auditing {verify_draft})"
    else:
        system = PLANNER_SYSTEM
        kickoff = planner_user_prompt(shot)
        mode = "PLAN (from scratch)"

    # Repair is a PATCHING job, not a fresh authorship job. Round 4 spent 38 minutes
    # delegating exact edits to subagents that did not have Edit, then rewrote a 1,390-line
    # plan through Write. Give the repair session the precise tool directly and remove the
    # delegation escape hatch; draft/verify keep their existing exploration behaviour.
    repair_tools, denied = _planner_tool_policy(bool(repair))
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        cwd=str(shot.folder),
        mcp_servers={"plan": pserver, "recipes": rserver},
        allowed_tools=["Read", "Glob", "Grep", "Write", *repair_tools, "WebSearch", "WebFetch", *pnames, *rnames],
        disallowed_tools=denied,
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # sheets/frames as base64 image blocks
        setting_sources=[],  # isolate from user/project settings
        max_turns=max_turns,
        effort="high",
    )

    stills = [p.name for p in shot.refs]
    videos = sorted(p.name for p in (shot.folder / "refs").glob("*.mp4"))
    log(
        f"plan agent [{mode}]: shot '{shot.id}' ({shot.frames}f @ {shot.fps}fps, "
        f"{shot.engine}), model {model}" + (f", tag '{tag}'" if tag else "")
    )
    log(f"refs: {len(stills)} stills {stills} + {len(videos)} videos {videos}", 1)
    log(
        f"lab: blender '{blender}' · artifacts → {lab_dir.relative_to(shot.folder)}/ · "
        f"web research ENABLED · max_turns {max_turns}",
        1,
    )

    costlog.bind(shot.folder, role="plan:" + mode.split()[0].lower(), model=model, tag=tag)
    tpath = transcript.bind(shot.folder, "plan", label=tag or mode)
    if tpath:
        log(f"transcript → {tpath.relative_to(shot.folder)}", 1)
    transcript.prompt(
        kickoff, role="kickoff", mode=mode, model=model, tag=tag, refs=stills, videos=videos, max_turns=max_turns
    )
    blocks = _kickoff_blocks(kickoff, shot)
    log(f"kickoff: {len(blocks) - 1} reference still(s) ATTACHED as images", 1)
    # The post-condition, not the absence of an exception. Two repair rounds were lost to a
    # session that raised "error result: success" at $0.0007 having written nothing, and the
    # real cause ("Repeated 529 Overloaded errors") was only in its assistant text — which is
    # why one attempt collects that text and hands it to the classifier.
    before = plan_path.stat().st_mtime_ns if plan_path.is_file() else -1

    async def _attempt() -> str:
        said: list[str] = []
        async for message in query(prompt=_one_user_message(blocks), options=options):
            log_message(message)
            for blk in getattr(message, "content", None) or []:
                text = getattr(blk, "text", None)
                if text:
                    said.append(text)
        return "\n".join(said)[-4000:]

    def _wrote() -> bool:
        return plan_path.is_file() and plan_path.stat().st_mtime_ns != before

    try:
        await run_session(_attempt, succeeded=_wrote, label=f"plan {mode}")
    except Exception as e:
        log(f"! plan session died: {str(e)[:200]}")
        transcript.event("died", error=str(e)[:2000])
        raise
    finally:
        transcript.unbind()
        costlog.unbind()
    if tag:
        final = plan_path.with_name(f"global.{tag}.md")
        plan_path.rename(final)
        plan_path = final
    lines = plan_path.read_text(encoding="utf-8").count("\n")
    log(f"plan written: {plan_path.relative_to(shot.folder)} ({lines} lines)")
    return plan_path


async def generate_layer_plan(
    folder: str | Path,
    layer_id: str,
    *,
    unit_id: str | None = None,
    model: str | None = None,
    blender: str = "blender",
    max_turns: int = 24,
) -> Path:
    """Generate one work-unit plan after its declared dependencies have sealed outcomes.

    This is intentionally a separate session and output contract. It cannot mutate the
    global plan or machine contracts, and there is no monolithic-plan fallback.
    """
    shot = load_shot(folder)
    model = model or Settings.from_environment(load_dotenv_file=False).planner_model
    global_path = global_plan_path(shot.folder)
    if not global_path.is_file():
        raise FileNotFoundError(f"{global_path} missing — generate and gate the strict global plan first")
    layers = load_layers(shot)
    try:
        layer = layers[str(layer_id)]
    except KeyError as exc:
        raise KeyError(f"unknown layer {layer_id!r}; available: {', '.join(layers)}") from exc
    from ..unit_state import load as load_unit_state
    from ..unit_state import validate_current
    from ..work_units import ready_units

    state = load_unit_state(shot.folder, str(layer.id))
    validate_current(state, str(layer.id), layer.stages)
    passed = {
        uid for uid, row in (state.get("units") or {}).items() if row.get("status") == "passed"
    }
    ready = ready_units(layer.stages, passed)
    if unit_id is not None:
        selected = next((unit for unit in layer.stages if unit.id == unit_id), None)
        if selected is None:
            raise KeyError(
                f"unknown unit {unit_id!r} in layer {layer.id}; available: "
                + ", ".join(unit.id for unit in layer.stages)
            )
        missing = sorted(set(selected.depends_on) - passed)
        if missing:
            raise ValueError(
                f"layer {layer.id} unit {selected.id} is blocked by unpassed dependencies: "
                + ", ".join(missing)
            )
    else:
        if not ready:
            raise ValueError(
                f"layer {layer.id} has no plannable unit; all units passed or dependencies are blocked"
            )
        selected = ready[0]
    target = work_unit_plan_path(shot.folder, selected)
    target.parent.mkdir(parents=True, exist_ok=True)
    rel_target = target.relative_to(shot.folder).as_posix()
    feedback = "\n\n".join(
        x
        for x in (
            prior_outcomes_block(shot.folder, str(layer.id)),
            amendment_block(shot.folder, str(layer.id)),
            contract_gaps_block(shot.folder, str(layer.id), selected.id),
        )
        if x
    )
    # Do not carry the global planner's monolithic output contract into a layer session.
    # The layer doctrine is intentionally self-contained and much smaller.
    system = LAYER_PLANNER_ADDENDUM.format(
        layer_id=layer.id,
        layer_title=layer.title,
        unit_id=selected.id,
        unit_title=selected.title,
        target=rel_target,
    )
    kickoff = layer_user_prompt(shot, layer, selected, rel_target, feedback)
    lab_dir = shot.folder / "logs" / f"plan_lab_layer_{int(layer.id):02d}"
    pserver, pnames = build_plan_tools(shot.folder, blender=blender, lab_dir=lab_dir)
    rserver, rnames = build_recipe_tools()
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        cwd=str(shot.folder),
        mcp_servers={"plan": pserver, "recipes": rserver},
        allowed_tools=["Read", "Write", *pnames, *rnames],
        disallowed_tools=["Bash", "Edit"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=max_turns,
        effort="high",
    )
    before = target.stat().st_mtime_ns if target.is_file() else -1
    blocks = _kickoff_blocks(kickoff, shot)
    costlog.bind(shot.folder, role="plan:layer", model=model, tag=str(layer.id))
    transcript.bind(shot.folder, "plan", label=f"layer-{layer.id}")
    transcript.prompt(
        kickoff, role="kickoff", mode="PLAN_LAYER", model=model, layer=layer.id, refs=[p.name for p in shot.refs]
    )

    async def _attempt() -> str:
        said: list[str] = []
        async for message in query(prompt=_one_user_message(blocks), options=options):
            log_message(message)
            for blk in getattr(message, "content", None) or []:
                if text := getattr(blk, "text", None):
                    said.append(text)
        return "\n".join(said)[-4000:]

    def _wrote() -> bool:
        return target.is_file() and target.stat().st_mtime_ns != before

    try:
        log(f"plan agent [LAYER {layer.id} · UNIT {selected.id}]: {selected.title} → {rel_target}")
        await run_session(_attempt, succeeded=_wrote, label=f"plan layer {layer.id} unit {selected.id}")
    finally:
        transcript.unbind()
        costlog.unbind()
    text = target.read_text(encoding="utf-8")
    if len(text.strip()) < 200:
        raise ValueError(f"{target} is too small to be an executable layer plan")
    if text.count("\n") + 1 > 160:
        raise ValueError(
            f"{target} has {text.count(chr(10)) + 1} lines; layer plans are capped at 160. "
            "Keep evidence in machine contracts/outcomes and rewrite this as an execution index"
        )
    log(f"unit plan written: {rel_target} ({text.count(chr(10))} lines)")
    return target


async def generate_plan_two_pass(
    folder: str | Path,
    *,
    draft_model: str | None = None,
    verify_model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_only: bool = False,
) -> Path:
    """The standard flow: draft from scratch, then adversarially verify.
    Keeps the draft (plan.<tag->draft.md + its lab) as the audit trail."""
    shot = load_shot(folder)
    configured = Settings.from_environment(load_dotenv_file=False).planner_model
    draft_model = draft_model or configured
    verify_model = verify_model or configured
    dtag = f"{tag}-draft" if tag else "draft"
    draft_path = global_plan_path(shot.folder).with_name(f"global.{dtag}.md")

    if verify_only:
        if not draft_path.is_file():
            raise FileNotFoundError(f"--verify-only needs an existing {draft_path.name}")
        log(f"two-pass: reusing existing draft {draft_path.name}")
    else:
        log(f"══ two-pass 1/2 · DRAFT · {draft_model} ══")
        await generate_plan(folder, model=draft_model, blender=blender, max_turns=max_turns, tag=dtag)

    log(f"══ two-pass 2/2 · VERIFY · {verify_model} · auditing {draft_path.name} ══")
    final = await generate_plan(
        folder,
        model=verify_model,
        blender=blender,
        max_turns=max_turns,
        tag=tag,
        verify_draft=draft_path.relative_to(shot.folder).as_posix(),
    )
    log(f"two-pass complete → {final.name} (draft kept: {draft_path.name})")
    return final


async def generate_plan_until_clean(
    folder: str | Path,
    *,
    draft_model: str | None = None,
    verify_model: str | None = None,
    blender: str = "blender",
    max_turns: int = 100,
    tag: str | None = None,
    verify_only: bool = False,
    max_rounds: int = 3,
) -> Path:
    """Draft → verify → GATE → repair → gate → … until the plan clears or stops moving.

    The loop exists because "solid" was previously a model's own opinion of its own work,
    and every mechanism built to make it more than that was self-certified: the planner
    marked `[unknown]` zero times across four documents (which is what gates research), it
    never once called `ask_supervisor`, and the verify pass — whose instruction #2 is
    "measure-check every approval still" — shipped seven fingerprints that do not
    reproduce. So the thing this loop converges against is a DETERMINISTIC gate over the
    artifacts on disk, not another critique pass. A model cannot talk its way past it.

    Three ways to stop, and two of them are failures that must be reported as failures:

      clean    no blocking findings — the plan aims at things that are actually there
      stalled  a round produced the SAME findings as the one before it. The repair pass
               is not converging, and paying for another identical answer helps nobody
      budget   max_rounds reached with findings outstanding

    Returns the plan path either way; the caller decides whether to build on it. This
    function does not raise on a dirty plan, because a plan with known, listed defects is
    more useful than no plan — what must never happen is a dirty plan looking clean.
    """
    from ..eval import plan_gate

    configured = Settings.from_environment(load_dotenv_file=False).planner_model
    draft_model = draft_model or configured
    verify_model = verify_model or configured

    shot = load_shot(folder)
    final = await generate_plan_two_pass(
        folder,
        draft_model=draft_model,
        verify_model=verify_model,
        blender=blender,
        max_turns=max_turns,
        tag=tag,
        verify_only=verify_only,
    )
    plan_name = final.relative_to(shot.folder).as_posix()

    prev_sig, outcome = None, "budget"
    for rnd in range(1, max_rounds + 1):
        # New plans use the current five-artifact contract.  The standalone gate keeps
        # missing scene checks warning-only for legacy shots, but a planner running now
        # must not claim CLEAN while leaving numeric scene facts to the vision judge.
        res = plan_gate.run(shot.folder, plan_name, require_scene_checks=True)
        log(f"══ gate {rnd}/{max_rounds} ══")
        log(plan_gate.report(res), 1)
        if res.clean:
            outcome = "clean"
            break
        sig = res.signature()
        if sig == prev_sig:
            outcome = "stalled"
            log(
                f"! gate findings are unchanged from round {rnd - 1} — the repair pass is "
                f"not converging. Stopping rather than paying for the same answer again."
            )
            break
        prev_sig = sig
        # Snapshot the plan being repaired: the repair session reads one file and writes
        # plans/global.md, and otherwise it would read the file it is replacing.
        snap = global_plan_path(shot.folder).with_name(f"global.round{rnd}.md")
        snap.write_text(final.read_text(encoding="utf-8"), encoding="utf-8")
        log(f"══ repair {rnd}/{max_rounds} · {verify_model} · {len(res.blocking)} blocking finding(s) → {snap.name} ══")
        final = await generate_plan(
            folder,
            model=verify_model,
            blender=blender,
            max_turns=max_turns,
            tag=tag,
            verify_draft=snap.relative_to(shot.folder).as_posix(),
            repair=(plan_gate.feedback(res), rnd),
        )
        plan_name = final.relative_to(shot.folder).as_posix()
    else:
        res = plan_gate.run(shot.folder, plan_name, require_scene_checks=True)
        log("══ gate (final) ══")
        log(plan_gate.report(res), 1)
        outcome = "clean" if res.clean else "budget"

    n = len(res.blocking)
    log(
        f"plan loop {outcome.upper()}: {final.name}"
        + (
            ""
            if outcome == "clean"
            else f" — {n} blocking finding(s) REMAIN. `bambi evals plan {shot.folder}` lists "
            f"them; building on this plan means building toward them."
        )
    )
    return final


def main() -> None:
    settings = Settings.from_environment()
    ap = argparse.ArgumentParser(description="Plan a shot. Default: two-pass (draft → adversarial verify).")
    ap.add_argument("folder", help="shot folder (contains brief.md, refs/)")
    ap.add_argument("--layer", help="generate only this layer's just-in-time plan")
    ap.add_argument("--unit", help="with --layer, generate this ready work unit instead of the first ready unit")
    ap.add_argument("--single", action="store_true", help="one from-scratch pass with --model (no verify)")
    ap.add_argument(
        "--verify-only", action="store_true", help="skip drafting; audit the existing plans/global.<tag->draft.md"
    )
    ap.add_argument("--model", default=settings.planner_model, help="model for --single or --layer runs")
    ap.add_argument("--draft-model", default=settings.planner_model)
    ap.add_argument("--verify-model", default=settings.planner_model)
    ap.add_argument(
        "--blender", default=settings.blender_bin, help="blender executable for the spike lab"
    )
    ap.add_argument(
        "--max-turns", type=int, default=None, help="turn cap per pass (default: 24 for --layer, 100 globally)"
    )
    ap.add_argument("--tag", default=None, help="isolate outputs: plans/global.<tag>.md + logs/plan_lab_<tag>/")
    ap.add_argument(
        "--until-clean",
        action="store_true",
        help="after the two passes, run the deterministic plan gate and "
        "repair until it clears, stalls, or hits --max-rounds",
    )
    ap.add_argument("--max-rounds", type=int, default=3, help="repair rounds for --until-clean (default 3)")
    args = ap.parse_args()

    if args.unit and not args.layer:
        ap.error("--unit requires --layer")
    if args.layer and (args.single or args.verify_only or args.until_clean or args.tag):
        ap.error("--layer is a dedicated JIT pass; do not combine it with global-pass flags")

    if args.layer:
        plan_path = anyio.run(
            lambda: generate_layer_plan(
                args.folder,
                args.layer,
                unit_id=args.unit,
                model=args.model,
                blender=args.blender,
                max_turns=args.max_turns or 24,
            )
        )
    elif args.single:
        plan_path = anyio.run(
            lambda: generate_plan(
                args.folder, model=args.model, blender=args.blender, max_turns=args.max_turns or 100, tag=args.tag
            )
        )
    elif args.until_clean:
        plan_path = anyio.run(
            lambda: generate_plan_until_clean(
                args.folder,
                draft_model=args.draft_model,
                verify_model=args.verify_model,
                blender=args.blender,
                max_turns=args.max_turns or 100,
                tag=args.tag,
                verify_only=args.verify_only,
                max_rounds=args.max_rounds,
            )
        )
    else:
        plan_path = anyio.run(
            lambda: generate_plan_two_pass(
                args.folder,
                draft_model=args.draft_model,
                verify_model=args.verify_model,
                blender=args.blender,
                max_turns=args.max_turns or 100,
                tag=args.tag,
                verify_only=args.verify_only,
            )
        )
    log(f"wrote {plan_path}")
    # Record what this plan was derived from, so a later brief edit is detectable
    # instead of silently leaving every layer built to a spec that no longer exists.
    from ..provenance import stamp

    used = args.model if (args.single or args.layer) else f"{args.draft_model}→{args.verify_model}"
    log(f"provenance → {stamp(args.folder, model=used, note='tag=' + str(args.tag))}")


if __name__ == "__main__":
    main()
