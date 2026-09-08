"""Recipe cookbook — vetted, retrievable Blender snippets for hard effects.

Recipes are DATA (`vfx_harness/knowledge/recipes/*.md` with frontmatter), retrieved just-in-time by
the build agent via the `find_recipe` tool and INLINED into build.py (so builds stay
self-contained — a recipe is an authoring aid, not a runtime dependency like a helper).
They grow by harvesting passing builds (see build_agent.distill_recipe), not by hand.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from vfx_harness.observability import run_artifacts

RECIPES_DIR = Path(__file__).with_name("recipes")
_FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = _FM.match(text)
    if not m:
        return {"name": path.stem, "tags": [], "when": "", "verified": False,
                "requires_roles": [], "body": text.strip(), "path": path}
    fm = yaml.safe_load(m.group(1)) or {}
    requires = fm.get("requires_roles") or []
    if not isinstance(requires, list):
        requires = []
    return {"name": fm.get("name", path.stem), "tags": fm.get("tags", []) or [],
            "when": fm.get("when", ""), "verified": bool(fm.get("verified", False)),
            "requires_roles": [str(item) for item in requires if str(item).strip()],
            "body": m.group(2).strip(), "path": path}


def _all() -> list[dict]:
    if not RECIPES_DIR.is_dir():
        return []
    return [_parse(p) for p in sorted(RECIPES_DIR.glob("*.md"))]


def _score(rec: dict, terms: list[str]) -> int:
    head = (rec["name"] + " " + " ".join(rec["tags"]) + " " + rec["when"]).lower()
    body = rec["body"].lower()
    # tag/name/when hits weigh more than body hits; verified gets a small boost
    relevance = sum(3 * (t in head) + (t in body) for t in terms)
    return relevance + int(rec["verified"]) if relevance else 0


_ALWAYS_IN_SCOPE = frozenset({"blender-5-api", "warm-session-probe-loop"})
_ALWAYS_TAGS = frozenset({"api", "blender5", "gotcha", "workflow", "measurement"})
_LIGHT_TAGS = frozenset({
    "lighting", "area-light", "bloom", "glare", "exposure", "spot", "emission",
})
_VOLUME_TAGS = frozenset({
    "atmosphere", "volume", "volumetric", "fog", "nebula", "haze", "clouds", "god-rays",
})
_MATERIAL_TAGS = frozenset({"material", "shader", "metal", "grade", "color", "tonemap", "filmic", "halation"})
_COMPOSITOR_TAGS = frozenset({"compositor", "tonemap", "filmic", "grade"})
_LIGHT_GLOBS = ("*light*", "lookdev.*", "world.*")
_VOLUME_GLOBS = ("world.*", "lookdev.*", "*volume*", "*atmos*")
_MATERIAL_GLOBS = ("lookdev.*", "*material*", "mat.*")
_COMPOSITOR_GLOBS = ("world.*", "lookdev.*")
_CAMERA_GLOBS = ("cam_*", "cam.*", "*camera*")


def _role_matches(roles: Sequence[str], globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(role, pattern) for role in roles for pattern in globs)


def recipe_required_globs(rec: dict) -> tuple[str, ...] | None:
    """Role globs the active unit must mutate, or None when the recipe is always legal.

    Lighting/volume/material/compositor tags restrict even when the recipe also
    mentions camera (run 20260826T170413Z-ba2b4c pulled camera-parented-spill-lights
    into a cam_spine unit that cannot tag lights). Explicit ``requires_roles`` wins.
    """
    explicit = rec.get("requires_roles") or []
    if explicit:
        return tuple(str(item) for item in explicit)
    if rec.get("name") in _ALWAYS_IN_SCOPE:
        return None
    tags = {str(tag).lower() for tag in (rec.get("tags") or [])}
    if tags and tags <= _ALWAYS_TAGS:
        return None
    if tags & _LIGHT_TAGS:
        return _LIGHT_GLOBS
    if tags & _VOLUME_TAGS:
        return _VOLUME_GLOBS
    if tags & _COMPOSITOR_TAGS:
        return _COMPOSITOR_GLOBS
    if tags & _MATERIAL_TAGS:
        return _MATERIAL_GLOBS
    if tags & {"camera", "rig", "roll", "shutter", "motion-blur", "aim"}:
        return _CAMERA_GLOBS
    return None


def recipe_in_scope(rec: dict, mutation_roles: Sequence[str] | None) -> bool:
    if mutation_roles is None:
        return True
    globs = recipe_required_globs(rec)
    if globs is None:
        return True
    return _role_matches(mutation_roles, globs)


def search_recipes(
    query: str,
    k: int = 3,
    *,
    mutation_roles: Sequence[str] | None = None,
    pool: int = 8,
) -> list[dict]:
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    if not terms:
        return []
    scored = [(rec, _score(rec, terms)) for rec in _all()]
    ranked = [rec for rec, s in sorted(scored, key=lambda x: -x[1]) if s > 0][: max(k, pool)]
    if mutation_roles is None:
        return ranked[:k]
    return [rec for rec in ranked if recipe_in_scope(rec, mutation_roles)][:k]


def out_of_scope_hits(query: str, mutation_roles: Sequence[str], *, pool: int = 8) -> list[dict]:
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    if not terms:
        return []
    scored = [(rec, _score(rec, terms)) for rec in _all()]
    ranked = [rec for rec, s in sorted(scored, key=lambda x: -x[1]) if s > 0][:pool]
    return [rec for rec in ranked if not recipe_in_scope(rec, mutation_roles)]


_CONTEXT_STOP = {
    "about", "after", "again", "against", "also", "before", "being", "build",
    "built", "every", "frame", "from", "into", "layer", "must", "only", "other",
    "reference", "scene", "should", "that", "their", "then", "there", "these",
    "this", "through", "ticket", "using", "when", "where", "which", "with",
}


def _context_terms(context: str) -> list[str]:
    return sorted({t for t in re.findall(r"[a-z0-9]+", context.lower())
                   if len(t) > 3 and t not in _CONTEXT_STOP})


def _context_score(rec: dict, terms: list[str]) -> int:
    """Metadata relevance without `_score`'s baseline or broad body false positives."""
    head = (rec["name"] + " " + " ".join(rec["tags"]) + " " + rec["when"]).lower()
    words = set(re.findall(r"[a-z0-9]+", head))
    return sum(1 for t in terms if t in words)


