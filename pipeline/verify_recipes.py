"""Re-verify the cookbook against the CURRENT Blender — by RUNNING it.

`verified: true` used to be written once, by the distiller, and then trusted forever. Two
things are wrong with that. The API moves (that is the whole reason `blender-5-api`
exists), and — worse — nobody ever checked that the recipe's code had been *executed* at
all. A recipe wrapped in `def bvfx_x(...)` "passes" a smoke test trivially: defining a
function runs none of its body, so an injected 4.x call inside it sails straight through.

So verification here means one specific thing, and the bar is the `night-city-field` bar:

    the code ran, a callable was actually CALLED, and a number came out.

The spike does exactly that: boot a throwaway headless Blender, build a scaffold scene,
exec the recipe's ```python blocks, then INVOKE every top-level callable the recipe
defined (with arguments synthesised from the scaffold, or supplied by the recipe's own
spike fixture) and measure what each call did to the scene — objects, materials, shader
nodes, modifiers, keyframes created. The measurement, the callables invoked, the Blender
version and the timestamp are written to `recipes/_verified.json` next to the cookbook, so
the claim on any recipe can be re-read later and checked against the code it was made
about (entries carry a sha256 of the exact code that ran; edit the recipe and the evidence
stops matching).

A recipe that cannot be made to run is not a defect — it is UNPROVEN, and it may keep
being useful prose. It just may not say `verified: true`.

Recipes that need scaffolding (a `street_mat`, a `cam_rig`, an argument the synthesiser
cannot guess) supply it in `recipes/_spikes/<name>.py`, which runs before the recipe's own
code and can seed `SPIKE_ARGS` for the invoker. Spike files live outside the .md so the
build agent never sees test scaffolding as if it were part of the technique.

    python -m pipeline.verify_recipes                 # spike everything, write the ledger
    python -m pipeline.verify_recipes --name night-city-field
    python -m pipeline.verify_recipes --audit         # no Blender: claims vs ledger
    python -m pipeline.verify_recipes --sync          # make frontmatter match the evidence
    python -m pipeline.verify_recipes --demote        # downgrade only, never upgrade
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from .recipes import RECIPES_DIR, _all

LEDGER = RECIPES_DIR / "_verified.json"
SPIKES = RECIPES_DIR / "_spikes"

# ```python              -> recipe code, executed
# ```python skip         -> illustrative (a 4.x/5.x contrast, a deliberate error), never run
_FENCE = re.compile(r"```python([^\n]*)\n(.*?)```", re.DOTALL)

# Errors that mean "this snippet wants a real shot", not "the API changed". They are no
# longer a free pass — they make a recipe UNPROVEN — but keeping them separate matters:
# an API move is a defect to fix today, a missing fixture is a spike file somebody owes.
_MISSING_FIXTURE = re.compile(
    r"NameError|"
    r"bpy_prop_collection\[key\].*key \"(?!.*Socket)[a-z_]+\" not found",
    re.IGNORECASE)

SENT = "@@SPIKE@@"

# The scaffold. Recipes are parameterised SNIPPETS, not programs: they assume a scene,
# named fixtures and the bvfx_* helper namespace. Give them as much of that as possible so
# that whatever still fails is failing on ITS OWN api usage, not on our staging.
_PRELUDE = '''
import bpy, math, random, sys, json, inspect, traceback
sys.path.insert(0, PIPELINE_DIR)
try:
    from blender.worker import _HELPERS          # bvfx_* live in the worker namespace
    globals().update(_HELPERS)
except Exception as _e:
    print("HELPERS_UNAVAILABLE", _e)
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
scene = sc
_av = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
sc.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _av else 'BLENDER_EEVEE'
sc.world = bpy.data.worlds.new('w'); sc.world.use_nodes = True   # use_empty leaves world=None
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.object
mat = bpy.data.materials.new('m'); mat.use_nodes = True
obj.data.materials.append(mat)
bsdf = mat.node_tree.nodes.get('Principled BSDF')
nt = mat.node_tree
N = 8
r_min, r_max = 5.0, 50.0
# Real datablocks, not Empties: a recipe doing obj.data.materials or cam.data.sensor_width
# must fail on the API if it fails at all, never on our scaffolding.
def _mkcam(name):
    o = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    sc.collection.objects.link(o); return o
cam = _mkcam('camera'); sc.camera = cam
_mkcam('cam')
cam_rig = bpy.data.objects.new('cam_rig', None); sc.collection.objects.link(cam_rig)
bpy.ops.mesh.primitive_plane_add(size=50); floor = bpy.context.object; floor.name = 'floor'
floor.data.materials.append(bpy.data.materials.new('floor_mat'))
bpy.ops.mesh.primitive_cube_add(); holder = bpy.context.object; holder.name = 'holder'


def _bvfx_snap():
    """Count what exists. The DELTA across a call is the "a number came out" evidence:
    a callable that touches nothing measurable did not do its job, whatever it returned."""
    d = bpy.data
    trees = ([m.node_tree for m in d.materials if m.node_tree]
             + [w.node_tree for w in d.worlds if w.node_tree] + list(d.node_groups))
    keys = 0
    for act in d.actions:
        try:
            for lay in act.layers:
                for st in lay.strips:
                    for cb in st.channelbags:
                        for fc in cb.fcurves:
                            keys += len(fc.keyframe_points)
        except Exception:
            print("! could not walk action", act.name)   # 5.x slotted-action shape changed
    # Counting datablocks alone is blind to IN-PLACE edits: numpy-bulk-mesh-edit reshapes
    # 200 existing boxes and creates nothing, and a count-only metric called that "ran
    # without changing anything". Sum the actual numbers too.
    geom = 0.0
    for m in d.meshes:
        co = [0.0] * (len(m.vertices) * 3)
        m.vertices.foreach_get("co", co)
        geom += sum(co)
    vals = 0.0
    for t in trees:
        for n in t.nodes:
            for s in list(n.inputs) + list(n.outputs):
                v = getattr(s, "default_value", None)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals += v
                elif hasattr(v, "__len__") and not isinstance(v, str):
                    vals += sum(x for x in v if isinstance(x, (int, float)))
    return {"objects": len(d.objects), "materials": len(d.materials),
            "meshes": len(d.meshes), "node_groups": len(d.node_groups),
            "curves": len(d.curves), "textures": len(d.textures),
            "shader_nodes": sum(len(t.nodes) for t in trees),
            "modifiers": sum(len(o.modifiers) for o in d.objects),
            "keyframes": keys,
            "vertex_coord_sum": round(geom, 3), "socket_value_sum": round(vals, 4)}


def _delta(a, b):
    return {k: b[k] - a[k] for k in b if b[k] != a[k]}


# Arguments the invoker may hand a recipe's callable. A spike fixture can add to this —
# that is the supported way to make a callable with shot-specific parameters invocable.
SPIKE_ARGS = {
    "obj": obj, "ob": obj, "object": obj, "mesh": obj.data, "target_obj": obj,
    "mat": mat, "material": mat, "nt": nt, "node_tree": nt, "tree": nt,
    "scene": sc, "sc": sc, "cam": cam, "camera": cam, "rig": cam_rig,
    "floor": floor, "holder": holder, "bsdf": bsdf,
    "sock": bsdf.inputs["Metallic"], "socket": bsdf.inputs["Metallic"],
    "ad": obj.animation_data,
}
SPIKE_NOTE = None     # a fixture may set this to a human-readable measured result
'''

_EPILOGUE = '''
_report = {"blender": bpy.app.version_string, "error": None, "phase": None,
           "defined": [], "called_by_recipe": [], "invoked": [], "unresolvable": [],
           "delta": {}, "note": None}


def _emit():
    print("\\n" + SENT + json.dumps(_report) + SENT + "\\n", flush=True)


G = dict(globals())
_b0 = _bvfx_snap()
try:
    if FIXTURE_SRC:
        _report["phase"] = "fixture"
        exec(compile(FIXTURE_SRC, "<spike-fixture>", "exec"), G)
        _b0 = _bvfx_snap()      # AFTER the fixture: the delta must measure the RECIPE
    _report["phase"] = "recipe"
    exec(compile(RECIPE_SRC, "<recipe>", "exec"), G)
except Exception as _e:
    _report["error"] = "".join(traceback.format_exception_only(type(_e), _e)).strip()
    traceback.print_exc()
    _emit()
    raise SystemExit(3)

_report["phase"] = "invoke"
_report["defined"] = DEFINED
_report["called_by_recipe"] = CALLED_AT_TOP
SPIKE_ARGS = G.get("SPIKE_ARGS", SPIKE_ARGS)
_report["note"] = G.get("SPIKE_NOTE")

_GUESS = (("frame", 24), ("name", "spike"), ("color", (1.0, 0.7, 0.35)),
          ("colour", (1.0, 0.7, 0.35)), ("angle", 30.0), ("deg", 30.0),
          ("count", 4), ("seed", 0), ("path", "nodes[\\"Emission\\"].inputs[1].default_value"))


def _arg_for(p):
    if p.name in SPIKE_ARGS:
        return SPIKE_ARGS[p.name], True
    for suffix, val in _GUESS:
        if p.name == suffix or p.name.endswith("_" + suffix):
            return val, True
    if p.name in ("f", "fr"):
        return 24, True
    return None, False


for _fn_name in DEFINED:
    _fn = G.get(_fn_name)
    if not callable(_fn) or _fn_name in CALLED_AT_TOP:
        continue                       # already exercised by the recipe's own apply block
    try:
        _sig = inspect.signature(_fn)
    except (TypeError, ValueError):
        continue
    _kw, _missing = {}, []
    for _p in _sig.parameters.values():
        if _p.kind in (_p.VAR_POSITIONAL, _p.VAR_KEYWORD):
            continue
        _v, _got = _arg_for(_p)
        if _got:
            _kw[_p.name] = _v
        elif _p.default is _p.empty:
            _missing.append(_p.name)
    if _missing:
        # Cannot prove this one ran. Not a defect — an unwritten spike fixture.
        _report["unresolvable"].append({"fn": _fn_name, "params": _missing})
        continue
    _a = _bvfx_snap()
    try:
        _out = _fn(**_kw)
        if inspect.isgenerator(_out):
            # Calling a generator function runs NONE of its body — exactly the
            # define-but-never-execute hole this whole tool exists to close.
            _out = list(_out)
        _report["invoked"].append({"fn": _fn_name, "ok": True,
                                   "delta": _delta(_a, _bvfx_snap())})
    except Exception as _e:
        _err = "".join(traceback.format_exception_only(type(_e), _e)).strip()
        _report["invoked"].append({"fn": _fn_name, "ok": False, "err": _err, "delta": {}})
        traceback.print_exc()

_report["delta"] = _delta(_b0, _bvfx_snap())
_emit()
'''


def _blocks(body: str) -> list[str]:
    """Executable code blocks, dedented. `skip` fences are illustrative and never run.

    Dedent matters: half this cookbook's code lives inside markdown list items, indented
    two spaces. An earlier verifier read that as an IndentationError and called the whole
    of `blender-5-api` "unverifiable" — a scaffolding bug wearing a recipe's failure.
    """
    out = []
    for info, code in _FENCE.findall(body):
        if "skip" in info.lower():
            continue
        code = textwrap.dedent(code)
        # unannotated legacy markers for "this block is showing you the WRONG way"
        if any(b in code for b in ("# 4.x", "AttributeError in 5.x", "does not exist",
                                   "# WRONG", "TypeError in 5.x")):
            continue
        out.append(code)
    return out


def _static(code: str) -> tuple[list[str], list[str], str | None]:
    """(top-level defs, names called at top level, syntax error).

    Both halves are load-bearing. `defined` is what we must prove RAN; `called at top
    level` is the recipe proving it itself, which is better evidence than our synthesised
    invocation and must not be double-executed (some apply blocks are not idempotent).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [], [], f"{type(e).__name__}: {e}"
    defined = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    called = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            continue                    # a call INSIDE a def is not a call
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                called.append(n.func.id)
    return defined, sorted(set(called)), None


