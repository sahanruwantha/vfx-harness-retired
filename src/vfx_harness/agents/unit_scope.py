"""Compiled scope of the active work unit.

Builders were forced to guess object-versus-role, invent sibling roles, and call
``inspect.getsource`` for helpers that ``run_bpy`` already injects. Kickoff and
``CLAUDE.md`` were layer-shaped while contracts are unit-shaped. This module is the
one compiler for that card: mutation surface, bound contracts, claims, judge frames,
and the helper inventory parsed from ``blender/worker.py`` without importing ``bpy``.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from vfx_harness.domain.image_debts import image_contract_debt_cards
from vfx_harness.domain.work_units import WorkUnit

SCHEMA = "vfx-harness.unit-scope/v1"
_WORKER = Path(__file__).resolve().parents[1] / "blender" / "worker.py"


def bound_scene_contract_ids(unit: WorkUnit) -> tuple[str, ...]:
    """Scene-contract ids this unit is answerable for, in claim then context order."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(cid: str) -> None:
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)

    for claim in unit.evaluation.claims:
        for binding in claim.evidence:
            if binding.kind == "scene_contract":
                add(binding.id)
    context = unit.evaluation.composition_context
    if context:
        for cid in context.contract_ids:
            add(cid)
    return tuple(ids)


def _signature(name: str, fn: ast.FunctionDef) -> str:
    args = fn.args
    parts: list[str] = []
    positional = list(args.args)
    defaults = list(args.defaults)
    n_plain = len(positional) - len(defaults)
    for index, arg in enumerate(positional):
        if index >= n_plain:
            parts.append(f"{arg.arg}={ast.unparse(defaults[index - n_plain])}")
        else:
            parts.append(arg.arg)
    if args.vararg is not None:
        parts.append(f"*{args.vararg.arg}")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is None:
            parts.append(arg.arg)
        else:
            parts.append(f"{arg.arg}={ast.unparse(default)}")
    if args.kwarg is not None:
        parts.append(f"**{args.kwarg.arg}")
    return f"{name}({', '.join(parts)})"


def _summary(fn: ast.FunctionDef) -> str:
    doc = ast.get_docstring(fn) or ""
    return doc.split("\n", 1)[0].strip()


