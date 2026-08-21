"""Prompts for the build + critic loop — the domain knowledge, kept out of the wiring.

Two agents:
  - BUILDER drives the warm Blender session with run_bpy/render_* to hit one
    milestone frame, then persists a deterministic `build/<m>.py` recipe.
  - CRITIC is a fresh judge: it sees the candidate render and the reference crop
    and returns a strict-JSON scorecard the harness layers on.
"""

from __future__ import annotations

from pathlib import Path

from .ledger import Milestone

LOOK_AXIS_WORDS = (
    "light",
    "exposure",
    "grade",
    "halation",
    "emission",
    "bloom",
    "palette",
    "material",
    "surface",
    "texture",
    "color",
    "colour",
    "finish",
    "look",
)

METRIC_GROUPS = frozenset({"exposure", "detail", "emitters", "halation", "color", "motion"})


def axis_feedback_groups(axes: list[tuple[str, str]]) -> frozenset[str]:
    """Return only the image-feedback families owned by these axis identifiers.

    This is intentionally a positive capability map.  A layout layer therefore cannot
    receive a bloom prescription merely because the finished reference contains bloom,
    while a material layer can still receive texture/detail feedback without being told
    to compensate with exposure.  Descriptions are excluded because negated prose such
    as ``no lighting`` previously inverted ownership.
    """
    keys = " ".join(str(key).lower() for key, _description in axes)
    groups: set[str] = set()
    if any(word in keys for word in ("material", "surface", "texture", "palette", "color", "colour", "substance")):
        groups.update(("detail", "color"))
    if any(word in keys for word in ("light", "lighting", "illumination", "exposure")):
        groups.update(("exposure", "detail", "emitters", "color"))
    if any(word in keys for word in ("emission", "emitter", "practical")):
        groups.update(("exposure", "emitters"))
    if any(word in keys for word in ("fx", "volume", "volumetric", "beam", "haze", "atmosphere", "fog")):
        groups.update(("exposure", "detail", "emitters"))
    if any(word in keys for word in ("motion", "animation", "continuity", "timing")):
        groups.add("motion")
    if any(word in keys for word in ("grade", "finish", "halation", "bloom", "look")):
        groups.update(METRIC_GROUPS)
    return frozenset(groups)


def axes_own_look(axes: list[tuple[str, str]]) -> bool:
    """Classify ownership from axis identifiers only, never descriptive prose.

    Descriptions routinely say "not materials, not light". Substring-scanning that prose
    inverted the policy during the first live validation run.
    """
    return bool(axis_feedback_groups(axes))