def _touched(delta: dict) -> bool:
    return any(v for v in delta.values())


def spike(rec: dict, blender: str = "blender", timeout: int = 240) -> dict:
    """Run the recipe for real. Returns the evidence record (verdict + measurement)."""
    blocks = _blocks(rec["body"])
    code = "\n\n".join(blocks)
    sha = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
    base = {"name": rec["name"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "code_sha256": sha, "blocks": len(blocks)}

    if not blocks:
        return {**base, "verdict": "prose", "why": "no executable block (prose-only recipe)"}

    defined, called, syn = _static(code)
    if syn:
        return {**base, "verdict": "stale", "why": syn}

    fixture = SPIKES / f"{rec['name']}.py"
    fixture_src = fixture.read_text(encoding="utf-8") if fixture.is_file() else ""

    src = (_PRELUDE.replace("PIPELINE_DIR", repr(str(Path(__file__).resolve().parent)))
           + f"\nSENT = {SENT!r}\nRECIPE_SRC = {code!r}\nFIXTURE_SRC = {fixture_src!r}\n"
           + f"DEFINED = {defined!r}\nCALLED_AT_TOP = {called!r}\n"
           + _EPILOGUE)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src); path = fh.name
    try:
        p = subprocess.run([blender, "--background", "--factory-startup", "--python", path],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {**base, "verdict": "stale", "why": f"timeout after {timeout}s"}
    finally:
        Path(path).unlink(missing_ok=True)

    out = p.stdout + p.stderr
    parts = out.split(SENT)
    if len(parts) < 3:
        tail = [l for l in out.splitlines() if "Error" in l or "error:" in l.lower()]
        return {**base, "verdict": "stale",
                "why": (tail[-1][:200] if tail else f"no spike report, rc={p.returncode}")}
    rep = json.loads(parts[-2])
    base["blender"] = rep["blender"]
    base["fixture"] = fixture.name if fixture_src else None

    if rep["error"]:
        # An API MOVE is a defect in the recipe. A missing placeholder is a defect in our
        # scaffolding — still unproven, but somebody owes a spike file, not a rewrite.
        kind = "unproven" if _MISSING_FIXTURE.search(rep["error"]) else "stale"
        return {**base, "verdict": kind,
                "why": f"{rep['phase']}: {rep['error'][:200]}"}

    broke = [i for i in rep["invoked"] if not i["ok"]]
    if broke:
        return {**base, "verdict": "stale",
                "why": f"{broke[0]['fn']}() raised: {broke[0]['err'][:180]}",
                "invoked": rep["invoked"]}

    ran = [i["fn"] for i in rep["invoked"]] + [c for c in rep["called_by_recipe"]
                                               if c in rep["defined"]]
    evidence = {"invoked": rep["invoked"], "called_by_recipe":
                [c for c in rep["called_by_recipe"] if c in rep["defined"]],
                "defined": rep["defined"], "delta": rep["delta"], "note": rep["note"]}

    if rep["defined"] and not ran:
        return {**base, **evidence, "verdict": "unproven",
                "why": "defines %s but nothing called it; no arguments could be "
                       "synthesised (%s) — add recipes/_spikes/%s.py"
                       % (", ".join(rep["defined"][:3]),
                          ", ".join(u["fn"] for u in rep["unresolvable"]) or "n/a",
                          rec["name"])}
    if not ran and not _touched(rep["delta"]):
        # No callable to invoke AND no measurable effect: this executed the way an empty
        # file executes. `warm-session-probe-loop` is the honest edge — it renders and
        # measures rather than building anything, so its evidence is the CALL, not a delta.
        return {**base, **evidence, "verdict": "unproven",
                "why": "ran without calling anything or changing the scene"}
    _label = {"vertex_coord_sum": "vertex coords moved",
              "socket_value_sum": "socket values changed"}
    what = ", ".join(_label.get(k) or (f"{k} +{v}" if v > 0 else f"{k} {v}")
                     for k, v in sorted(rep["delta"].items())) or "read-only, built nothing"
    return {**base, **evidence, "verdict": "verified",
            "why": f"ran {'+'.join(ran) or 'top-level code'} → {rep['note'] or what}"}


# ---- ledger -----------------------------------------------------------------

def load_ledger() -> dict:
    if not LEDGER.is_file():
        return {}
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! unreadable ledger {LEDGER}: {e}")
        return {}


def save_ledger(entries: dict) -> None:
    LEDGER.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def current_sha(rec: dict) -> str:
    return hashlib.sha256("\n\n".join(_blocks(rec["body"])).encode("utf-8")).hexdigest()[:16]


def audit(recs: list[dict], ledger: dict) -> list[tuple[dict, bool, str]]:
    """Cross-check every `verified:` claim against the ledger. No Blender, no cost.

    This is the check that makes the flag mean something between spike runs: a recipe that
    is edited after being verified no longer matches its evidence, and says so.
    """
    rows = []
    for r in recs:
        e = ledger.get(r["name"])
        sha = current_sha(r)
        if not e:
            entitled, why = False, "no spike on record"
        elif e.get("code_sha256") != sha:
            entitled, why = False, f"code changed since spike {e.get('at', '?')[:10]}"
        elif e.get("verdict") == "verified":
            entitled, why = True, f"spiked {e['at'][:10]}: {e.get('why', '')}"
        else:
            entitled, why = False, f"{e.get('verdict')}: {e.get('why', '')}"
        rows.append((r, entitled, why))
    return rows


def set_flag(path: Path, value: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    want, other = f"verified: {str(value).lower()}", f"verified: {str(not value).lower()}"
    if want in text.split("---")[1]:
        return False
    path.write_text(text.replace(other, want, 1), encoding="utf-8")
    return True


# ---- cli --------------------------------------------------------------------

_MARK = {"verified": "✓", "unproven": "?", "stale": "✗", "prose": "·"}


def main() -> None:
    ap = argparse.ArgumentParser(description="Prove recipes by running them.")
    ap.add_argument("--name", help="verify only this recipe")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--audit", action="store_true",
                    help="no Blender: check `verified:` claims against the ledger")
    ap.add_argument("--sync", action="store_true",
                    help="rewrite frontmatter to match the evidence (both directions)")
    ap.add_argument("--demote", action="store_true",
                    help="downgrade unproven/stale recipes, never upgrade")
    args = ap.parse_args()

    recs = [r for r in _all() if not args.name or r["name"] == args.name]
    ledger = load_ledger()

    if not args.audit:
        SPIKES.mkdir(exist_ok=True)
        for r in recs:
            e = spike(r, args.blender)
            ledger[r["name"]] = e
            print(f"  {_MARK[e['verdict']]} {r['name']:32s} {e['verdict']:9s} {e['why']}")
        save_ledger(ledger)
        print(f"\nevidence written to {LEDGER.relative_to(LEDGER.parent.parent.parent)}")

    rows = audit(recs, ledger)
    print()
    wrong = [(r, ent, why) for r, ent, why in rows if r["verified"] != ent]
    for r, ent, why in rows:
        flag = "claims verified" if r["verified"] else "unverified"
        if r["verified"] != ent:
            print(f"  ! {r['name']:32s} {flag}, entitled={ent} — {why}")
    n_ok = sum(1 for _r, e, _w in rows if e)
    print(f"{n_ok} of {len(rows)} recipes have live spike evidence; "
          f"{len(wrong)} frontmatter flag(s) disagree")

    if (args.sync or args.demote) and wrong:
        changed = 0
        for r, ent, _why in wrong:
            if args.demote and ent:
                continue                       # --demote never promotes
            changed += set_flag(r["path"], ent)
        print(f"rewrote verified: on {changed} recipe(s)")
    raise SystemExit(1 if any(e.get("verdict") == "stale"
                              for e in ledger.values()) else 0)


if __name__ == "__main__":
    main()
