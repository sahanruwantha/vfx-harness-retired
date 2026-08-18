"""A structural index of a generated build script, so edits don't require re-reading it.

Build scripts run 10–23 KB. The builder was advertised only Read/Write/Glob, so its
obvious move for "change the plume colour" was to Write the whole file again — 23 KB of
regenerated output to alter one tuple, with every chance of dropping something it had
already got right.

Reading the file first is no cheaper. What makes targeted editing practical is knowing
WHERE to look without loading everything: a map of sections, functions, and — the thing a
build script is actually organised around — which lines create or configure each named
object and material. Then it reads ~30 lines and does a string replacement, which is how
a person edits code.

`outline()` is pure text analysis; no Blender, no model.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# `obj = D.objects.new('tower_dot', me)`, `bpy.data.materials.new("plume_mat")`, …
_CREATE = re.compile(
    r"""(?:bpy\.)?(?:data|D)\.(objects|materials|meshes|worlds|node_groups|collections)"""
    r"""\.new\(\s*['"]([^'"]+)['"]""")
# `D.objects['tower_dot']`, `bpy.data.materials["plume_mat"]`
_LOOKUP = re.compile(
    r"""(?:bpy\.)?(?:data|D)\.(objects|materials|meshes|worlds|node_groups)"""
    r"""\[\s*['"]([^'"]+)['"]\s*\]""")
# `obj.name = 'tower_dot'`
_RENAME = re.compile(r"""\.name\s*=\s*['"]([^'"]+)['"]""")
# banner comments the generators like to emit: `# --- sky ---`, `# 3) city`
_BANNER = re.compile(r"^\s*#\s*(?:[-=#*]{2,}\s*)?(.{3,70}?)\s*(?:[-=#*]{2,})?\s*$")


def outline(path: str | Path, max_names: int = 8) -> str:
    p = Path(path)
    if not p.is_file():
        return f"{p}: no such file"
    src = p.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()

    # top-level defs, with their line spans
    spans: list[tuple[int, int, str]] = []
    try:
        for node in ast.parse(src).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                end = getattr(node, "end_lineno", node.lineno)
                spans.append((node.lineno, end, f"def {node.name}(...)"))
    except SyntaxError as e:
        spans.append((e.lineno or 1, e.lineno or 1, f"!! SyntaxError: {e.msg}"))

    # banner comments become section starts (skip anything inside a def)
    in_def = {n for a, b, _ in spans for n in range(a, b + 1)}
    sections: list[tuple[int, str]] = []
    for i, ln in enumerate(lines, 1):
        if i in in_def or not ln.lstrip().startswith("#"):
            continue
        # only the FIRST line of a comment block is a heading; the rest are prose that
        # continues it (and reads as gibberish out of context in an outline)
        prev = lines[i - 2].strip() if i >= 2 else ""
        if prev.startswith("#"):
            continue
        m = _BANNER.match(ln)
        if m and len(m.group(1)) > 3 and not m.group(1).startswith(("noqa", "type:")):
            sections.append((i, m.group(1)))

    # names created / touched, and where
    created: dict[str, list[int]] = {}
    touched: dict[str, list[int]] = {}
    for i, ln in enumerate(lines, 1):
        for kind, name in _CREATE.findall(ln):
            created.setdefault(f"{kind[:-1]} {name}", []).append(i)
        for _kind, name in _LOOKUP.findall(ln):
            touched.setdefault(name, []).append(i)
        for name in _RENAME.findall(ln):
            created.setdefault(f"object {name}", []).append(i)

    out = [f"{p.name} — {len(lines)} lines, {len(src)//1024} KB",
           "Read only the span you need (Read offset/limit), then Edit that string. "
           "Do NOT rewrite the file."]
    if spans:
        out.append("\nFUNCTIONS")
        for a, b, label in sorted(spans):
            out.append(f"  L{a}-{b}  {label}")
    if sections:
        out.append("\nSECTIONS")
        for i, label in sections:
            out.append(f"  L{i:<5d} {label}")
    if created:
        out.append("\nCREATED HERE (name → lines)")
        for name, ls in sorted(created.items())[:max_names * 4]:
            out.append(f"  {name:38s} L{','.join(map(str, ls[:6]))}")
    if touched:
        out.append("\nREFERENCED FROM EARLIER LAYERS (name → lines)")
        for name, ls in sorted(touched.items())[:max_names * 2]:
            out.append(f"  {name:38s} L{','.join(map(str, ls[:6]))}")
    return "\n".join(out)


def find_lines(path: str | Path, needle: str, context: int = 2) -> str:
    """Where does <needle> appear, with a little context — locate before you Read."""
    p = Path(path)
    if not p.is_file():
        return f"{p}: no such file"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    hits = [i for i, ln in enumerate(lines, 1) if needle.lower() in ln.lower()]
    if not hits:
        return f"{needle!r} not found in {p.name}"
    out = [f"{p.name}: {len(hits)} hit(s) for {needle!r}"]
    for i in hits[:12]:
        lo, hi = max(1, i - context), min(len(lines), i + context)
        out.append(f"  --- L{lo}-{hi}")
        for j in range(lo, hi + 1):
            out.append(f"  {'>' if j == i else ' '}{j:5d} {lines[j-1][:110]}")
    return "\n".join(out)