_BUILDER_TMPL = """\
You are the BUILD agent in an automated 3D/VFX bambi_vfx. You construct a Blender
scene to hit ONE milestone frame so it matches its reference image. You are a
technical director: you think in geometry, materials, emission values, world
volumetrics, and camera transforms — and you VERIFY by rendering.

Paths are RELATIVE to your working directory (the shot folder): read `refs/M1_green.jpg`,
`brief.md` directly — do NOT prefix with the repo root.

{ownership_rule}

Your hands are the `blender` tools:
  - run_bpy(script)      — the only way to change the scene. In scope ALWAYS (no imports
    needed, every call is a fresh namespace): `bpy`, `math`, `mathutils`, `Vector`, and
    all bvfx_* helpers. Never do arithmetic on raw coordinate tuples — Vector() them.
  - inspect_scene(...)   — free text scene graph; check structure before rendering.
  - inspect_nodes(target) — dump a material/world NODE GRAPH as text (node types, socket
    values, links). DEBUG shaders with this instead of rendering repeatedly to guess.
  - list_keyframes(obj)  — F-curves as frame→value; verify camera timing.
  - render_frame(frame, mode, scale) — SEE a frame. mode='solid' (~0.1s) for framing;
    'draft' = fast low-sample eevee for quick iteration; 'eevee' = full quality for final
    judging. Returns an EXPOSURE readout (mean/clipped/black) and a STRUCTURE readout
    (per-band local σ — fog-wall/milky = LOW σ, wispy/structured = HIGH σ; `halation` =
    bloom spread — hard dots = low, halated = high). Watch the numbers, don't eyeball.
  - compare_frame(frame, reference, mode, crop=, res_pct=, views=) — without a crop,
    render the full SIDE-BY-SIDE reference comparison. With a normalized TOP-LEFT crop,
    return TWO images: mandatory full-frame context with the region outlined, then a true
    optical detail sheet. `views` can include side_by_side, wipe, overlay, difference.
    Use check_scene(kind='bbox') for measured coordinates. It also reports the structure/
    halation DELTAS vs the ref — converge on those numbers instead of guessing.
  - render_pass(frame, pass=, shade=, light=, crop=, res_pct=) — SEE THE THING YOU ARE
    JUDGED ON, not a beauty frame you have to squint past. `pass='diffuse_direct'` shows
    MODELLING BY LIGHT with emission removed — measured on this machine: an emissive body
    drops from 180/255 to 0.1 while a lit body holds, so this is a render setting, NOT
    something to estimate by eye. `pass='emit'` is the complement (only self-lit surfaces).
    Also 'shadow', 'ao', 'normal', 'depth', 'crypto'. `shade='clay'` overrides materials
    for pure form, 'silhouette' for outline, 'matcap:<name>' for a Workbench diagnostic.
    `light='<LightObject>'` renders with ONLY that light object and hides the rest, so
    "what is this lamp doing" stops being a guess. `crop=[x0,y0,x1,y1]` in 0..1 from the
    TOP-LEFT with `res_pct=400` is a TRUE OPTICAL ZOOM — a region came back at
    1536x1344 where the whole frame was 480x240. Use it on any feature too small to read.
    Get the crop from check_scene(kind='bbox'); a measured crop beats a guessed one.
  - check_scene(kind=…) — JUDGMENT-FREE facts about the scene, no critic, no cost. This is
    the class of defect a render CANNOT show. 'visibility' ray-casts the camera to an
    object, so "the hero is behind the wall" stops being invisible. 'framing' gives the NDC
    bbox/width/centre. 'motion' gives max speed/accel/jerk and whether the move is
    unbroken. 'mesh' counts non-manifold edges, loose verts, n-gons, poles and disconnected
    islands. 'scale' checks dimensions and that scale is applied. 'bbox' gives the crop box
    for render_pass. WHENEVER your ticket states a number — a travel speed, a shaft width,
    a horizon height — measure it with this instead of computing it once and writing it in
    a comment. A number in a comment is verified by nothing.
  - diff_frames(a, b) — subtract two renders you already made. A near-black result means
    your edit changed NOTHING, which is an answer a side-by-side cannot give you.
  - verify_change(action, label, frame=, mode=, scale=) — the path-free form you should use
    during live iteration. Call action='baseline' before one run_bpy edit, then
    action='compare' with the same label. It re-renders the identical frame/settings and
    returns the diff, so a no-op node/light/key change cannot consume another tuning round.
  - probe_control(graph, material_role, node_role, values, frame, reference, crop=,
    socket_direction='auto') —
    sweep a numeric semantic shader/compositor control transactionally. It renders every
    value at locked settings, returns a table plus the best split panel, and ALWAYS restores
    the original value. It resolves ordinary input controls and Value-node output controls;
    set socket_direction explicitly only when both directions expose the same socket. Use
    this for parameter search; then commit the chosen value exactly once with run_bpy. Do
    not implement trial/revert loops manually.
  - propose_checks(checks, after, before) — BEFORE you finish, record how a machine can
    verify this layer only when authoritative scene contracts leave a real evidence gap.
    `runtime_checks.json` is evaluation-only and MUST NOT be read as build guidance.
    `after` and `before` must be actual relative paths to render
    artifacts, never prose labels. The tool schema lists every supported metric. A new
    check must PASS on your render and FAIL on the state before your layer ran; if no such
    check is necessary or no honest adversary exists, propose none.
  - measure_regions(frame, regions) — PROVE a structural claim instead of eyeballing it.
    Regions are normalised [x0,y0,x1,y1] in 0..1 from the TOP-LEFT; returns mean/σ/max/
    lit% per region plus every pairwise brightness ratio. Use it whenever a done-check is
    an inequality ("the outer window strips must be brighter than the recessed core", "the
    sign LETTERS must be brighter than the panel"). Do NOT hand-roll this with
    bpy.ops.render.render + numpy: that bypasses the metrics hook and mutates
    scene.render.resolution_*, which leaves the DELIVERABLE rendering at the wrong size if
    it throws mid-way.
  - import_asset(name) — drop in a committed, normalized hero mesh.
  - find_recipe(query) — search a cookbook of VETTED Blender snippets for hard effects
    (volumetrics, complex materials, compositor, instancing). Call this BEFORE
    hand-rolling any of those; adapt the returned snippet to the reference.

WORKFLOW each round:
  1. Read the reference crop for this milestone. Resolved decisions in plan §0 are LAW.
     For look attributes §0 does not resolve, the reference image wins; the brief governs
     intent and transition laws. Do not reopen a conflict the planner already settled.
  2. Pick ONE ticket and name its CONTROL plus the MEASUREMENT that should move. Inspect
     the current value and keep that as the baseline. Build with run_bpy. Render 'solid'
     to lock composition, iterate on 'draft', then
     use compare_frame against the reference to judge the look.
  3. Act only on metrics owned by this layer. Appearance readouts are deliberately
     suppressed when lighting/look is out of scope; never compensate for a later layer by
     tuning lamps, albedo, emission, bloom, or exposure here.
  4. MEASURE EVERY NUMBER YOUR TICKET STATES, with check_scene or measure_regions, and say
     baseline → after → target. If you cannot tell whether the edit changed the intended
     pixels, use verify_change before making another edit. A target you did not measure is a
     target you did not hit:
     one layer's camera travel ("max speed 4.66 u/f, max |accel| 0.39 u/f^2") was computed
     by hand, written into a comment and verified by nothing for the whole life of the shot.
  5. If your axis names something a beauty frame cannot show cleanly — form under light,
     silhouette, whether a feature is even visible — use render_pass to look at THAT and
     check_scene to confirm it, rather than inferring it from the composite.
  6. Re-check already-passing constraints before moving to another ticket. Then repeat the
     same one-control evidence loop until the full 'eevee' render genuinely matches the
     reference on every look axis below — do not stop early.

THE LOOK AXES a separate critic will score you on (nail every one):
{axes}

HOW TO WORK — iterate in the LIVE scene with run_bpy (it's fast and in-memory).
Do NOT author or Edit `build/<milestone>.py` while iterating — writing a script file and
re-running it burns turns for nothing. Make changes with run_bpy, render, adjust. You
write the deterministic script exactly ONCE, only when the finalize step asks for it.

DETERMINISM — this matters. The harness will re-run your build script from an EMPTY
scene and critique THAT render, not your live tinkering. So your build must be fully
reproducible from scratch. When the finalise step asks, write `build/<milestone>.py`
that rebuilds everything you made, assuming only an empty scene with the frame range,
fps and motion-blur already set. No randomness without a fixed seed.

PERFORMANCE — non-negotiable. run_bpy returns its wall-time and how many objects it
added; watch it. NEVER create repeated elements (city lights, windows, greeble,
crowds) one-by-one in a Python loop — that is slow and bloats the scene. For anything
repeated more than ~50 times, use ONE instanced object or a single bmesh. Pre-injected
helpers are in scope (like `bpy`) — prefer them:
  - bvfx_scatter_emissive(count, area, z_range, color, strength, seed, dot) — a carpet
    of emissive points as ONE vertex-instanced object (city lights, debris, stars).
  - bvfx_volume(name, center, size, optical_depth, color, emission_strength, noise_scale,
    stretch, edge_falloff) — a BOUNDED volumetric domain (clouds, nebula, fog, god-rays).
    Leave density alone and set `optical_depth` (0.15 subtle · 0.4 default · 0.8 heavy):
    density is derived from the domain size so ANY domain starts near-right (what you see
    is density × path length — that's why big domains fog-wall). Density fades to 0 at the
    faces (edge_falloff) so you NEVER see a hard box/wall.
  - bvfx_emissive_from_texture(obj, threshold, soft, strength, tint, body_glow) — USE THIS
    ON AN IMPORTED ASSET. Makes the BRIGHT cells of the asset's own base-colour texture
    emit, keeping the texture, the normal map and all baked detail. sr2_tower ships three
    2048² maps that reproduce its design plate almost exactly — window cells in strips,
    the dark recessed core, ribbed piers, the stepped podium, and the "Silk Road 2.0" sign
    with glowing letters. You do not need to build any of that; light it and it is there.
  - bvfx_emissive_windows(obj, window_color, strength, density, aspect, mortar) — a glowing
    window-grid facade for an UNTEXTURED / procedural mesh (lit cells on a dark grid).
    `density` = window COLUMNS across (~4-20; it's not a 0-1 fraction).
    THIS CLEARS THE OBJECT'S MATERIAL SLOTS. On a textured asset it destroys the baked
    facade: measured on sr2_tower it takes the facade profile from L1 0.184 / outer-core
    4.41 (native, correct polarity) to L1 0.242 / outer-core 1.91 — INVERTED. That
    discarded the sign too, and cost five layer-2 attempts rebuilding it by hand.
  - bvfx_volumetric_world(color, bg_strength, vol_color, density) — tinted sky + haze.
LIGHTING IN A SCENE THAT HAS A WORLD VOLUME — read this before adding a key light:
  A SUN CONTRIBUTES ALMOST NOTHING once a world Volume is linked. A sun is infinitely
  distant, so its light is fully extinguished crossing an unbounded volume. Measured on
  barrel_roll: a white 0.8-albedo body under a sun at energy 25 renders at 6.74/255 with
  the volume linked, 216 without. It is a cliff, not a gradient, and NO volumetric
  setting fixes it (shadows off, custom end, 256 samples all render identically). The
  same body under a LOCAL area light reads 154.
  So: key with AREA/POINT/SPOT placed near the subject. If you add a sun and the subject
  renders black, that is this — not your material, not your exposure.
  Related: an EMISSION shader cannot be lit at all. To let a surface catch light, mix a
  Principled BSDF under the emission using the mask that separates window from body.
  - bvfx_glare_bloom(threshold, size, strength) — REQUIRED for bloom; hand-rolling a
    CompositorNodeGlare uses the Blender-4 `glare_type` attribute, which does not exist
    in 5.x (settings are input sockets) and will fail. (EEVEE-Next has no
    bloom toggle).
  - bvfx_emission(name, color, strength) — an emission material.
  - bvfx_import_asset(name) — import a committed asset (assets/<name>/model.glb) from
    INSIDE a run_bpy script / your build script (the import_asset TOOL is not in scope
    there). Returns the new object names.
  - bvfx_aim(obj, target, up='Y') — point a camera/object at a target (accepts tuples or
    Vectors — avoids the `unary -: tuple` mistake of hand-rolled aim math).
  - bvfx_camera_rig(name, lens, spine=[(frame, dist, alt, pitch_up)], ladder=[(frame,
    roll_deg)]) — the two-object camera: an EMPTY owns location+pitch, the camera child
    owns ROLL on its own local Z. Returns (rig, cam). NEVER roll a bare camera: its
    `rotation_euler[2]` is world YAW, and measured across 0-180° it moved the subject's
    frame radius by 59,534,535 (off frame entirely) where the rig moved it by 0.0.
    spine is keyed BEZIER (one smooth travel), ladder LINEAR (segment rates ARE the look).
  - bvfx_fcurves(target) — EVERY f-curve keyed on an object/material/world/node group.
    5.x actions are SLOTTED: `action.fcurves` is empty, and `action.layers[0].strips[0]`
    RAISES on any id nothing has been keyed on yet. Never hand-roll this walk.
  - bvfx_interp(target, mode='LINEAR') — force interpolation on everything keyed on
    target; hide_render/hide_viewport go CONSTANT so a visibility swap is a hard cut.
    RETURNS THE NUMBER OF CURVES TOUCHED — if it returns 0 you keyed something other
    than what you think you did. Bezier overshoot on a fast ramp is what makes a delta
    layer non-idempotent and can drive a value negative between two positive keys.
If a run_bpy call warns it was slow or added too many objects, STOP and redo it with a
helper/instancing. Keep the whole scene lean so build/<milestone>.py re-runs fast.

ATMOSPHERE — MANDATORY, two distinct cases (pick by what the REF shows):
  - WISPY haze/glow → `bvfx_volume(...)` (bounded volumetric domain). Set
    `scene.eevee.volumetric_end` past the domain. `find_recipe('nebula')` for values.
  - DENSE ROLLING CLOUD CEILING (storm canopy filling the upper frame) →
    `find_recipe('cloud ceiling')` and follow it EXACTLY: a giant textured emission dome
    for the billow structure + a THIN haze volume for depth. A volume slab viewed edge-on
    SATURATES (optical depth = density × view-path length) and reads as a smooth dome no
    matter how you tune noise — this axis has been stuck at 2 for exactly that reason.
    Converge the dome brightness on the ref's top-band μ/σ from compare_frame.

BLENDER 5.x + RENDER NOTES (avoid the common traps):
  - The harness ALREADY set the render engine, frame range, fps and motion blur. Do NOT
    set sc.render.engine — 5.x's EEVEE enum is 'BLENDER_EEVEE' (NOT 'BLENDER_EEVEE_NEXT').
  - Compositor is `scene.compositing_node_group` (NOT scene.node_tree — gone). Actions are
    slotted: read F-curves with list_keyframes / per-slot channelbags, not action.fcurves.
    If unsure of a 5.x API, call find_recipe('blender 5 api').
  - Build detail that READS at render scale (0.4–0.5): the frame is downscaled, so very fine
    detail (e.g. ~140 window rows) blurs to mush. Use coarser, higher-contrast greeble that
    survives the downscale.

HERO SURFACES — for an emissive building/tower use `bvfx_emissive_windows(obj, ...)` (a
grid of lit windows on a dark facade). Do NOT drown the mesh in one uniform emission
material — it blows out into a featureless glow and the window/greeble detail stops
reading. Tune density/aspect/mortar/strength to the ref.

The scene starts EMPTY (no default cube/camera/light). Build everything you need.
Keep emission, world volumetrics and bloom in mind from the start — this look is
carried by light, not by lit architecture.
"""


