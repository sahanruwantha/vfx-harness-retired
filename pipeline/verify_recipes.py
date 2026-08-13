"""Re-verify the cookbook against the CURRENT Blender.

`verified: true` is written once, by the distiller, and then trusted forever — but the
API moves (that is the whole reason `blender-5-api` exists) and a stale recipe is worse
than no recipe: it is confidently wrong, and the builder trusts it.

This extracts each recipe's ```python blocks, runs them in a throwaway headless Blender,
and reports which still execute. It is a smoke test, not a look test: it proves the API
calls resolve, not that the result is beautiful.

    python -m pipeline.verify_recipes            # all
    python -m pipeline.verify_recipes --name night-city-field
    python -m pipeline.verify_recipes --demote   # flip failures to verified: false
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import tempfile
from pathlib import Path

from .recipes import RECIPES_DIR, _all

_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)

# Failures that mean "this snippet needs a real shot", not "the API changed".
_UNVERIFIABLE = re.compile(
    r"NameError|IndentationError|"
    r"bpy_prop_collection\[key\].*key \"(?!.*Socket)[a-z_]+\" not found|"
    r"no camera", re.IGNORECASE)

# Recipes are parameterised SNIPPETS, not programs. They assume a scene, named fixtures
# and the bvfx_* helper namespace. Give them as much of that as possible, then classify
# whatever still fails — a missing placeholder is not an API break.
_PRELUDE = """
import bpy, math, random, sys
sys.path.insert(0, PIPELINE_DIR)
try:
    from blender.worker import _HELPERS          # bvfx_* live in the worker namespace
    globals().update(_HELPERS)
except Exception as _e:
    print("HELPERS_UNAVAILABLE", _e)
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
_av = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
sc.render.engine = 'BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _av else 'BLENDER_EEVEE'
sc.world = bpy.data.worlds.new('w'); sc.world.use_nodes = True   # use_empty=True leaves world=None
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.object
mat = bpy.data.materials.new('m'); mat.use_nodes = True
obj.data.materials.append(mat)
bsdf = mat.node_tree.nodes.get('Principled BSDF')
nt = mat.node_tree
N = 8
r_min, r_max = 5.0, 50.0
# a camera + commonly-assumed fixtures, so a recipe that reaches for one is testing its
# API usage rather than our scaffolding
# Real datablocks, not Empties: a recipe doing obj.data.materials or cam.data.sensor_width
# must fail on the API if it fails at all, never on our scaffolding.
def _mkcam(name):
    o = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    sc.collection.objects.link(o); return o
cam = _mkcam('camera'); sc.camera = cam
_mkcam('cam'); _mkcam('cam_rig')
bpy.ops.mesh.primitive_plane_add(size=50); floor = bpy.context.object; floor.name = 'floor'
floor.data.materials.append(bpy.data.materials.new('floor_mat'))
bpy.ops.mesh.primitive_cube_add(); holder = bpy.context.object; holder.name = 'holder'
"""


def _exercised(code: str) -> bool:
    """Does this block actually CALL anything at module level, or only define?

    A recipe wrapped in `def bvfx_x(...)` executes cleanly whatever is inside it — the
    body never runs. Reporting that as "verified" is a lie of exactly the kind this tool
    exists to prevent: an injected `b.metallic_legacy_4x = 1` inside a def sailed through.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return True                     # a parse failure is a real, reportable failure
    for node in tree.body:
        if isinstance(node, (ast.Expr, ast.Assign, ast.AugAssign, ast.For, ast.With, ast.If)):
            if any(isinstance(n, ast.Call) for n in ast.walk(node)):
                return True
    return False


def _runnable(code: str) -> bool:
    """Skip blocks that are illustrative rather than executable."""
    bad = ("# 4.x", "AttributeError in 5.x", "does not exist", "raises")
    return not any(b in code for b in bad)


def verify(rec: dict, blender: str = "blender") -> tuple[bool, str]:
    blocks = [b for b in _BLOCK.findall(rec["body"]) if _runnable(b)]
    if not blocks:
        return True, "no executable block (prose-only recipe)"
    prelude = _PRELUDE.replace("PIPELINE_DIR", repr(str(Path(__file__).resolve().parent)))
    src = prelude + "\n\n".join(blocks) + "\nprint('RECIPE_OK')\n"
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src); path = fh.name
    try:
        p = subprocess.run([blender, "--background", "--factory-startup", "--python", path],
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    finally:
        Path(path).unlink(missing_ok=True)
    if "RECIPE_OK" in p.stdout:
        if not any(_exercised(b) for b in blocks):
            return None, "syntax-only (defines a helper; body never runs here)"
        return True, "executes"
    tail = [l for l in (p.stdout + p.stderr).splitlines()
            if "Error" in l or "error:" in l.lower()]
    err = tail[-1][:150] if tail else f"rc={p.returncode}"
    # Only an API MOVE is a recipe defect. A snippet reaching for a placeholder or a
    # fixture we did not build is untestable here, not stale.
    if _UNVERIFIABLE.search(err):
        return None, f"unverifiable ({err.split(':')[0]}) — needs shot context"
    return False, err


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-verify recipes against this Blender.")
    ap.add_argument("--name", help="verify only this recipe")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--demote", action="store_true",
                    help="set verified: false on recipes that no longer execute")
    args = ap.parse_args()

    recs = [r for r in _all() if not args.name or r["name"] == args.name]
    ok = bad = skip = 0
    failures = []
    for r in recs:
        good, why = verify(r, args.blender)
        mark = "✓" if good else ("−" if good is None else "✗")
        print(f"  {mark} {r['name']:32s} {why}")
        if good is None:
            skip += 1
        elif good:
            ok += 1
        else:
            bad += 1; failures.append(r)
    print(f"\n{ok} verified · {bad} STALE (api moved) · {skip} unverifiable "
          f"(of {len(recs)})")
    if args.demote and failures:
        for r in failures:
            t = r["path"].read_text(encoding="utf-8")
            r["path"].write_text(t.replace("verified: true", "verified: false", 1), encoding="utf-8")
        print(f"demoted {len(failures)} recipe(s) to verified: false")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
