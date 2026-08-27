"""Semantic role alphabet and matching — one implementation, every caller.

A *tag* is one dotted token on one host. A *selector* is a fnmatch pattern over tags.
Commas are not membership. Contracts, ``bvfx_role()``, and builder scene tools must
not grow a private copy of this matching (ADR-0003).

The Blender worker loads this file by path so its interpreter never imports the
``vfx_harness`` package.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping, Sequence

ROLE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")

_SELECTOR_KEYS = (
    "roles",
    "control_roles",
    "material_roles",
    "compare_roles",
    "compare_control_roles",
    "node_roles",
    "node_group_roles",
    "from_node_roles",
    "to_node_roles",
)


def validate_role_token(role) -> str:
    """A host carries one role. CSV, whitespace, and wildcards are not a tag.

    Run 20260825T143912Z-0b5ab4 stored ``'cam.blockout_fg,cam.blockout_depth_tiers'``
    as a single custom-property string that matched neither contract.
    """
    text = str(role or "").strip()
    if not text or not ROLE_TOKEN.fullmatch(text):
        raise ValueError(
            "bvfx_role: role must be one dotted token matching [A-Za-z0-9._-]+; "
            f"got {role!r} — commas are not membership; tag one role per host or split hosts"
        )
    return text


def match_semantic(value, patterns) -> bool:
    """Whether ``value`` matches any fnmatch pattern. Empty pattern lists miss."""
    if isinstance(patterns, str):
        patterns = [patterns]
    if not patterns:
        return False
    text = str(value or "")
    return any(fnmatch.fnmatchcase(text, str(pattern)) for pattern in patterns)


def format_object_miss(
    *,
    inventory: Sequence[Mapping[str, str]],
    role: str | None = None,
    name: str | None = None,
) -> str:
    """HIR-0018: a miss names the request and the names *and* roles that exist."""
    present_roles = sorted({str(row.get("role") or "") for row in inventory if row.get("role")})
    present_names = sorted(str(row.get("name") or "") for row in inventory if row.get("name"))
    roles_s = ", ".join(present_roles) or "(none)"
    names_s = ", ".join(present_names) or "(none)"
    if role:
        return (
            f"role {role!r} matched no objects; object roles present: {roles_s}; "
            f"names present: {names_s}"
        )
    return (
        f"no object named {name!r}; names present: {names_s}; "
        f"object roles present: {roles_s} — pass role= for a semantic selector"
    )


def format_object_ambiguous(*, role: str, hits: Sequence[Mapping[str, str]]) -> str:
    """HIR-0041: a shared role is not a miss; name the hosts and the legal next action."""
    listed = ", ".join(f"{row['name']!r} role={row['role']!r}" for row in hits)
    return (
        f"role {role!r} matched {len(hits)} objects ({listed}); "
        "a single-subject check needs exactly one host — pass object= with one of those names. "
        "An exact role string is already what you passed; sharing a role is not a selector miss"
    )


def pick_objects(
    inventory: Sequence[Mapping[str, str]],
    *,
    role: str | None = None,
    name: str | None = None,
) -> list[Mapping[str, str]]:
    """Filter an inventory. Caller enforces exactly-one for single-subject tools."""
    has_role = bool(role)
    has_name = bool(name)
    if has_role == has_name:
        raise ValueError("pass role= or object=, not both")
    if name:
        return [row for row in inventory if str(row.get("name") or "") == name]
    return [row for row in inventory if match_semantic(row.get("role"), [role])]


def selector_token_error(token: str, key: str) -> str | None:
    """Authoring lint: a selector may be a pattern, never a comma-joined list."""
    text = str(token)
    if "," in text or any(ch.isspace() for ch in text):
        return (
            f"{key} token {text!r} is not a selector: commas and whitespace are not "
            "membership. One dotted token per host; a pattern (cam.blockout_*) selects "
            "many hosts"
        )
    return None


def selector_punctuation_error(row: Mapping) -> str | None:
    for key in _SELECTOR_KEYS:
        raw = row.get(key) or []
        tokens: Iterable = [raw] if isinstance(raw, str) else raw
        if not isinstance(tokens, (list, tuple)):
            continue
        for token in tokens:
            if not isinstance(token, str) or not token:
                continue
            error = selector_token_error(token, key)
            if error:
                return error
    return None
