"""Stage 2 — the PLAN harness.

Standard flow is TWO-PASS, an A/B-tested division of labour:

  pass 1  DRAFT   (default claude-opus-5)   — from-scratch forensics: deep scene
          read, research, spikes. Empirically the stronger cold-start discoverer.
  pass 2  VERIFY  (default claude-fable-5)  — adversarial audit of the draft:
          frame claims re-derived, still↔source twins metric-matched, spike
          citations evidence-checked, gaps measured, missed prior work salvaged.
          Empirically the stronger reviewer. Writes the superseding plan.md.

The draft is kept alongside (`plan.draft.md` + its lab dir) as the audit trail.
`--single` runs one from-scratch pass (the pre-two-pass behavior);
`--verify-only` skips pass 1 and audits an existing draft.

Both passes run with the same tool surface the build harness deliberately lacks:
still forensics (measure_ref), a Blender spike lab, the cookbook, the open
web, and the one-shot headless Blender spike lab.

Usage:
    python -m pipeline.plan_agent <shot-folder>                     # two-pass
    python -m pipeline.plan_agent <shot-folder> --single [--model M]
    python -m pipeline.plan_agent <shot-folder> --verify-only
    common flags: [--draft-model M] [--verify-model M] [--blender BIN]
                  [--max-turns N] [--tag T]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import anyio
from claude_agent_sdk import ClaudeAgentOptions, query

from .brief import load_shot
from .log import log, log_message
from .plan_tools import build_plan_tools
from .prompts import (PLANNER_SYSTEM, VERIFIER_ADDENDUM, planner_user_prompt,
                      verifier_user_prompt)
from .recipes import build_recipe_tools

DRAFT_MODEL = "claude-opus-5"
VERIFY_MODEL = "claude-fable-5"
MODEL = VERIFY_MODEL  # single-pass default


async def generate_plan(folder: str | Path, *, model: str = MODEL,
                        blender: str = "blender", max_turns: int = 100,
                        tag: str | None = None,
                        verify_draft: str | None = None) -> Path:
    """Run ONE planning session. With `tag`, outputs are isolated:
    plan.md → plan.<tag>.md, lab artifacts → logs/plan_lab_<tag>/.
    With `verify_draft`, the session runs in VERIFY MODE against that draft file."""
    shot = load_shot(folder)
    plan_path = shot.folder / "plan.md"
    lab_dir = shot.folder / "logs" / (f"plan_lab_{tag}" if tag else "plan_lab")

    pserver, pnames = build_plan_tools(shot.folder, blender=blender, lab_dir=lab_dir)
    rserver, rnames = build_recipe_tools()

    if verify_draft:
        system = PLANNER_SYSTEM + VERIFIER_ADDENDUM.format(draft=verify_draft)
        kickoff = verifier_user_prompt(shot, verify_draft)
        mode = f"VERIFY (auditing {verify_draft})"
    else:
        system = PLANNER_SYSTEM
        kickoff = planner_user_prompt(shot)
        mode = "PLAN (from scratch)"

    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system,
        cwd=str(shot.folder),
        mcp_servers={"plan": pserver, "recipes": rserver},
        allowed_tools=["Read", "Glob", "Grep", "Write",
                       "WebSearch", "WebFetch", *pnames, *rnames],
        disallowed_tools=["Edit", "Bash"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # sheets/frames as base64 image blocks
        setting_sources=[],                # isolate from user/project settings
        max_turns=max_turns,
        effort="high",
    )

    stills = [p.name for p in shot.refs]
    videos = sorted(p.name for p in (shot.folder / "refs").glob("*.mp4"))
    log(f"plan agent [{mode}]: shot '{shot.id}' ({shot.frames}f @ {shot.fps}fps, "
        f"{shot.engine}), model {model}" + (f", tag '{tag}'" if tag else ""))
    log(f"refs: {len(stills)} stills {stills} + {len(videos)} videos {videos}", 1)
    log(f"lab: blender '{blender}' · artifacts → {lab_dir.relative_to(shot.folder)}/ · "
        f"web research ENABLED · max_turns {max_turns}", 1)

    try:
        async for message in query(prompt=kickoff, options=options):
            log_message(message)
    except Exception as e:  # noqa: BLE001
        log(f"! plan session died: {str(e)[:200]}")
        raise

    if not plan_path.is_file():
        log(f"! agent finished without writing {plan_path.name}")
        raise RuntimeError(f"agent finished without writing {plan_path.name}")
    if tag:
        final = shot.folder / f"plan.{tag}.md"
        plan_path.rename(final)
        plan_path = final
    lines = plan_path.read_text(encoding='utf-8').count('\n')
    log(f"plan written: {plan_path.name} ({lines} lines)")
    return plan_path


async def generate_plan_two_pass(folder: str | Path, *,
                                 draft_model: str = DRAFT_MODEL,
                                 verify_model: str = VERIFY_MODEL,
                                 blender: str = "blender", max_turns: int = 100,
                                 tag: str | None = None,
                                 verify_only: bool = False) -> Path:
    """The standard flow: draft from scratch, then adversarially verify.
    Keeps the draft (plan.<tag->draft.md + its lab) as the audit trail."""
    shot = load_shot(folder)
    dtag = f"{tag}-draft" if tag else "draft"
    draft_path = shot.folder / f"plan.{dtag}.md"

    if verify_only:
        if not draft_path.is_file():
            raise FileNotFoundError(f"--verify-only needs an existing {draft_path.name}")
        log(f"two-pass: reusing existing draft {draft_path.name}")
    else:
        log(f"══ two-pass 1/2 · DRAFT · {draft_model} ══")
        await generate_plan(folder, model=draft_model, blender=blender,
                            max_turns=max_turns, tag=dtag)

    log(f"══ two-pass 2/2 · VERIFY · {verify_model} · auditing {draft_path.name} ══")
    final = await generate_plan(folder, model=verify_model, blender=blender,
                                max_turns=max_turns, tag=tag,
                                verify_draft=draft_path.name)
    log(f"two-pass complete → {final.name} (draft kept: {draft_path.name})")
    return final


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Plan a shot. Default: two-pass (draft → adversarial verify).")
    ap.add_argument("folder", help="shot folder (contains brief.md, refs/)")
    ap.add_argument("--single", action="store_true",
                    help="one from-scratch pass with --model (no verify)")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip drafting; audit the existing plan.<tag->draft.md")
    ap.add_argument("--model", default=MODEL, help="model for --single runs")
    ap.add_argument("--draft-model", default=DRAFT_MODEL)
    ap.add_argument("--verify-model", default=VERIFY_MODEL)
    ap.add_argument("--blender", default=os.environ.get("BLENDER_BIN", "blender"),
                    help="blender executable for the spike lab")
    ap.add_argument("--max-turns", type=int, default=100, help="turn cap per pass")
    ap.add_argument("--tag", default=None,
                    help="isolate outputs per run: plan.<tag>.md + logs/plan_lab_<tag>/")
    args = ap.parse_args()

    if args.single:
        plan_path = anyio.run(lambda: generate_plan(
            args.folder, model=args.model, blender=args.blender,
            max_turns=args.max_turns, tag=args.tag))
    else:
        plan_path = anyio.run(lambda: generate_plan_two_pass(
            args.folder, draft_model=args.draft_model,
            verify_model=args.verify_model, blender=args.blender,
            max_turns=args.max_turns, tag=args.tag,
            verify_only=args.verify_only))
    log(f"wrote {plan_path}")
    # Record what this plan was derived from, so a later brief edit is detectable
    # instead of silently leaving every layer built to a spec that no longer exists.
    from .provenance import stamp
    used = args.model if args.single else f"{args.draft_model}→{args.verify_model}"
    log(f"provenance → {stamp(args.folder, model=used, note='tag=' + str(args.tag))}")


if __name__ == "__main__":
    main()