def recipe_index(verified_only: bool = False, *, context: str | None = None,
                 limit: int = 8) -> str:
    """One line per recipe: name + when, optionally filtered to one layer's tickets.

    Retrieval was never the weak link — `find_recipe("make the city look real")` returns
    night-city-field just fine. Discovery was: the builder can only query for a recipe it
    already suspects exists. Layer S searched for a recipe name it had read in its plan and
    never asked about bloom, so it hand-rolled a 4.x Glare node twice while
    `cinematic-grade` and `blender-5-api` both sat in the cookbook with the fix.

    Skills solve this by keeping name+description in context and loading the body on
    demand. Same trade here: the index is ~4% of the library's tokens.
    """
    recs = [r for r in _all() if r["verified"] or not verified_only]
    if context is not None:
        terms = _context_terms(context)
        ranked = sorted(recs, key=lambda r: (-_context_score(r, terms), r["name"]))
        # Blender API mistakes are cross-cutting and expensive; retain that one discovery
        # line even when a ticket is about composition or modelling. Everything else must
        # earn its system-prompt space from the current layer's words.
        selected = [r for r in ranked if _context_score(r, terms) > 0][:max(0, limit - 1)]
        api = next((r for r in recs if r["name"] == "blender-5-api"), None)
        if api and api not in selected:
            selected.append(api)
        recs = selected[:limit]
    if not recs:
        return ""
    lines = [f"  - {r['name']} — {r['when']}" for r in recs if r["when"]]
    return ("RECIPE COOKBOOK — vetted techniques. Call find_recipe(<name>) for a "
            f"section index, then pull only <name>#<section> BEFORE hand-rolling any of these ({len(lines)}):\n"
            + "\n".join(lines))


def log_recipe_use(shot_folder, names: list[str]) -> None:
    """Append which recipes a build actually pulled.

    Without this we cannot promote or prune: a recipe used in every build should graduate
    into a bvfx_* helper (zero context, impossible to mis-adapt), and one never pulled in
    N builds is dead weight behind an index line."""
    if not names:
        return
    p = run_artifacts.logs_dir(shot_folder) / "recipe_use.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(UTC).isoformat(timespec="seconds"),
                             "recipes": sorted(set(names))}) + "\n")


