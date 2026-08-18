"""Find names a module USES but never binds — NameErrors waiting for the right code path.

    python -m pipeline.evals unbound

WHY. `build_agent.build_unit()` called `reset_tool_use()` and `tool_use_summary()`, and
`build_agent` never imported either. The call sits at the top of the function, outside any
try block, so EVERY layer build raised `NameError` before doing any work — the main build
path was dead. Nothing caught it:

  · the harness tests for the tool telemetry passed, because they import the functions
    from `pipeline.log` themselves rather than through the module that calls them;
  · importing `pipeline.build_agent` succeeds, because an unbound global is only resolved
    when the line executes;
  · the per-layer report is written inside a `try/except Exception` that logs
    "! run report unavailable" — so the SECOND of the two failures had a cosy place to
    hide even once the first was fixed.

A linter would say this. There is no linter in this repo's loop, and the failure is
severe (the pipeline cannot build) and silent (nothing runs until a real build), so it
gets a check of its own that runs with the rest of the suite in under a second.

It is a compile-time check on purpose: no module is imported, nothing is executed, so it
is safe to run over code with heavy or Blender-only imports.
"""

from __future__ import annotations

import ast
import builtins
import dis
import types
from pathlib import Path

_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__",
                                  "__loader__", "__package__", "__builtins__",
                                  "__annotations__", "__debug__"}


def _bound_at_module_level(tree: ast.Module) -> set[str]:
    """Every name the module namespace will contain. Misses nothing that matters:
    AnnAssign (`_ERRORS: list[str] = []`) is included — leaving it out is what made the
    first version of this check report four false positives."""
    out: set[str] = set()

    def add_target(t):
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                add_target(e)
        elif isinstance(t, ast.Starred):
            add_target(t.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                add_target(t)
        elif isinstance(node, ast.AnnAssign):
            add_target(node.target)
        elif isinstance(node, ast.AugAssign):
            add_target(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            add_target(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            add_target(node.optional_vars)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.Global):
            out.update(node.names)
        elif isinstance(node, (ast.NamedExpr,)):
            add_target(node.target)
    return out


def _walk_codes(co: types.CodeType):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from _walk_codes(c)


def scan(path: Path) -> list[dict]:
    """Global loads in `path` that the module never binds. Compiled, never imported."""
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
        code = compile(src, str(path), "exec")
    except SyntaxError as e:
        return [{"name": "<syntax error>", "func": "-", "line": e.lineno or 0,
                 "detail": str(e)}]

    bound = _bound_at_module_level(tree) | _BUILTINS
    # A local import inside a function binds the name for that function only; the
    # bytecode then loads it as a LOCAL, not a global, so it never reaches this list.
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for co in _walk_codes(code):
        for ins in dis.get_instructions(co):
            if ins.opname not in ("LOAD_GLOBAL", "LOAD_NAME"):
                continue
            name = ins.argval
            if not isinstance(name, str) or name in bound:
                continue
            key = (co.co_name, name)
            if key in seen:
                continue
            seen.add(key)
            hits.append({"name": name, "func": co.co_name,
                         "line": ins.positions.lineno if ins.positions else 0,
                         "detail": ""})
    return hits


def audit(root: Path | None = None) -> dict:
    root = root or Path(__file__).resolve().parents[2] / "pipeline"
    # Blender-side modules run under Blender's interpreter with `bpy` present and a
    # different sys.path; they are not importable here and their globals are not this
    # namespace's problem.
    skip = {"worker.py", "checks.py", "render_ext.py"}
    # Recipe spikes are SNIPPETS, executed by the verifier inside Blender in a namespace
    # that already holds the bvfx_* helpers and SPIKE_ARGS. Scanning them as modules
    # reports every injected name as unbound — 15 findings, none of them real, which is
    # exactly enough noise to make the two real ones ignorable.
    skip_dirs = {"_spikes", "__pycache__"}
    found = {}
    for p in sorted(root.rglob("*.py")):
        if p.name in skip or skip_dirs & set(p.parts):
            continue
        hits = scan(p)
        if hits:
            found[str(p.relative_to(root.parent))] = hits
    return {"ok": not found, "modules": found,
            "n": sum(len(v) for v in found.values())}


def report(d: dict) -> str:
    if d["ok"]:
        return ("── unbound names ──\n   ✓ every module binds every global it loads")
    L = [f"── unbound names ──", f"   ✗ {d['n']} name(s) that will raise NameError:"]
    for mod, hits in d["modules"].items():
        L.append(f"   {mod}")
        for h in hits:
            L.append(f"     line {h['line']:>5}  {h['func']}() → {h['name']}"
                     + (f"  {h['detail']}" if h["detail"] else ""))
    L.append("   Each is a live NameError on the first code path that reaches it. An "
             "unbound global does not fail at import, so a module like this passes every "
             "test that does not execute the exact line.")
    return "\n".join(L)


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="pipeline.evals unbound")
    ap.add_argument("--path", help="a single file or directory to scan")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    target = Path(a.path) if a.path else None
    if target and target.is_file():
        hits = scan(target)
        d = {"ok": not hits, "modules": {str(target): hits} if hits else {},
             "n": len(hits)}
    else:
        d = audit(target)
    if a.json:
        import json
        print(json.dumps(d, indent=2))
    else:
        print(report(d))
    return 0 if d["ok"] else 1