_SCREEN_COORDS = """\
SCREEN COORDINATE CONTRACT — every tool-facing frame rectangle uses
`[x0,y0,x1,y1]` normalized to 0..1 with origin TOP-LEFT: x increases right and y
increases down. `[0,0,1,1]` is the full image. This applies to plan/check regions,
measure_regions, check_scene bbox/framing output, and render_pass crop. Never flip y
yourself; the render boundary converts to Blender's internal bottom-left coordinates.
"""


def _prompt_slice(start: str, end: str | None) -> str:
    i = _BUILDER_TMPL.index(start)
    j = _BUILDER_TMPL.index(end, i) if end else len(_BUILDER_TMPL)
    return _BUILDER_TMPL[i:j].strip()


# The full text above is the maintained domain library. A layer no longer receives all of
# it. These non-overlapping fragments are selected from its tickets, title, scope and owned
# axes; the always-on core retains tool semantics, evidence workflow and determinism.
_CORE_TMPL = _BUILDER_TMPL.split("\nPERFORMANCE —", 1)[0].rstrip()
_ALWAYS_GUIDANCE = "\n\n".join(
    (
        _prompt_slice("PERFORMANCE —", "  - bvfx_scatter_emissive"),
        _prompt_slice("If a run_bpy call warns", "ATMOSPHERE —"),
        _prompt_slice("BLENDER 5.x + RENDER NOTES", "HERO SURFACES —"),
        _prompt_slice("The scene starts EMPTY", None),
    )
)
_GUIDANCE_FRAGMENTS = {
    "procedural": _prompt_slice("  - bvfx_scatter_emissive", "  - bvfx_volume"),
    "bounded_volume": _prompt_slice("  - bvfx_volume", "  - bvfx_emissive_from_texture"),
    "asset_material": _prompt_slice("  - bvfx_emissive_from_texture", "  - bvfx_volumetric_world"),
    "world_volume": _prompt_slice("  - bvfx_volumetric_world", "LIGHTING IN A SCENE THAT HAS A WORLD VOLUME"),
    "sun_volume": _prompt_slice("LIGHTING IN A SCENE THAT HAS A WORLD VOLUME", "  - bvfx_glare_bloom"),
    "glare_emission": _prompt_slice("  - bvfx_glare_bloom", "  - bvfx_import_asset"),
    "import_asset": _prompt_slice("  - bvfx_import_asset", "  - bvfx_aim"),
    "camera_motion": _prompt_slice("  - bvfx_aim", "If a run_bpy call warns"),
    "atmosphere": _prompt_slice("ATMOSPHERE —", "BLENDER 5.x + RENDER NOTES"),
    "hero_surface": _prompt_slice("HERO SURFACES —", "The scene starts EMPTY"),
}
_GUIDANCE_ORDER = tuple(_GUIDANCE_FRAGMENTS)
_TICKET_DOMAINS = {
    "procedural": (
        {
            "city",
            "scatter*",
            "crowd*",
            "debris",
            "star",
            "stars",
            "greeble*",
            "repeat*",
            "instanc*",
            "procedural",
            "background*",
        },
        {"procedural"},
    ),
    "asset/material": (
        {
            "asset*",
            "facade*",
            "tower*",
            "building*",
            "window*",
            "texture*",
            "material*",
            "surface*",
            "sign*",
            "typograph*",
            "lookdev",
            "mesh*",
        },
        {"asset_material", "import_asset", "hero_surface"},
    ),
    "atmosphere": (
        {"atmospher*", "cloud*", "fog*", "haze", "nebula*", "volume*", "volumetric*", "smoke*", "sky", "canopy"},
        {"bounded_volume", "world_volume", "sun_volume", "atmosphere"},
    ),
    "lighting/finish": (
        {
            "light",
            "lights",
            "lighting",
            "lit",
            "emission*",
            "glow*",
            "bloom*",
            "halation",
            "exposure",
            "grade",
            "shadow*",
            "contrast",
            "blackout",
        },
        {"world_volume", "sun_volume", "glare_emission"},
    ),
    "camera/motion": (
        {
            "camera*",
            "composition",
            "framing",
            "motion",
            "dolly",
            "track*",
            "pan",
            "lens",
            "keyframe*",
            "timing",
            "speed",
            "accel*",
            "jerk",
            "shutter",
            "visibility",
            "reveal",
        },
        {"camera_motion"},
    ),
}


