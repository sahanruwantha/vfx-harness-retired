# bambi-vfx

An **agent-driven 3D/VFX pipeline**. An LLM technical artist ("the desk") builds one cinematic shot
in a live, headless Blender through real tools — probe the API, run bpy, look, revise — and the shot
moves through a **department pipeline** exactly as a real studio does, each stage gated by its own
domain-expert critic.

```
modeling / layout  →  look-dev  →  [ FX / sim ]  →  lighting  →  comp
   (layout critic)   (lookdev)      (fx critic)     (lighting)   (craft)
```

Each department is a fresh desk over a **persistent, versioned `.blend`**: it opens the prior stage's
published scene, adds only its own layer, and is signed off on its own terms before the next stage
starts — so a lighting fix can never clobber approved modeling.

## How it works

- **The desk** (`agents/scene_builder.py`, `agents/blender_tools.py`) — an LLM artist with live Blender
  tools: `run_bpy`, `introspect`, `scene_graph` (the free "viewport"), `viewport_snapshot`, `render`.
  It works to a *viewport → dailies* doctrine: sense structure with the free symbolic channel, build in
  coherent passes, and spend a small **look budget** at checkpoints — not a render after every edit.
- **Departments** (`scene/departments.py`) — the sequential stages above over a published `.blend`,
  each with its own critic (`agents/dept_critics.py`, `agents/art_director.py`).
- **The supervisor** (`agents/scene_supervisor.py`, `scene/supervisor.py`) — decides a shot's
  methodology (3D / plate / hybrid) and breaks it into **per-element** tasks; an `fx`-tagged element
  turns the FX department on for that shot.
- **Asset sourcing** (`scene/assets.py`) — the modeling desk can `acquire_asset(...)`: generate an
  image (Higgsfield) → 3D mesh (Tripo) → cached GLB → imported, instead of sculpting primitives.
- **Motion** (`scene/animate.py`, `scene/anim_departments.py`) — the same idea for moving shots.
- **Shared contracts** (`develop/`, `footage/`) — a slim set of data types the pipeline builds
  against (`Clip`, `BeatEntry`, `Verdict`, `FrameSample`, the `Render3D` leaf type). Decoupled from
  any documentary/research back-half — this repo is the 3D pipeline only.

## Quickstart

Requires **Blender 5.x** on `PATH` (headless) and Python ≥ 3.10.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest                 # the full unit suite (no Blender needed — fakes)
python -m scene        # Phase-0 smoke: launch the bridge, build + render a gray-box, headless
```

Asset generation is optional — copy `.env.example` to `.env` and fill in the Tripo/Higgsfield keys to
enable `acquire_asset`. Without them the pipeline degrades to procedural geometry.

## Shot briefs

A shot is specified by a structured markdown **brief** plus a **reference board** of milestone images
(set angles + each beat's first frame). See [`shots/barrel_roll/`](shots/barrel_roll/) for the format:
`brief.md` (frontmatter + description + `## Milestones` + `## Beats`) alongside `refs/` (the master
reference and the isolated milestone frames the desks build to and the critics judge against).

## Layout

```
src/scene/     the Blender bridge + department pipeline + realizers
src/agents/    the desk, its tools, and the per-department critics
src/develop/   slim shared contracts (ledger, verdict, leaf type aliases)
src/footage/   FrameSample + FootageCandidate
tests/         the unit suite (Blender/SDK faked)
shots/         shot briefs + reference boards
```
