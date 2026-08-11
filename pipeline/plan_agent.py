"""Stage 2 — the plan agent.

Runs a Claude Agent SDK agent inside the shot folder. The agent reads brief.md
and the reference images with its Read tool, reasons about the shot, and writes a
single `plan.md` build plan with its Write tool. Output is that markdown file.

Usage:
    python -m pipeline.plan_agent <shot-folder>
    plan <shot-folder>            # console script
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from .brief import load_shot
from .prompts import PLANNER_SYSTEM, planner_user_prompt

MODEL = "claude-fable-5"


async def generate_plan(folder: str | Path, *, verbose: bool = True) -> Path:
    shot = load_shot(folder)
    plan_path = shot.folder / "plan.md"

    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=PLANNER_SYSTEM,
        cwd=str(shot.folder),
        allowed_tools=["Read", "Glob", "Write"],
        disallowed_tools=["Edit", "Bash", "WebFetch", "WebSearch"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # renders/read-images can exceed the 1MB default
        setting_sources=[],  # isolate from user/project CLAUDE.md and settings
        max_turns=24,
        effort="high",
    )

    async for message in query(prompt=planner_user_prompt(shot), options=options):
        if not verbose:
            continue
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    print(f"  · {block.text.strip()[:200]}")
                elif isinstance(block, ToolUseBlock):
                    print(f"  → {block.name}")
        elif isinstance(message, ResultMessage):
            cost = getattr(message, "total_cost_usd", None)
            if cost is not None:
                print(f"  (session cost ${cost:.4f})")

    if not plan_path.is_file():
        raise RuntimeError(f"agent finished without writing {plan_path.name}")
    return plan_path


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m pipeline.plan_agent <shot-folder>", file=sys.stderr)
        raise SystemExit(2)

    folder = sys.argv[1]
    print(f"planning shot: {folder}")
    plan_path = anyio.run(generate_plan, folder)
    print(f"\nwrote {plan_path}")


if __name__ == "__main__":
    main()
