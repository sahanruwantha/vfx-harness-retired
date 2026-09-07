"""Recipe discovery and scope refusals shared by model transports."""

import re
from dataclasses import dataclass

from vfx_harness.knowledge import recipes


@dataclass(frozen=True)
class RecipeLookup:
    text: str
    used: tuple[str, ...]
    status: str


def fragment_key(query: str, used: tuple[str, ...]) -> str:
    """Canonical retrieval identity, independent of aliases in the search query."""
    _base, separator, selector = query.lower().partition("#")
    key = ",".join(used)
    if separator:
        key += "#" + "-".join(re.findall(r"[a-z0-9]+", selector))
    return key


def lookup(query: str, roles: tuple[str, ...] | None) -> RecipeLookup:
    hits = recipes.search_recipes(query, mutation_roles=roles)
    if not hits:
        blocked = recipes.out_of_scope_hits(query, roles or ()) if roles is not None else []
        if blocked:
            names = ", ".join(rec["name"] for rec in blocked)
            present = ", ".join(roles) if roles else "(none)"
            return RecipeLookup(
                "ABSTAIN: no in-scope recipe for that query. Keyword hits "
                f"{names} require mutation roles this unit does not own. "
                f"Requested query {query!r}; mutation roles present: "
                f"{present}. Do not improvise those techniques here — they "
                "belong to a unit that mutates the missing roles.", (), "out_of_scope",
            )
        return RecipeLookup(
            "no matching recipe — improvise; a good solution may be harvested "
            "into the cookbook if this milestone passes.", (), "not_found",
        )
    text, used = recipes.recipe_search_response(query, hits)
    return RecipeLookup(text, tuple(used), "found")