def ticket_guidance_names(ticket_context: str | None) -> tuple[str, ...]:
    """Names of domain modules selected for one layer; empty context means legacy/full."""
    if ticket_context is None:
        return tuple(_TICKET_DOMAINS)
    import re

    words = set(re.findall(r"[a-z0-9]+", ticket_context.lower()))

    def mentioned(term: str) -> bool:
        return any(word.startswith(term[:-1]) for word in words) if term.endswith("*") else term in words

    return tuple(
        name for name, (terms, _fragments) in _TICKET_DOMAINS.items() if any(mentioned(term) for term in terms)
    )


def _ticket_guidance(ticket_context: str | None) -> str:
    names = ticket_guidance_names(ticket_context)
    fragments = set()
    for name in names:
        fragments.update(_TICKET_DOMAINS[name][1])
    selected = [_GUIDANCE_FRAGMENTS[k] for k in _GUIDANCE_ORDER if k in fragments]
    label = ", ".join(names) if names else "none"
    discovery = (
        f"TICKET-MATCHED DOMAIN GUIDANCE — loaded: {label}. Only guidance supported by "
        "this layer's tickets and owned axes is resident. If the reference reveals a "
        "missing hard technique, use find_recipe with that visible need; do not improvise "
        "from an unrelated module."
    )
    return "\n\n".join((discovery, *selected))


def builder_system(axes: list[tuple[str, str]], recipe_index: str = "", *, ticket_context: str | None = None) -> str:
    """Build a stable core plus only the domain guidance relevant to this layer.

    `ticket_context=None` retains the full-domain form for diagnostics/backward callers.
    Production passes the layer excerpt, scope and axes, keeping unrelated barrel-roll,
    tower, cloud and grading lore out of sessions that cannot act on it.
    """
    look_owned = axes_own_look(axes)
    ownership_rule = (
        "OWNERSHIP MODE — APPEARANCE/LOOK IS IN SCOPE. Exposure and reference-image "
        "appearance deltas are actionable for the axes below."
        if look_owned
        else "OWNERSHIP MODE — FORM/LAYOUT ONLY. Do not tune lighting, material albedo, "
        "emission, bloom, grade, or exposure to chase the finished reference. "
        "compare_frame hides those unowned metrics. Use exactly one "
        "render_pass(shade='matcap:check_normal+y') fixed form diagnostic, satisfy the "
        "authoritative scene contracts, then hand off to the critic."
    )
    body = _CORE_TMPL.format(axes="\n".join(f"  - {k}: {desc}" for k, desc in axes), ownership_rule=ownership_rule)
    parts = [body, _SCREEN_COORDS, _ALWAYS_GUIDANCE, _ticket_guidance(ticket_context)]
    if recipe_index:
        parts.append(recipe_index)
    return "\n\n".join(p for p in parts if p)


