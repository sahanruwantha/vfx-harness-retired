"""Stage 2.5 — the asset agent.

Builds the bespoke/hero 3D assets a shot needs, sourced from the shot's OWN
references (never text-to-image'd fresh). The agent reads plans/global.md's asset work-list
and the refs/, and for each genuinely-modelled hero asset it picks the reference
view(s) that show it, then calls the `prepare_asset` tool — which isolates the
subject, runs image→3D via Meshy, normalizes to a committed model.glb, and returns a
preview render the agent checks against the reference.

Particles/volumes/FX and anything the build carries procedurally are NOT assets
here — only bespoke geometry that's worth reconstructing.

Usage:
    python -m bambi_vfx.agents.asset_builder <shot-folder>
"""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

import anyio
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)

from ..assets.images import get_image_backend
from ..assets.normalize import prepare_asset
from ..brief import Shot, load_shot
from ..config import DEFAULT_EXECUTION_MODEL, Settings, load_environment
from ..log import log, log_message

# Asset output is frozen upstream and inherited by every layer, so the harness must keep
# its preview/reference gate strong even when a cheaper execution model does the routing.
MODEL = DEFAULT_EXECUTION_MODEL


def asset_model() -> str:
    return Settings.from_environment(load_dotenv_file=False).asset_model

ASSET_SYSTEM = """\
You are the ASSET agent in an automated 3D/VFX bambi_vfx. You build the bespoke,
hero 3D MODELS a shot needs — reconstructed from the shot's OWN reference images,
so they are pixel-faithful to the brief, never invented from a text prompt.

INPUTS in your working directory (the SHOT FOLDER): `plans/global.md` (the dependency map),
`brief.md`, and `refs/` (the reference images — the source of truth). Use paths
RELATIVE to this folder — read `plans/global.md`, `brief.md`, `refs/M4_end.jpg` directly; do
NOT prefix with the repo root.

FINDING THE WORK-LIST: the plan is organised as layers and tickets. An asset is needed
wherever a ticket says to IMPORT one — grep the plan for `bvfx_import_asset(` /
`import_asset(` / `.glb`, plus any explicit assets table. THE NAME IN THE PLAN IS
BINDING: build scripts call `bvfx_import_asset('<name>')` with that exact string, so
create the asset under exactly that name — a mismatch breaks the build (it already
burned one: the plan asked for `sr2_tower`, a build guessed `hero_tower`).

WHAT COUNTS AS AN ASSET: only genuinely-modelled hero/environment geometry that the
plan says to import. Do NOT build particles, volumes/atmosphere, fog, light fields,
the world/sky, or anything the plan builds PROCEDURALLY in bpy (a ticket that
specifies primitives/dimensions/materials is procedural — not your job). If no ticket
imports an asset, say so plainly and stop without building anything.

FOR EACH bespoke asset:
  1. Look at refs/ and choose the CLEAREST view of this asset. If the refs are dark,
     small, or cluttered (night crops etc.), a single clean front view is better than
     several messy ones — the 'regen' isolation redraws it sharp anyway.
  2. Write a concrete `subject` description (form, materials, any sign text, base) so
     the redraw stays faithful, and call `prepare_asset`. It regenerates a clean
     white-bg product shot of the subject, runs image→3D (Meshy), normalizes the mesh
     (base at origin, scaled to height) and returns a PREVIEW render.
  3. Compare the preview to the reference. If the silhouette/proportion is wrong, sharpen
     the `subject` description or pick a cleaner view and retry. Accept only when the
     preview reads convincingly as the reference asset.

Keep it tight: a shot usually has only one or two real hero assets. Report what you
built (name → model path) and what you deliberately skipped as non-modelled.
"""


