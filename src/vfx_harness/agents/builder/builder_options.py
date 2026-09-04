"""SDK options for the live, scene-mutating builder session."""

from __future__ import annotations

from claude_agent_sdk import ClaudeAgentOptions

from vfx_harness.agents.build_prompts import builder_system
from vfx_harness.agents.builder.models import (
    MAX_BUDGET_USD,
    MAX_TURNS,
    TASK_BUDGET_TOKENS,
    builder_model,
)
from vfx_harness.agents.guardrails import builder_hooks
from vfx_harness.agents.sdk_options import sdk_options
from vfx_harness.domain.brief import Shot
from vfx_harness.knowledge.recipes import RECIPES_DIR, recipe_index
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority


def _builder_options(
    shot: Shot,
    mcp_servers: dict,
    tool_names: list[str],
    axes: list[tuple[str, str]],
    ref_rel: str | None = None,
    script_rel: str | None = None,
    phase: dict[str, str] | None = None,
    ticket_context: str | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
    attempt_guard=None,
) -> ClaudeAgentOptions:
    return sdk_options(
        model=builder_model(),
        system_prompt=builder_system(
            axes,
            recipe_index(context=ticket_context) if ticket_context is not None else recipe_index(),
            ticket_context=ticket_context,
        ),
        cwd=str(shot.folder),
        hooks=builder_hooks(
            shot.folder,
            [shot.folder, RECIPES_DIR],
            ref_rel=ref_rel,
            script_rel=script_rel,
            phase=phase,
            selected_authority=selected_authority,
            attempt_guard=attempt_guard,
        ),
        mcp_servers=mcp_servers,
        # LIVE_BUILD owns the warm Blender scene, never the artifact on disk. Write/Edit
        # are absent rather than merely prompt-discouraged; publication and repair use
        # dedicated sessions below with mutually exclusive mutation surfaces.
        allowed_tools=["Read", "Glob", "Grep", "WebFetch", *tool_names],
        # allowed_tools is an AUTO-APPROVE list, not a whitelist: under bypassPermissions
        # every unlisted tool still runs. Deny explicitly or it is available.
        # WebFetch is allowed but hook-restricted to Blender docs (see guardrails):
        # with no lookup at all the builder re-guesses a failing API verbatim.
        disallowed_tools=[
            "Write",
            "Edit",
            "Bash",
            "Task",
            "Agent",
            "NotebookEdit",
            "KillShell",
            "BashOutput",
        ],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # renders/read-images can exceed the 1MB default
        # "project" loads shots/<id>/CLAUDE.md on EVERY request, so the layer contract
        # survives compaction — the kickoff message does not.
        setting_sources=["project"],
        max_turns=MAX_TURNS,  # headroom only — MAX_BUDGET_USD is the real stop
        max_budget_usd=MAX_BUDGET_USD,
        # Three different jobs, and they are not substitutes:
        #   task_budget      ADVISORY — the model sees the countdown and can reserve room
        #                    to finish and validate instead of being cut off mid-thought
        #   max_budget_usd   ENFORCED financial ceiling
        #   max_turns        runaway-loop backstop
        # Advisory pacing does not fix a loop that cannot converge — layer 2 burned 46
        # rounds and layer 5 sixteen because nothing could FAIL them on the axis that
        # mattered, and a countdown would only have stopped them sooner with less to show.
        # It is here because being cut off mid-script is strictly worse than landing early.
        **({"task_budget": TASK_BUDGET_TOKENS} if TASK_BUDGET_TOKENS else {}),
        effort="high",
    )