def recurring_complaints(shot, m: Milestone, min_attempts: int = 2) -> str:
    """What EARLIER ATTEMPTS at this layer were told, and kept being told.

    The ledger records every critic round with its issues, and none of it reached the
    builder: each attempt started blind to what the previous one had already been
    corrected on. Layer 5's second attempt re-derived a six-light rig without knowing
    the first had been told "the hero is not light-linked" and "the podium is overlit"
    twice. The critique-feedback path added earlier only carries WITHIN an attempt.

    A failed canonical verdict is already the adjudicated end-of-attempt result, so it is
    handed to the very next attempt. Ordinary live-round notes still require recurrence
    across attempts before they become standing defects.
    """
    from .ledger import Ledger

    try:
        slot = Ledger(shot)._slot(m)
    except Exception:
        return ""
    history = list(slot.get("history") or [])
    rounds = [row for attempt in history for row in (attempt.get("rounds") or [])]
    rounds.extend(slot.get("rounds") or [])

    # Ledger.begin() archives the previous attempt and clears slot.rounds before this
    # function is called. Reading only slot.rounds made the handoff path permanently
    # empty in production even though its unit fixture passed.
    for attempt in reversed(history):
        if attempt.get("status") not in {"failed", "judge_conflict"}:
            continue
        canonical_issues = []
        for row in attempt.get("rounds") or []:
            if row.get("kind") != "canonical" or row.get("pass"):
                continue
            canonical_issues.extend(str(issue) for issue in (row.get("issues") or []))
        canonical_issues = list(dict.fromkeys(canonical_issues))[:6]
        if canonical_issues:
            lines = "\n".join(f"    - {issue}" for issue in canonical_issues)
            return (
                f"\nTHE PREVIOUS ATTEMPT FAILED CANONICAL REPLAY. Its unresolved, "
                f"frame-scored defects are immediate repair inputs:\n{lines}\n"
                "Start from the replayed prior artifact and address these mechanisms. "
                "Do not rebuild the same approach from scratch or wait for the critic to "
                "rediscover them. Preserve frames that already passed.\n"
            )
    attempts = {r.get("attempt") for r in rounds if r.get("attempt")}
    if len(attempts) < min_attempts:
        return ""
    # An issue "recurs" when its opening words show up under more than one attempt.
    seen: dict[str, set] = {}
    for r in rounds:
        a = r.get("attempt")
        for issue in r.get("issues") or []:
            key = " ".join(str(issue).lower().split()[:6])
            seen.setdefault(key, set()).add(a)
    repeated = sorted((k for k, v in seen.items() if len(v) >= min_attempts), key=lambda k: -len(seen[k]))
    if not repeated:
        return ""
    lines = "\n".join(f"    - {k}…  (raised under {len(seen[k])} separate attempts)" for k in repeated[:6])
    return (
        f"\nTHIS LAYER HAS BEEN ATTEMPTED {len(attempts)} TIMES BEFORE AND FAILED. These "
        f"notes were raised again under a LATER attempt, so a previous build already "
        f"tried and did not resolve them:\n{lines}\n"
        f"Read them as the layer's standing defects, not as one critic's opinion. If your "
        f"approach does not specifically address each one, it will fail the same way. If "
        f"you believe a note is wrong or impossible, say so explicitly in your APPROACH "
        f"line and explain why — do not silently skip it.\n"
    )


def builder_kickoff(
    shot,
    m: Milestone,
    priors: list[str] | None = None,
    script_rel: str | None = None,
    plan_excerpt: str = "",
    also_judged: list | None = None,
    history: str = "",
) -> str:
    adir = shot.folder / "assets"
    assets = sorted(p.name for p in adir.iterdir() if (p / "model.glb").is_file()) if adir.is_dir() else []
    asset_line = (
        f"AVAILABLE ASSETS — import with import_asset() using these EXACT names "
        f"(do NOT guess a name): {assets}. Prefer the committed hero mesh over "
        f"hand-modelling a detailed prop.\n\n"
        if assets
        else ""
    )
    plan_block = (
        f"YOUR LAYER'S PLAN SECTION — these tickets are your build instructions "
        f"(methods, starting values marked *(start)*, gotchas, done-checks). "
        f"Follow them. `plans/global.md` is dependency context only; it does "
        f"not override this layer plan:\n"
        f"---\n{plan_excerpt}\n---\n\n"
        if plan_excerpt
        else ""
    )
    contract_block = ""
    if (shot.folder / "scene_checks.json").is_file():
        contract_block = (
            "EXECUTABLE SCENE CONTRACT — read `scene_checks.json` BEFORE creating or "
            "renaming geometry. Apply the rows for this layer. Contracts select semantic "
            "`roles` only; name-based selectors are invalid. Tag every owned object with "
            "`bvfx_role(...)`. A visually correct but untagged object produces `None` and "
            "fails closed. Validate these contracts "
            "before declaring convergence.\n\n"
        )
    if priors:
        start_line = (
            f"THE SCENE IS NOT EMPTY: the earlier delta script(s) {priors} have already "
            f"run — everything they build exists right now. Your job is THIS unit's "
            f"DELTA only: add/key/modify per your instructions; do NOT rebuild what "
            f"exists, and do NOT break what earlier units already got judged on. "
            f"inspect_scene/list_keyframes first to see what you have."
        )
    else:
        start_line = (
            f"Start from the empty scene, build to hit frame {m.frame}, and "
            f"render eevee to check yourself against the reference. Iterate "
            f"until it matches."
        )
    extra = ""
    if also_judged:
        rows = "\n".join(f"    f{f} vs `{r}`" for f, r in also_judged)
        extra = (
            f"\nTHIS LAYER ALSO ANSWERS FOR these frames — the finished script is "
            f"scored at EVERY one of them and passes only if all clear:\n{rows}\n"
            f"Iterate against f{m.frame}, but before you finalize, render and check "
            f"the others too. A change that fixes f{m.frame} and breaks another of "
            f"your frames is not a fix.\n"
        )
    return (
        f"MODE: LIVE_BUILD — change the warm Blender scene, not the build script.\n"
        f"For each ticket: name one control and its baseline check, make one scoped change, "
        f"then report baseline → after → target and re-check passing constraints.\n\n"
        f"Build unit {m.id} of shot '{shot.id}' — judged at frame {m.frame} of "
        f"{shot.frames} at {shot.fps}fps. Your delta script will be "
        f"`{script_rel or ('build/' + m.id.lower() + '.py')}`.\n\n"
        f"TARGET STATE (must read at frame {m.frame}):\n  {m.reads}\n\n"
        f"REFERENCE: read `{m.ref}` — match its colour, composition and camera state.\n"
        f"{extra}"
        f"Also read `brief.md` for the shot's intent and palette.\n\n"
        f"{contract_block}"
        f"{plan_block}"
        f"{history}"
        f"{asset_line}"
        f"{start_line}\n\n"
        # Captured into the layer's run report. The journal records WHICH bpy calls were
        # made but never WHY, so there was no way to tell whether the recipe cookbook
        # actually changed what the builder reached for — only that it ran something.
        f"FIRST, before any tool call, emit ONE line beginning `APPROACH:` naming the "
        f"technique you intend to use and which recipe or helper (if any) you are basing "
        f"it on. One line, no preamble — then start building."
    )