def _asset_tool(shot: Shot):
    @tool(
        "prepare_asset",
        "Build a committed 3D asset from the shot's reference view. Isolates the "
        "subject (default 'regen': an image→image model redraws it as a clean, sharp, "
        "WHITE-BACKGROUND product shot — essential when the refs are dark/low-res "
        "crops), runs image→3D via Meshy, normalizes to assets/<name>/model.glb, and "
        "returns a 3/4 preview render to check against the reference. Give a good "
        "`subject` description so the redraw stays faithful. Use a single clean front "
        "view. Takes a few minutes; retries transient failures automatically.",
        {"type": "object",
         "properties": {
             "name": {"type": "string", "description": "asset name, e.g. tower"},
             "references": {"type": "array", "items": {"type": "string"},
                            "description": "paths under refs/, 1-4 views of the same object"},
             "subject": {"type": "string",
                         "description": "short description of the asset to reconstruct, e.g. "
                         "'the Silk Road 2.0 skyscraper: a tall dark windowed slab with a lit "
                         "sign at the top and a flared podium base' — used to regenerate a "
                         "clean white-bg view"},
             "isolate": {"type": "string", "enum": ["regen", "cutout", "none"],
                         "description": "regen (default): image→image redraw as a clean white-bg "
                         "product shot (best for dark/low-res refs); cutout: plain bg removal"},
             "backend": {"type": "string", "enum": ["meshy"]},
             "target_height": {"type": "number"},
             "face_limit": {"type": "integer",
                            "description": "cap tris (default 250000; raise/lower if needed)"},
         },
         "required": ["name", "references", "subject"]},
    )
    async def prepare(args):
        refs = [str((shot.folder / r).resolve()) for r in args["references"]]

        def _run():
            return prepare_asset(
                shot, args["name"], references=refs, subject=args.get("subject"),
                isolate=args.get("isolate", "regen"), backend=args.get("backend", "meshy"),
                target_height=float(args.get("target_height", 100.0)),
                face_limit=args.get("face_limit", 250000))  # default cap on hero density

        try:
            meta = await anyio.to_thread.run_sync(_run)
        except Exception as e:  # surface failures to the agent so it can retry
            return {"content": [{"type": "text", "text": f"prepare_asset failed: {e}"}],
                    "is_error": True}

        blocks = [{"type": "text", "text":
                   f"asset '{meta['name']}' → {meta.get('model')} | tris={meta.get('tris')} "
                   f"bbox={meta.get('bbox_dims')} views={len(meta.get('views', []))}"}]
        prev = meta.get("preview")
        if prev and (shot.folder / prev).is_file():
            blocks.append({"type": "text", "text": "preview (3/4 solid render):"})
            blocks.append({"type": "image",
                           "data": base64.standard_b64encode((shot.folder / prev).read_bytes()).decode(),
                           "mimeType": "image/png"})
        elif meta.get("preview_error"):
            blocks.append({"type": "text", "text": f"(preview failed: {meta['preview_error']})"})
        return {"content": blocks}

    server = create_sdk_mcp_server(name="assets", version="0.1.0", tools=[prepare])
    return server, ["mcp__assets__prepare_asset"]


async def build_assets(folder: str | Path, *, verbose: bool = True) -> None:
    shot = load_shot(folder)
    server, names = _asset_tool(shot)
    model = asset_model()
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=ASSET_SYSTEM,
        cwd=str(shot.folder),
        mcp_servers={"assets": server},
        allowed_tools=["Read", "Glob", *names],
        disallowed_tools=["Bash", "Write", "Edit", "WebFetch", "WebSearch"],
        permission_mode="bypassPermissions",
        max_buffer_size=32 * 1024 * 1024,  # renders/read-images can exceed the 1MB default
        setting_sources=[],
        max_turns=40,
        effort="medium",  # asset routing/view-picking — not deep reasoning; keeps it snappy
    )
    kickoff = (
        f"Build the bespoke hero 3D assets for shot '{shot.id}'. Read `plans/global.md` and find "
        f"every ticket that imports an asset (grep `bvfx_import_asset(` / `.glb`) — those "
        f"exact names are your work-list. Read `brief.md`, then look at refs/. For each "
        f"genuinely-modelled asset, pick reference views and call prepare_asset; skip "
        f"anything carried by particles/FX/procedural. Check each preview against its "
        f"reference before accepting."
    )
    log(f"asset agent: shot '{shot.id}', model {model}, effort {options.effort}")
    try:  # refresh image-backend credentials now, not 20 minutes into the run
        get_image_backend().ensure_fresh()
    except Exception as e:
        log(f"warning: image backend not ready ({str(e)[:160]})", 1)
    async with ClaudeSDKClient(options=options) as client:
        await client.query(kickoff)
        async for message in client.receive_response():
            if verbose:
                log_message(message)


def main() -> None:
    load_environment()
    ap = argparse.ArgumentParser(description="Build a shot's bespoke 3D assets from refs.")
    ap.add_argument("folder", help="shot folder (contains brief.md, plans/global.md, refs/)")
    args = ap.parse_args()
    print(f"asset stage: {args.folder}")
    anyio.run(build_assets, args.folder)


if __name__ == "__main__":
    main()
