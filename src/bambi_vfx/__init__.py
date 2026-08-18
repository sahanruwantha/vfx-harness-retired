"""Clean-slate agent-driven 3D/VFX pipeline, built on the Claude Agent SDK.

Input contract: a shot folder containing `brief.md` (frontmatter + prose spec)
and a `refs/` directory of milestone images. Everything downstream is generated.

Stage 2 — the plan agent — reads the brief and writes a `plan.md` build plan.
Render / critic / build stages come later.

Importing this package has no configuration side effects. Command entry points load the
project's ``.env`` explicitly through :mod:`bambi_vfx.config`.
"""

__all__: list[str] = []
