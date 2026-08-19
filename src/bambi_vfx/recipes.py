"""Recipe cookbook — vetted, retrievable Blender snippets for hard effects.

Recipes are DATA (`bambi_vfx/recipes/*.md` with frontmatter), retrieved just-in-time by
the build agent via the `find_recipe` tool and INLINED into build.py (so builds stay
self-contained — a recipe is an authoring aid, not a runtime dependency like a helper).
They grow by harvesting passing builds (see build_agent.distill_recipe), not by hand.
"""

from __future__ import annotations

import re
from datetime import UTC
from pathlib import Path

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

RECIPES_DIR = Path(__file__).with_name("recipes")
_FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = _FM.match(text)
    if not m:
        return {"name": path.stem, "tags": [], "when": "", "verified": False,
                "body": text.strip(), "path": path}
    fm = yaml.safe_load(m.group(1)) or {}
    return {"name": fm.get("name", path.stem), "tags": fm.get("tags", []) or [],
            "when": fm.get("when", ""), "verified": bool(fm.get("verified", False)),
            "body": m.group(2).strip(), "path": path}


def _all() -> list[dict]:
    if not RECIPES_DIR.is_dir():
        return []
    return [_parse(p) for p in sorted(RECIPES_DIR.glob("*.md"))]


def _score(rec: dict, terms: list[str]) -> int:
    head = (rec["name"] + " " + " ".join(rec["tags"]) + " " + rec["when"]).lower()
    body = rec["body"].lower()
    # tag/name/when hits weigh more than body hits; verified gets a small boost
    return sum(3 * (t in head) + (t in body) for t in terms) + (1 if rec["verified"] else 0)


def search_recipes(query: str, k: int = 3) -> list[dict]:
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    if not terms:
        return []
    scored = [(rec, _score(rec, terms)) for rec in _all()]
    return [rec for rec, s in sorted(scored, key=lambda x: -x[1]) if s > 0][:k]


def recipe_index(verified_only: bool = False) -> str:
    """One line per recipe: name + when. This goes in the SYSTEM PROMPT, always.

    Retrieval was never the weak link — `find_recipe("make the city look real")` returns
    night-city-field just fine. Discovery was: the builder can only query for a recipe it
    already suspects exists. Layer S searched for a recipe name it had read in its plan and
    never asked about bloom, so it hand-rolled a 4.x Glare node twice while
    `cinematic-grade` and `blender-5-api` both sat in the cookbook with the fix.

    Skills solve this by keeping name+description in context and loading the body on
    demand. Same trade here: the index is ~4% of the library's tokens.
    """
    recs = [r for r in _all() if r["verified"] or not verified_only]
    if not recs:
        return ""
    lines = [f"  - {r['name']} — {r['when']}" for r in recs if r["when"]]
    return ("RECIPE COOKBOOK — vetted techniques. Call find_recipe(<name>) to pull the "
            f"full snippet + gotchas BEFORE hand-rolling any of these ({len(lines)}):\n"
            + "\n".join(lines))


def log_recipe_use(shot_folder, names: list[str]) -> None:
    """Append which recipes a build actually pulled.

    Without this we cannot promote or prune: a recipe used in every build should graduate
    into a bvfx_* helper (zero context, impossible to mis-adapt), and one never pulled in
    N builds is dead weight behind an index line."""
    if not names:
        return
    import json
    from datetime import datetime
    p = Path(shot_folder) / "logs" / "recipe_use.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"),
                             "recipes": sorted(set(names))}) + "\n")


def build_recipe_tools(on_use=None):
    """An MCP server exposing find_recipe to the build agent."""
    @tool(
        "find_recipe",
        "Search the vetted Blender recipe cookbook for a technique — volumetrics, "
        "materials, compositor, instancing, grade. Returns matching snippets + gotchas "
        "to ADAPT into your run_bpy. Call this BEFORE hand-rolling any hard effect.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    )
    async def find_recipe(args):
        hits = search_recipes(args["query"])
        if on_use and hits:
            try:
                on_use([h["name"] for h in hits])
            except Exception:
                # telemetry drives promote/prune; losing it silently means the cookbook
                # can never learn which recipes earn their place
                print("! recipe-use telemetry failed", flush=True)
        if not hits:
            return {"content": [{"type": "text",
                    "text": "no matching recipe — improvise; a good solution may be harvested "
                            "into the cookbook if this milestone passes."}]}
        # The TOP hit in full, the rest as one-liners. Returning every body meant one query
        # could put ~17KB of cookbook into the conversation, and a tool result is replayed
        # on every subsequent model call — the draft pass alone made 13 of these calls.
        # Summaries-only would be cheaper still but costs a second round-trip in the common
        # case where one recipe is obviously right, so: the likely answer stays immediately
        # usable and the alternatives are named and fetchable by name.
        top, rest = hits[0], hits[1:]
        text = f"### {top['name']}  (when: {top['when']})\n{top['body']}"
        if rest:
            more = "\n".join(f"  · {h['name']} — {h['when']}" for h in rest)
            text += (f"\n\n---\nAlso matched ({len(rest)}); query by name for the body:\n"
                     f"{more}")
        return {"content": [{"type": "text", "text": text}]}

    server = create_sdk_mcp_server(name="recipes", version="0.1.0", tools=[find_recipe])
    return server, ["mcp__recipes__find_recipe"]
