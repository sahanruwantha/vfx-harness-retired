"""Approach review — the escalation a professional makes when tuning stops working.

The build loop could only ever tune. On a plateau it gave up:

    if verdict["mean"] <= prev_mean:
        log("no gain over last round — stopping revisions"); break

That is precisely the moment a craftsperson stops adjusting values and changes TECHNIQUE.
barrel_roll layer G scored 2.83 twice, from two independent builds, with city_texture
pinned at 2 in every round of ~30 — nobody ever asked whether instanced boxes with a
regular window grid was the wrong way to build a city. It was. The answer existed
(night-city-field), and no amount of emission tuning could reach it.

So a plateau now escalates to a REVIEWER: a separate agent, deliberately not the builder,
because the builder is anchored on choices it already defended. It sees the render, the
reference, the stuck axes and the script, and answers one question — is the METHOD wrong,
and what should replace it? Its verdict is fed back as the next revision instruction.
"""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from vfx_harness.agents.model_stream import with_idle_deadline
from vfx_harness.agents.sdk_options import sdk_options
from vfx_harness.infrastructure.config import DEFAULT_EXECUTION_MODEL, Settings
from vfx_harness.infrastructure.sandbox import sandbox_hooks
from vfx_harness.knowledge.recipes import RECIPES_DIR, recipe_index, search_recipes
from vfx_harness.observability import costlog
from vfx_harness.observability.log import log, log_message

REVIEWER_MODEL = DEFAULT_EXECUTION_MODEL


def reviewer_model() -> str:
    return Settings.from_environment(load_dotenv_file=False).reviewer_model

REVIEWER_SYSTEM = """\
You are a VFX supervisor doing an APPROACH REVIEW. A build has stopped improving: two
rounds of tuning produced no gain on the axes it owns.

Your job is NOT to suggest better parameter values. Assume every value has already been
tuned — because it has. Your job is to decide whether the TECHNIQUE is capable of
reaching the reference at all, and if not, to name the one that is.

How to think:
- Look at what the reference does that the render structurally cannot. "Too dim" is a
  value. "Made of identical instanced boxes when the reference is an irregular field with
  per-building colour variation" is a technique.
- A technique is wrong when no setting of its parameters reaches the target. Say so
  plainly, and say what the replacement is.
- If the technique IS right and it is genuinely a values problem, say KEEP and say which
  single value matters most. Do not invent a rewrite to look useful — a needless rewrite
  throws away working work.
- Prefer a cookbook recipe over inventing something. Cite it by name.

Answer in this shape, briefly:
VERDICT: REPLACE | KEEP
WHY: one or two sentences on what the reference does that this approach cannot reach.
DO: the concrete replacement (or the single value, if KEEP). Name recipes to pull.
"""


def _options(shot_folder: Path) -> ClaudeAgentOptions:
    return sdk_options(
        model=reviewer_model(),
        system_prompt=REVIEWER_SYSTEM + "\n\n" + recipe_index(),
        cwd=str(shot_folder),
        allowed_tools=["Read", "Glob"],
        disallowed_tools=["Write", "Edit", "Bash", "Grep", "WebFetch", "WebSearch",
                          "Task", "Agent", "NotebookEdit"],
        hooks=sandbox_hooks(shot_folder, RECIPES_DIR, cwd=shot_folder),
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,
        setting_sources=[],
        max_turns=8,
        effort="high",
    )


def _stuck_axes(verdict: dict, owns: tuple) -> list[str]:
    scores = {k: v for k, v in verdict.get("scores", {}).items()
              if isinstance(v, (int, float))}
    owned = [k for k in scores if not owns or k in owns]
    return sorted(owned, key=lambda k: scores[k])[:3]


async def review(shot, layer, render_rel: str, verdict: dict, script_rel: str,
                 metric_report: str = "", verbose: bool = True) -> dict:
    """-> {"replace": bool, "text": str}. Empty text means the review was unavailable."""
    stuck = _stuck_axes(verdict, tuple(layer.owns))
    hints = {h["name"] for a in stuck for h in search_recipes(a.replace("_", " "), k=2)}
    script = shot.folder / script_rel
    sc = verdict.get("scores", {})
    stuck_line = ", ".join(f"{k}={sc.get(k)}" for k in stuck)
    prompt = (
        f"Layer {layer.id} — {layer.title}. It has stopped improving.\n"
        f"Scope: {layer.reads}\n"
        f"Axes it owns: {', '.join(layer.owns) or '(none declared)'}\n"
        f"Stuck lowest: {stuck_line}\n"
        f"Critic's issues: {'; '.join(verdict.get('issues', [])[:3])}\n\n"
        f"{metric_report}\n\n"
        f"Compare the render `{render_rel}` against the reference `{layer.judge_ref}`.\n"
        + (f"The current approach is in `{script_rel}` — read it.\n" if script.is_file() else
           "No script written yet; judge the approach from the render.\n")
        + (f"Possibly relevant recipes: {sorted(hints)}\n" if hints else "")
        + "\nIs the TECHNIQUE wrong, or only the values?")
    text = ""
    try:
        async for m in with_idle_deadline(
            query(prompt=prompt, options=_options(shot.folder)),
            label="approach review",
        ):
            if verbose:
                log_message(m)
            elif isinstance(m, ResultMessage):
                costlog.record(m)
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, TextBlock):
                        text += b.text
    except Exception as e:
        log(f"approach review unavailable: {str(e)[:80]}", 1)
        return {"replace": False, "text": ""}
    replace = "VERDICT: REPLACE" in text.upper()
    log(f"approach review: {'REPLACE the technique' if replace else 'KEEP, tune values'}", 1)
    return {"replace": replace, "text": text.strip()}


def revision_from_review(layer, review_out: dict) -> str:
    """Turn the review into the builder's next instruction."""
    if review_out["replace"]:
        return (
            "STOP TUNING. An approach review found the TECHNIQUE cannot reach the "
            "reference, not that the values are off. Two rounds of adjustment produced "
            "no gain, so do not adjust again — REBUILD this part a different way.\n\n"
            f"{review_out['text']}\n\n"
            "Pull any recipe named above with find_recipe before you start. Remove the "
            "old construction rather than layering on top of it.")
    return ("An approach review says the technique is sound and this is a values "
            f"problem:\n\n{review_out['text']}\n\nMake that change and re-render.")