@lru_cache(maxsize=1)
def helper_inventory() -> tuple[dict[str, str], ...]:
    """Every ``bvfx_*`` name injected into ``run_bpy``. Parsed from worker.py, not copied."""
    source = _WORKER.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_WORKER))
    impls = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    mapping: ast.Dict | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_HELPERS":
                if not isinstance(node.value, ast.Dict):
                    raise ValueError("worker.py _HELPERS must be a dict literal")
                mapping = node.value
    if mapping is None:
        raise ValueError("worker.py has no _HELPERS dict")
    helpers: list[dict[str, str]] = []
    for key, value in zip(mapping.keys, mapping.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise ValueError("worker.py _HELPERS keys must be string literals")
        if not isinstance(value, ast.Name):
            raise ValueError(f"worker.py _HELPERS[{key.value!r}] must name a function")
        fn = impls.get(value.id)
        if fn is None:
            raise ValueError(f"worker.py _HELPERS[{key.value!r}] names unknown {value.id}")
        helpers.append(
            {
                "name": key.value,
                "impl": value.id,
                "signature": _signature(key.value, fn),
                "summary": _summary(fn),
            }
        )
    return tuple(helpers)


def _contract_row(row: Mapping[str, Any]) -> dict[str, Any]:
    frames = row.get("frames")
    out: dict[str, Any] = {
        "id": str(row.get("id") or ""),
        "kind": row.get("kind"),
        "roles": list(row.get("roles") or []),
        "frame": row.get("frame"),
    }
    if isinstance(frames, list):
        out["frames"] = list(frames)
    return out


def compile_unit_scope(
    *,
    unit: WorkUnit,
    layer_id: str,
    contracts: Sequence[Mapping[str, Any]],
    helpers: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Project the active unit onto a queryable card. Sibling units are not inputs."""
    by_id = {
        str(row["id"]): row
        for row in contracts
        if isinstance(row, Mapping) and row.get("id")
    }
    bound = bound_scene_contract_ids(unit)
    missing = [cid for cid in bound if cid not in by_id]
    if missing:
        present = sorted(by_id) or ["(none)"]
        raise ValueError(
            f"unit {unit.id} binds unknown scene contracts {missing}; "
            f"present: {', '.join(present)}"
        )
    inventory = tuple(helpers) if helpers is not None else helper_inventory()
    image_debts = [card.as_dict() for card in image_contract_debt_cards(unit)]
    return {
        "schema": SCHEMA,
        "unit_id": unit.id,
        "layer_id": str(layer_id),
        "title": unit.title,
        "mutates": {
            "mode": unit.mutates.mode,
            "roles": list(unit.mutates.roles),
            "controls": list(unit.mutates.controls),
            "dresses": list(unit.mutates.dresses),
            "script_spans": list(unit.mutates.script_spans),
        },
        "provides": list(unit.provides),
        "look_capabilities": list(unit.look_capabilities),
        "judge": {
            "primary": unit.evaluation.primary_judge,
            "frames": [
                {"frame": point.frame, "ref": point.ref} for point in unit.evaluation.judges
            ],
        },
        "claims": [
            {
                "id": claim.id,
                "authority": claim.authority,
                "required": claim.required,
                "proposition": claim.proposition,
                "moments": list(claim.moments),
                "evidence": [
                    {"kind": binding.kind, "id": binding.id} for binding in claim.evidence
                ],
            }
            for claim in unit.evaluation.claims
        ],
        "contracts": [_contract_row(by_id[cid]) for cid in bound],
        "image_debts": image_debts,
        "helpers": [dict(row) for row in inventory],
    }


def compile_unit_scope_for_shot(shot, unit: WorkUnit, layer_id: str) -> dict[str, Any]:
    from vfx_harness.evidence.scene_checks import load_rows

    return compile_unit_scope(
        unit=unit, layer_id=str(layer_id), contracts=load_rows(shot.folder)
    )


def _format_bound_contract(row: Mapping[str, Any]) -> str:
    if row.get("frame") is not None:
        frame = str(row["frame"])
    else:
        frame = ",".join(str(item) for item in row.get("frames") or []) or "—"
    roles = ",".join(row.get("roles") or []) or "—"
    return f"  - `{row['id']}` kind={row.get('kind')} roles={roles} frame={frame}"


def format_unit_scope_card(card: Mapping[str, Any]) -> str:
    """Compact kickoff/CLAUDE.md projection of the same JSON ``unit_scope`` returns."""
    mutates = card.get("mutates") or {}
    judge = card.get("judge") or {}
    frames = ", ".join(
        f"f{row['frame']}" for row in (judge.get("frames") or []) if isinstance(row, Mapping)
    )
    claims = "\n".join(
        f"  - `{row['id']}` ({row.get('authority')}): {row.get('proposition')}"
        for row in (card.get("claims") or [])
        if isinstance(row, Mapping)
    ) or "  - (none)"
    contracts = "\n".join(
        _format_bound_contract(row)
        for row in (card.get("contracts") or [])
        if isinstance(row, Mapping)
    ) or "  - (none bound)"
    debts = "\n".join(
        f"  - `{row['id']}` frame={row.get('frame')} property={row.get('property')} "
        f"axis={row.get('axis')}"
        for row in (card.get("image_debts") or [])
        if isinstance(row, Mapping)
    ) or "  - (none)"
    helpers = "\n".join(
        f"  - `{row['signature']}`"
        for row in (card.get("helpers") or [])
        if isinstance(row, Mapping) and row.get("signature")
    )
    return (
        f"UNIT SCOPE CARD — compiled from the active work unit "
        f"`{card.get('unit_id')}` on layer {card.get('layer_id')}. "
        f"Query `unit_scope` for this JSON. Do not inspect.getsource helpers, "
        f"and do not guess a sibling unit's roles.\n"
        f"mutates.roles: {', '.join(mutates.get('roles') or []) or 'none'}\n"
        f"mutates.controls: {', '.join(mutates.get('controls') or []) or 'none'}\n"
        f"mutates.dresses: {', '.join(mutates.get('dresses') or []) or 'none'}\n"
        f"mutates.script_spans: {', '.join(mutates.get('script_spans') or []) or 'none'}\n"
        f"judge frames: {frames or 'none'} (primary f{judge.get('primary')})\n"
        f"claims:\n{claims}\n"
        f"bound scene contracts:\n{contracts}\n"
        f"owed image-contract debts (propose_checks, exact id/frame/property/axis):\n"
        f"{debts}\n"
        f"run_bpy helpers (injected):\n{helpers}"
    )
