"""Clean-slate agent-driven 3D/VFX pipeline, built on the Claude Agent SDK.

Input contract: a shot folder containing `brief.md` (frontmatter + prose spec)
and a `refs/` directory of milestone images. Everything downstream is generated.

Stage 2 — the plan agent — reads the brief and writes a `plan.md` build plan.
Render / critic / build stages come later.

Importing the package loads the repo's `.env` (never overriding what's already in the
environment), so `MESHY_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` work from a plain shell
without sourcing anything first.
"""

from pathlib import Path

from dotenv import load_dotenv

# repo-root .env when running from a checkout, else whatever the cwd tree offers
for _candidate in (Path(__file__).resolve().parent.parent / ".env", Path.cwd() / ".env"):
    if _candidate.is_file():
        load_dotenv(_candidate, override=False)  # a real env var always wins
        break

from .brief import Shot, load_shot  # noqa: E402 — must follow the .env load
from .plan_agent import generate_plan  # noqa: E402

__all__ = ["Shot", "load_shot", "generate_plan"]
