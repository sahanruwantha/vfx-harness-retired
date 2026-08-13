"""Confine an agent's filesystem tools to the directories it actually needs.

`cwd` is a default, not a jail, and `allowed_tools` only AUTO-APPROVES — under
`permission_mode="bypassPermissions"` every other tool still runs. So an agent given
["Read", "Write", "Glob"] can still call Grep, and nothing stops any of them walking
out of the shot folder.

That is not hypothetical: during a barrel_roll build both the BUILD agent and the
DISTILL agent grepped `~/.claude/projects/<session>.jsonl` — the orchestrating
conversation — to research their own error messages. They ingested the supervisor's
running commentary about them and burned turns reading megabytes of JSONL.

`path_sandbox(*roots)` returns a PreToolUse hook that denies any file-touching tool
whose path argument resolves outside `roots`. Hooks run in our process, before the tool
executes, and cost no context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

from .log import log

# tool name -> the arg(s) that carry a path
_PATH_ARGS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path",),
    "Grep": ("path",),
    "LSP": ("path",),
}


def _resolve(p: str, cwd: Path) -> Path:
    q = Path(p).expanduser()
    return (q if q.is_absolute() else cwd / q).resolve()


def path_sandbox(*roots: str | Path, cwd: str | Path | None = None) -> HookMatcher:
    """PreToolUse hook denying file tools that reach outside `roots`."""
    allowed = [Path(r).resolve() for r in roots]
    base = Path(cwd).resolve() if cwd else allowed[0]

    async def _check(inp: Any, tool_use_id: str | None, ctx: Any) -> dict:
        tool = inp.get("tool_name") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        args = (inp.get("tool_input") if isinstance(inp, dict)
                else getattr(inp, "tool_input", {})) or {}
        for key in _PATH_ARGS.get(tool, ()):
            raw = args.get(key)
            if not raw:
                continue  # omitted path means "cwd", which is inside the sandbox
            target = _resolve(str(raw), base)
            if any(target == a or a in target.parents for a in allowed):
                continue
            where = ", ".join(str(a) for a in allowed)
            log(f"⛔ sandbox: {tool} denied on {target} (outside {where})", 1)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"{target} is outside this agent's working directories. You may only "
                    f"read or write under: {where}. Everything you need for this task is "
                    f"there — do not look for context elsewhere on the filesystem."),
            }}
        return {}

    return HookMatcher(matcher=None, hooks=[_check])


def sandbox_hooks(*roots: str | Path, cwd: str | Path | None = None) -> dict:
    """Ready-to-pass `hooks=` value for ClaudeAgentOptions."""
    return {"PreToolUse": [path_sandbox(*roots, cwd=cwd)]}