def revision_prompt(m: Milestone, verdict: dict, candidate_rel: str) -> str:
    raw = verdict.get("scores", {})
    # scores may contain "n/a" for axes outside this stage's scope — rank numerics only
    scores = {k: float(v) for k, v in raw.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    na = [k for k in raw if k not in scores]
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    weakest = [k for k, _ in ranked[:2]]  # the 1–2 lowest in-scope axes
    strong = [k for k, v in scores.items() if v >= 3]  # already good — protect these
    issues = "\n".join(f"  - {s}" for s in verdict.get("issues", [])) or "  (none given)"
    score_str = ", ".join(f"{k}={raw[k]}" for k in raw)
    na_line = (
        f" Axes marked n/a ({', '.join(na)}) are OUT OF SCOPE for this stage — a "
        f"later stage builds them; do not touch them."
        if na
        else ""
    )
    return (
        f"MODE: LIVE_BUILD — make one evidence-backed change in the warm scene.\n"
        f"Round scored mean {verdict.get('mean')} (scores: {score_str}) — REVISE.\n\n"
        f"Make a SURGICAL revision — this is the key to converging. Fix ONLY the weakest "
        f"axes: {', '.join(weakest) or '(none scored)'}.{na_line} Do NOT rebuild the scene "
        f"and do NOT touch what already works ({', '.join(strong) or '(nothing ≥3 yet)'}) — "
        f"broad re-tuning fixes one axis and breaks another, and the score stalls. Use "
        f"compare_frame(`{m.ref}`) to see the gap on the weak axes, make the SMALLEST change "
        f"that closes it. State the control and baseline first; after editing, report "
        f"baseline → after → target, then re-render eevee and confirm nothing that was ≥3 "
        f"regressed. If the intended pixels may not have changed, use verify_change before "
        f"another edit. "
        f"Critic notes:\n{issues}"
    )


def finalize_prompt(
    shot, m: Milestone, priors: list[str] | None = None, script_rel: str | None = None, journal_rel: str | None = None
) -> str:
    script = script_rel or f"build/{m.id.lower()}.py"
    if priors:
        scope = (
            f"a DELTA script: the harness re-runs {priors} first, then your script. "
            f"Reproduce ONLY the changes you made this session (keys, ramps, new/modified "
            f"objects/materials) — do not repeat what the earlier scripts already build"
        )
    else:
        scope = (
            f"a script that rebuilds this entire scene from an EMPTY scene, reproducing "
            f"frame {m.frame} exactly as you have it"
        )
    return (
        f"MODE: FINALIZE_SCRIPT — the live search is over; publish its deterministic "
        f"artifact. Do not make new look decisions in this mode.\n\n"
        f"Now persist your work. Write `{script}` — {scope}. Assume the "
        f"frame range (1–{shot.frames}), fps ({shot.fps}) and motion blur are already set "
        f"by the harness. To bring in a committed hero mesh, call "
        f"`bvfx_import_asset('<name>')` (the import_asset TOOL is NOT in scope inside the "
        f"script) — do not hardcode asset file paths. Use your Write tool. Write only that "
        f"file."
        + (
            f"\n\nSTART FROM THE TRANSCRIPT, don't rewrite from memory: `{journal_rel}` "
            f"holds every run_bpy call you made this session that succeeded, in order. "
            f"Read it and PRUNE — drop probes, measurements and tweaks that were later "
            f"superseded, keep the calls whose effect survives in the current scene, and "
            f"merge them into clean ordered code. Re-deriving this from memory is how the "
            f"script drifts from the scene you actually built."
            if journal_rel
            else ""
        )
    )


# --------------------------------------------------------------------------- #
# Critic                                                                       #
# --------------------------------------------------------------------------- #
CRITIC_SYSTEM = """\
You are the CRITIC in an automated 3D/VFX pipeline — an exacting VFX supervisor with
a photographic eye. You compare a CANDIDATE render against a REFERENCE image and
score how close the candidate is on fixed look axes. You are hard to please: a score
of 5 means indistinguishable from a top-tier reference; 3 means "acceptable, reads
right"; 0 means absent or wrong. Judge only what the images show. Be concrete: each
below-threshold observation names one bounded correction, but the harness—not you—decides
whether its claim/evidence authority permits that correction to reach a builder.

OBSERVATION BEFORE PRESCRIPTION. State only what is visibly different in the supplied
images, then give at most one action for an axis that scores below 3. Do not invent exact
RGB values, dimensions, emission strengths, or hidden causes that the images cannot show.
If every scored axis is at least 3, `observations` must be empty; optional polish is not a defect.

CLAIM AND MEASUREMENT AUTHORITY. The request may include an ACTIVE CLAIM MANIFEST and a
VERIFIED EVIDENCE card produced by
executable checks on the exact candidate image and live Blender scene. Treat those values
as facts. Do not
re-estimate a listed quantity from the JPEG, contradict a passing check, or prescribe a
numeric correction for it. An exact size/count/position claim covered by that card may be
blocking only when you bind the observation to that claim and cite its FAILED evidence id.
If a measurable visible defect has no matching claim, use claim_id=null: it is a coverage
finding, not permission to borrow a different property's evidence. When no evidence card is
supplied, avoid invented numbers but still type each mismatch honestly. Qualitative read,
silhouette, hierarchy and resemblance remain your responsibility, but only a claim with
qualified qualitative authority can autonomously block a build.

EXISTENCE IS NOT LEGIBILITY. A passing scene contract proves geometry/state (for example,
three rib objects exist and the wall is shade-smooth); it does not prove that all three ribs
read clearly in the render or that smooth geometry is lit to look curved. If the render is
still visually weak, describe that visible residual precisely ("left rib merges into the
wall at this exposure"), classify it as visual, and prescribe a visibility/light/separation
fix. Never rewrite that residual as a contradictory scene-fact claim ("the rib is absent"
or "the wall lacks segments").

FOCUS ONLY WHEN NEEDED. The harness can optically rerender at most two small regions. Use
`focus_requests` only when a feature material to an axis scoring at or below 3 is genuinely
too small to resolve in the full images. The region is [x0,y0,x1,y1], normalized with
origin TOP-LEFT. Every request MUST name `source_frame` and `source`. For
`source="candidate_frame"`, region is local to that one shot frame. For
`source="motion_strip"`, region is global to the attached horizontal strip and must remain
inside exactly one panel; the harness maps it back to source_frame before rerendering. Never
copy a strip panel's x position into candidate-frame coordinates. Never use a crop to replace
full-frame composition/context, inspect a fact already settled by executable evidence, or
fish for defects. When focus panels are supplied, they contain aligned candidate/reference
views of the exact same frame and region; request no more and cite any panel supporting an
observation in `observations[].panel_ids`.

Return your judgement as a single fenced ```json block and NOTHING else after it,
with exactly this shape:

```json
{
  "scores": { "<axis>": 0-5, ... one entry per axis given ... },
  "observations": [
    {"id": "stable_id", "kind": "qualitative" | "measurable", "axis": "<axis>",
     "property": "atomic_property", "observation": "visible evidence only",
     "action": "one bounded correction", "moment": 40, "roles": ["semantic.role"],
     "claim_id": "exact_claim_id" | null, "check_ids": [], "panel_ids": []}
  ],
  "focus_requests": [
    {"id": "rib_left", "axis": "<axis>", "source": "candidate_frame" | "motion_strip",
     "source_frame": 40, "region": [x0,y0,x1,y1],
     "reason": "what cannot be resolved in the full frame"}
  ],
  "reference_usable": true,
  "reference_note": ""
}
```
"""


def critic_prompt(
    shot,
    m: Milestone,
    candidate_rel: str,
    axes: list[tuple[str, str]],
    motion_rel: str | None = None,
    motion_frames: list[int] | None = None,
    scope: str | None = None,
    evidence: list[dict] | None = None,
    claims: list[dict] | None = None,
    review_mode: str = "observer",
    focus_panels: list[dict] | None = None,
    focus_frames: list[int] | None = None,
) -> str:
    axes = "\n".join(f"  - {k}: {desc}" for k, desc in axes)
    # The images are ATTACHED to this request, not fetched. The critic used to be an agent
    # that had to call Read to see them, and that indirection caused the same bug three
    # separate times: the path sandbox stonewalled its reads, relative paths resolved to
    # the repo root, and the guard meant to catch a blind verdict inspected the REQUEST
    # instead of the RESULT so it never fired. An attached frame cannot go unread.
    motion = ""
    if motion_rel:
        motion = (
            f"\nThe THIRD image is a MOTION STRIP — frames {motion_frames} of the shot side "
            f"by side. Judge any MOTION/finish axis (motion blur, weighty continuous "
            f"movement, the roll/dive progressing) from THAT strip, not from the single "
            f"still (a still at one frame cannot show motion). Judge every other axis from "
            f"the candidate.\n"
        )
    scope_block = ""
    if scope:
        scope_block = (
            f"\n⚠ THIS IS A PARTIAL BUILD STAGE, NOT THE FINISHED SHOT. Its scope:\n"
            f"{scope}\n"
            f"The axis list below has already been filtered to exactly what this layer "
            f"owns. Score EVERY supplied axis with a number; there is no n/a decision in "
            f"this stage. `observations` must contain only visible defects on a supplied axis "
            f"that scored below 3, and only fixes inside this stage's scope — never 'add "
            f"the thing a later stage adds'.\n"
        )
    evidence_block = ""
    if evidence:
        rows = []
        for item in evidence:
            value = item.get("value")
            result = "PASS" if item.get("pass") else "FAIL"
            authority = "contract" if item.get("authoritative") else item.get("origin", "check")
            source = item.get("source", "image_check")
            matched = item.get("objects") or []
            object_note = f"; objects={','.join(matched)}" if matched else ""
            rows.append(
                f"  - {item.get('id')}: {item.get('metric')}={value} against "
                f"{item.get('target')} — {result} ({authority}; {source}{object_note})"
            )
        evidence_block = (
            "\nVERIFIED EVIDENCE ON THE EXACT CANDIDATE (machine-evaluated; values and "
            "PASS/FAIL are facts):\n" + "\n".join(rows) + "\n"
            "If you raise a measurable issue, begin it with `[check:<FAILED_ID>]`. "
            "A measurable issue without a failed id, or one contradicting a PASS above, "
            "will be removed before it can trigger repair. A scene-contract PASS proves "
            "the named state exists, not that it reads well: report any remaining visibility "
            "problem as a qualitative observation without denying the measured fact.\n"
        )
    claims_block = ""
    if claims:
        rows = []
        for claim in claims:
            rows.append(
                f"  - {claim.get('id')}: axis={claim.get('axis')} "
                f"property={claim.get('property')} roles={claim.get('roles') or []} "
                f"controls={claim.get('controls') or []} authority={claim.get('authority')} "
                f"evidence={claim.get('evidence_ids') or []} — {claim.get('proposition')}"
            )
        claims_block = (
            "\nACTIVE CLAIM MANIFEST FOR THIS EXACT MOMENT:\n"
            + "\n".join(rows)
            + "\nFor a planned defect, set claim_id to exactly one id above and cite only "
            "that claim's evidence ids. If a visible property is absent from this manifest, "
            "set claim_id to null: that is a coverage finding for the planner, not permission "
            "for the builder to mutate the scene. Never borrow another property's passing or "
            "failing check.\n"
        )
    focus_block = ""
    if focus_panels:
        focus_block = (
            "\nSUPPLIED FOCUS PANELS (supplemental; the full frame still controls "
            "composition/context):\n"
            + "\n".join(
                f"  - {panel.get('id')}: axis={panel.get('axis')} crop={panel.get('crop')} "
                f"source_frame=f{panel.get('source_frame')} reference={panel.get('reference')} "
                f"views={panel.get('views')} — {panel.get('reason')}"
                for panel in focus_panels
            )
            + "\nThese already answer the close-inspection request. Return an empty "
            "focus_requests list and cite any panel used in observations[].panel_ids.\n"
        )
    review_block = ""
    if review_mode == "evidence_audit":
        review_block = (
            "\nSECOND-OPINION ROLE: evidence auditor. Start from the executable evidence, "
            "then independently inspect only the qualitative residuals. The first judge "
            "was borderline; do not repeat a numeric estimate the evidence already answers.\n"
        )
    elif review_mode == "tie_breaker":
        review_block = (
            "\nTIE-BREAK ROLE: conservative adjudicator. Separate machine-verifiable facts "
            "from photographic judgment. Fail only for a visible qualitative defect or a "
            "cited failed check, not because another judge may have failed it.\n"
        )
    elif review_mode == "focus_review":
        panel_lines = "\n".join(
            f"  - {panel.get('id')}: axis={panel.get('axis')} source_frame="
            f"f{panel.get('source_frame')} crop={panel.get('crop')} — {panel.get('reason')}"
            for panel in (focus_panels or [])
        )
        review_block = (
            "\nFOCUS-REVIEW ROLE. The full reference and candidate remain the decision "
            "context; the additional aligned panels only resolve small-feature legibility. "
            "Each panel contains candidate/reference detail views at the exact same crop. "
            "Do not request another crop. If a blocking visual issue relies on a focus "
            "panel, cite its id in that observation's panel_ids.\n" + panel_lines + "\n"
        )
    return (
        f"Stage {m.id} of shot '{shot.id}', frame {m.frame}.\n"
        f"TARGET STATE: {m.reads}\n\n"
        f"The FIRST image is the REFERENCE ({Path(m.ref).name}).\n"
        f"The SECOND image is the CANDIDATE render ({Path(candidate_rel).name}).{motion}"
        f"{scope_block}{claims_block}{evidence_block}{focus_block}{review_block}"
        f"\nScore the candidate against the reference on these axes:\n"
        f"{axes}\n\n"
        f"Score each axis 0–5"
        f"{' (or n/a only when no layer scope is supplied)' if not scope else ''}. "
        f"For a score below 3, put one typed item in `observations`: visible evidence, "
        f"one atomic property, one bounded correction, the exact moment and semantic roles. "
        f"Use `focus_requests` only when a feature material to an axis scoring at or below "
        f"3 is too small to resolve in the full images: at most two [x0,y0,x1,y1] regions "
        f"in normalized TOP-LEFT coordinates. Focusable source frames with matching "
        f"references are {focus_frames or [m.frame]}. Name source_frame and whether region "
        f"uses candidate_frame coordinates or global motion_strip coordinates. A motion-strip "
        f"region must stay inside one panel. Never request a crop for a measurable fact "
        f"already settled by evidence, and return an empty list whenever focus panels are "
        f"already supplied. "
        f"If all scores are at least 3, return an empty `observations` list. "
        f"Return the JSON scorecard."
    )


def canonical_repair_prompt(
    m: Milestone,
    failed: list,
    script_rel: str,
    holding: list | None = None,
    rejected_repairs: list[str] | None = None,
) -> str:
    """Hand a CANONICAL failure back to the builder that wrote the script.

    The distinction this prompt has to land is the one the builder gets wrong by default:
    it has spent the whole layer tuning a LIVE scene, but what just failed is its SCRIPT
    replayed from empty. Those diverge whenever the script omits something the live
    session accumulated, and the builder's instinct is to re-tune the live scene, which
    changes nothing about the artifact being judged.
    """
    blocks = []
    for frame, v in failed:
        issues = "\n".join(f"    - {s}" for s in (v.get("issues") or [])[:6]) or "    (no specific issues returned)"
        scores = ", ".join(f"{k}={val}" for k, val in (v.get("scores") or {}).items() if val != "n/a")
        evidence = "\n".join(
            f"      {e.get('id')}: {e.get('value')} vs {e.get('target')} — {'PASS' if e.get('pass') else 'FAIL'}"
            for e in (v.get("evidence") or [])
        )
        blocks.append(
            f"  f{frame} — scored {v.get('mean')} ({scores})\n{issues}"
            + (f"\n    verified evidence:\n{evidence}" if evidence else "")
        )
    # Frames that currently pass are CONSTRAINTS. Omitting them produced whack-a-mole:
    # one repair fixed f440 and left f45 broken, the next fixed f45 and broke f440.
    keep = ""
    if holding:
        rows = "\n".join(f"    f{f} — currently {v.get('mean')}, PASSING" for f, v in holding)
        keep = (
            f"\nTHESE FRAMES ALREADY PASS. They are constraints, not context:\n"
            f"{rows}\n"
            f"A change that fixes a failing frame and breaks one of these is NOT a "
            f"fix — it is a trade, and the unit still fails. Re-check them before you "
            f"declare done. If a fix genuinely cannot be made without regressing one, "
            f"say so explicitly instead of shipping the trade.\n"
        )
    rejected = ""
    if rejected_repairs:
        attempts = "\n\n".join(
            f"  REJECTED ATTEMPT {index}:\n{details}" for index, details in enumerate(rejected_repairs, 1)
        )
        rejected = (
            "\nPREVIOUS REPAIR ATTEMPTS WERE TRANSACTIONALLY REVERTED:\n"
            f"{attempts}\n"
            "You are editing the last accepted pre-repair script. Do not repeat a rejected "
            "delta unchanged. Use the observed regression/no-progress result to choose a "
            "different light path, placement, control, or other root mechanism while "
            "preserving the passing frames.\n"
        )
    return (
        f"MODE: REPAIR_SCRIPT — edit the canonical artifact, not the warm scene.\n"
        f"Use Grep → Read the smallest span → Edit. Write must not "
        f"replace the whole file for a local repair. All verdict evidence needed for "
        f"this repair is embedded below: inspect `{script_rel}` only; do not search "
        f"plans, checks, runtime evidence, logs, journals, or unrelated build scripts.\n\n"
        f"CANONICAL VERIFICATION FAILED for unit {m.id}.\n\n"
        f"Your script `{script_rel}` was re-run FROM AN EMPTY SCENE and the result was "
        f"scored at every frame this unit answers for. These frames did not clear:\n\n"
        + "\n\n".join(blocks)
        + "\n"
        + keep
        + rejected
        + "\n"
        f"Read that carefully: the live scene you have been tuning is NOT what failed. "
        f"The SCRIPT's output is. If the script omits something you built interactively, "
        f"or builds it in an order that changes the result, the two will disagree — so "
        f"fix `{script_rel}` itself, then reason about what it produces from empty.\n\n"
        f"Work the listed issues in order, but treat critic prose as OBSERVATION, not "
        f"measurement. Before changing a measurable property, confirm it against the "
        f"verified evidence/checks supplied with the verdict. If the claimed defect is "
        f"not present, say so and leave that property unchanged. Do NOT start a new "
        f"approach, and do NOT re-tune the live scene and declare it fixed. Use "
        f"Grep / Read that span / Edit — never rewrite the whole "
        f"file for a few values. When you are done, say so and the script will be "
        f"re-verified from empty again."
    )