def recipe_search_response(query: str, hits: Sequence[dict]) -> tuple[str, list[str]]:
    """Return summaries, a compact exact-name index, or one requested section.

    Tool results are replayed into every later model call. A fuzzy query whose top hit
    happened to be the 10KB Blender API recipe used to inject that whole body even when
    the caller was merely discovering whether a motion-blur recipe existed.
    """
    if not hits:
        return "", []
    raw_base, separator, raw_selector = query.lower().partition("#")
    normalized = "-".join(re.findall(r"[a-z0-9]+", raw_base))
    selector = "-".join(re.findall(r"[a-z0-9]+", raw_selector)) if separator else ""
    exact = next((hit for hit in hits if str(hit["name"]).lower() == normalized), None)
    if exact is not None:
        sections = recipe_sections(str(exact["body"]))
        if selector == "full":
            text = f"### {exact['name']}#full  (when: {exact['when']})\n{exact['body']}"
            used = [str(exact["name"])]
        elif selector:
            section = next((row for row in sections if row["id"] == selector), None)
            if section is None:
                available = ", ".join(row["id"] for row in sections)
                return (
                    f"unknown recipe section {selector!r} for {exact['name']}; "
                    f"available: {available}, full",
                    [],
                )
            text = (
                f"### {exact['name']}#{section['id']}  (when: {exact['when']})\n"
                f"{section['body']}"
            )
            used = [str(exact["name"])]
        else:
            lines = "\n".join(
                f"  · {row['id']} ({len(row['body'])} chars) — {row['title']}"
                for row in sections
            )
            text = (
                f"### {exact['name']}  (when: {exact['when']})\n"
                f"Recipe body: {len(exact['body'])} chars. Load only the section you need "
                f"with find_recipe({exact['name']!r} + '#<section>'):\n{lines}\n"
                f"Use {exact['name']}#full only when several indexed sections are jointly required."
            )
            used = []
        rest = [hit for hit in hits if hit is not exact]
        if rest:
            more = "\n".join(f"  · {hit['name']} — {hit['when']}" for hit in rest)
            text += (
                f"\n\n---\nAlso matched ({len(rest)}); query by exact name for its section index:\n"
                f"{more}"
            )
        return text, used
    lines = "\n".join(f"  · {hit['name']} — {hit['when']}" for hit in hits)
    return (
        "Ranked recipe matches (summaries only). Query one exact recipe name to load its "
        f"compact section index:\n{lines}",
        [],
    )


def recipe_sections(body: str) -> list[dict[str, str]]:
    """Stable prose/code sections for bounded recipe retrieval."""
    parts = [part.strip() for part in re.split(r"(```[\s\S]*?```)", body) if part.strip()]
    sections: list[dict[str, str]] = []
    prose_index = code_index = 0
    for part in parts:
        is_code = part.startswith("```") and part.endswith("```")
        if is_code:
            code_index += 1
            section_id = f"snippet-{code_index}"
            content = part[3:-3].strip()
            lines = [line.strip("# ") for line in content.splitlines() if line.strip()]
            title = next((line for line in lines if not re.fullmatch(r"[a-z0-9_+-]+", line.lower())), "code")
        else:
            prose_index += 1
            section_id = f"notes-{prose_index}"
            lines = [line.strip("# ") for line in part.splitlines() if line.strip()]
            title = lines[0] if lines else "notes"
        sections.append(
            {
                "id": section_id,
                "title": re.sub(r"\s+", " ", title)[:120],
                "body": part,
            }
        )
    return sections or [{"id": "notes-1", "title": "recipe", "body": body}]


class RecipeContextBudget:
    """Bound body fragments injected into one model session.

    Indexes and fuzzy summaries remain available for discovery. Body retrieval is the
    expensive, replayed context, so repeated sections and a fourth body are refused.
    """

    def __init__(self, max_bodies: int = 3, max_chars: int = 12_000):
        self.max_bodies = int(max_bodies)
        self.max_chars = int(max_chars)
        self.loaded: dict[str, int] = {}

    def admit(self, key: str, chars: int) -> str:
        if key in self.loaded:
            return (
                f"RECIPE CONTEXT REFUSED: {key} is already loaded in this session. "
                "Use the existing fragment; rereading it adds no authority."
            )
        if len(self.loaded) >= self.max_bodies or sum(self.loaded.values()) + chars > self.max_chars:
            present = ", ".join(self.loaded) or "(none)"
            return (
                "RECIPE CONTEXT BUDGET EXHAUSTED: loaded body fragments "
                f"{present}. Act with the typed tools and measurements already available; "
                "if a specific missing fact blocks progress, name that missing instrument "
                "instead of reconstructing more cookbook entries."
            )
        self.loaded[key] = int(chars)
        return ""
