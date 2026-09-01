"""Compiled scope of the active work unit.

Builders were forced to guess object-versus-role, invent sibling roles, and call
``inspect.getsource`` for helpers that ``run_bpy`` already injects. Kickoff and
``CLAUDE.md`` were layer-shaped while contracts are unit-shaped. This module is the
one compiler for that card: mutation surface, bound contracts, claims, judge frames,
and the helper inventory parsed from ``blender/worker.py`` without importing ``bpy``.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vfx_harness.domain.atomicity import residual_instrument_family, write_clusters
from vfx_harness.domain.image_debts import image_contract_debt_cards
from vfx_harness.domain.publish_interfaces import compile_unit_publish_interfaces
from vfx_harness.domain.unit_completion_receipts import UnitCompletionReceipt
from vfx_harness.domain.work_units import WorkUnit, bound_claim_contract_ids
from vfx_harness.evidence.scene_checks import deferred_subject_composition_forecast_ids_for_unit, load_rows
from vfx_harness.orchestration.unit_completion_authorizations import (
    AuthorizedUnitCompletionSet,
)
from vfx_harness.orchestration.unit_state import unit_digest as digest_of
from vfx_harness.orchestration.unit_state_identity import authorized_passed_unit_ids

if TYPE_CHECKING:
    from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority

SCHEMA = "vfx-harness.unit-scope/v1"
INTERFACE_SCHEMA = "vfx-harness.unit-interface/v1"
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
    returns = f" -> {ast.unparse(fn.returns)}" if fn.returns is not None else ""
    return f"{name}({', '.join(parts)}){returns}"


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


def helpers_for_route(
    inventory: Sequence[Mapping[str, str]],
    route: str,
) -> tuple[dict[str, str], ...]:
    """Hide the helper that would reopen a second construction authority."""
    hide = "bvfx_import_asset" if route == "generate" else "bvfx_import_construction"
    return tuple(dict(row) for row in inventory if row.get("name") != hide)


def _contract_row(row: Mapping[str, Any]) -> dict[str, Any]:
    # This is already the exact, active-unit closure. Keep every evaluator field so
    # the builder sees socket, graph, node/material selectors, paths, and bounds rather
    # than rediscovering them through failed tags. Boundedness comes from selecting only
    # bound ids, not from amputating the selected contracts.
    return dict(row)


def compile_unit_scope(
    *,
    unit: WorkUnit,
    layer_id: str,
    contracts: Sequence[Mapping[str, Any]],
    helpers: Sequence[Mapping[str, str]] | None = None,
    authored_publishes: Any = None,
    unit_digest: str = "",
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
    inventory = helpers_for_route(inventory, unit.construction.route)
    image_debts = [card.as_dict() for card in image_contract_debt_cards(unit)]
    clusters = write_clusters(unit, contracts)
    publish_family = (
        clusters[0].instrument_family
        if len(clusters) == 1
        else residual_instrument_family(unit)
    )
    publish_interfaces = [
        interface.as_dict()
        for interface in compile_unit_publish_interfaces(
            unit,
            layer_id=str(layer_id),
            bound_contract_ids=bound_claim_contract_ids(unit),
            authored=authored_publishes,
            instrument_family=publish_family,
            unit_digest=unit_digest,
        )
    ]
    return {
        "schema": SCHEMA,
        "unit_id": unit.id,
        "layer_id": str(layer_id),
        "title": unit.title,
        "mutates": {
            "mode": unit.mutates.mode,
            "roles": list(unit.mutates.roles),
            "controls": list(unit.mutates.controls),
            "control_roles": {
                control: list(roles) for control, roles in unit.mutates.control_roles
            },
            "dresses": list(unit.mutates.dresses),
            "script_spans": list(unit.mutates.script_spans),
        },
        "provides": list(unit.provides),
        "look_capabilities": list(unit.look_capabilities),
        "write_clusters": [
            {
                "role_namespace": cluster.role_namespace,
                "host_class": cluster.host_class,
                "instrument_family": cluster.instrument_family,
            }
            for cluster in clusters
        ],
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
                "axis": claim.axis,
                "property": claim.property,
                "asserts": claim.asserts,
                "repair_owner": claim.repair_owner,
                "subject_roles": list(claim.subject_roles),
                "subject_controls": list(claim.subject_controls),
                "moments": list(claim.moments),
                "evidence": [
                    {"kind": binding.kind, "id": binding.id} for binding in claim.evidence
                ],
            }
            for claim in unit.evaluation.claims
        ],
        "contracts": [_contract_row(by_id[cid]) for cid in bound],
        "image_debts": image_debts,
        "diagnostic_instruments": ([{
            "name": "inspect_view",
            "purpose": "orbit/elevation/solo inspection of owned form by semantic role",
            "views": ["through_camera", "orbit", "front", "right", "back", "left", "top"],
            "acceptance_evidence": False,
        }] if "geometry" in unit.provides else []),
        "publish_interfaces": publish_interfaces,
        "producer_unit_digest": str(unit_digest or ""),
        "consumes": [
            {
                "producer": item.producer,
                "interface_id": item.interface_id,
                "kind": item.kind,
            }
            for item in unit.consumes
        ],
        "construction": {
            "route": unit.construction.route,
            "witnesses": list(unit.construction.witnesses),
        },
        "helpers": [dict(row) for row in inventory],
    }


def compile_predecessor_interface(
    card: Mapping[str, Any],
    *,
    producer_digest: str = "",
    durable_hash: str = "",
    durable_status: str = "passed",
    completion_authorized: bool = False,
    allowed_interface_keys: frozenset[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Project a passed dependency onto the interface its consumers may need."""
    mutates = card.get("mutates") or {}
    claims = card.get("claims") or []
    contracts = card.get("contracts") or []
    image_debts = card.get("image_debts") or []
    published = [
        dict(row)
        for row in (card.get("publish_interfaces") or [])
        if isinstance(row, Mapping)
        and (
            allowed_interface_keys is None
            or (str(row.get("id") or ""), str(row.get("kind") or ""))
            in allowed_interface_keys
        )
    ]
    digest_bound = bool(durable_hash or producer_digest)
    digest_matches = not digest_bound or (
        bool(durable_hash)
        and bool(producer_digest)
        and durable_hash == producer_digest
    )
    stale = (
        durable_status != "passed"
        or not digest_matches
        or not completion_authorized
    )
    return {
        "schema": INTERFACE_SCHEMA,
        "unit_id": str(card.get("unit_id") or ""),
        "title": str(card.get("title") or ""),
        "dependency_status": (
            "passed"
            if not stale
            else ("stale" if durable_status == "passed" else (durable_status or "unavailable"))
        ),
        "provides": list(card.get("provides") or []),
        "semantic_roles": list(mutates.get("roles") or []),
        "dressed_surfaces": list(mutates.get("dresses") or []),
        "controls": list(mutates.get("controls") or []),
        "look_capabilities": list(card.get("look_capabilities") or []),
        "sealed_claim_ids": [
            str(row.get("id"))
            for row in claims
            if isinstance(row, Mapping) and row.get("required") and row.get("id")
        ],
        "scene_contract_ids": [
            str(row.get("id"))
            for row in contracts
            if isinstance(row, Mapping) and row.get("id")
        ],
        "image_contract_ids": [
            str(row.get("id"))
            for row in image_debts
            if isinstance(row, Mapping) and row.get("id")
        ],
        "publish_interfaces": [] if stale else published,
        "producer_unit_digest": str(producer_digest or ""),
    }


def compile_scope_with_predecessors(
    *,
    unit: WorkUnit,
    layer_id: str,
    contracts: Sequence[Mapping[str, Any]],
    units: Sequence[WorkUnit] = (),
    durable_state: Mapping[str, Any] | None = None,
    completion_authorization: AuthorizedUnitCompletionSet | None = None,
    helpers: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Active-unit card plus digest-matched predecessor publish interfaces."""

    producer_digest = digest_of(unit)
    card = compile_unit_scope(
        unit=unit,
        layer_id=str(layer_id),
        contracts=contracts,
        helpers=helpers,
        unit_digest=producer_digest,
    )

    contract_by_id = {
        str(row.get("id")): row
        for row in contracts
        if isinstance(row, Mapping) and row.get("id")
    }
    forecast_ids = deferred_subject_composition_forecast_ids_for_unit(
        contracts, units, unit, layer_id
    )
    card["deferred_subject_forecasts"] = [
        {
            **_contract_row(contract_by_id[contract_id]),
            "diagnostic_only": True,
            "acceptance_evidence": False,
        }
        for contract_id in forecast_ids
    ]
    by_id = {item.id: item for item in units}
    consumed_by_producer: dict[str, frozenset[tuple[str, str]]] = {}
    for uid in unit.depends_on:
        consumed_by_producer[uid] = frozenset(
            (item.interface_id, item.kind)
            for item in unit.consumes
            if item.producer == uid
        )
    durable_rows = (durable_state or {}).get("units") or {}
    authorized_predecessors = authorized_passed_unit_ids(
        durable_state or {},
        tuple(units),
        completion_authorization=completion_authorization,
    )
    predecessors: list[dict[str, Any]] = []
    for uid in unit.depends_on:
        producer = by_id.get(uid)
        if producer is None:
            continue
        row = durable_rows.get(uid) or {}
        pred_digest = digest_of(producer)
        completion_authorized = False
        if row.get("status") == "passed":
            receipt = UnitCompletionReceipt.parse(
                row.get("completion_receipt"),
                f"unit scope predecessor {layer_id}.{uid} completion receipt",
            )
            completion_authorized = (
                uid in authorized_predecessors
                and completion_authorization is not None
                and completion_authorization.receipt_digest(uid)
                == receipt.receipt_digest
            )
        predecessors.append(
            compile_predecessor_interface(
                compile_unit_scope(
                    unit=producer,
                    layer_id=str(layer_id),
                    contracts=contracts,
                    helpers=(),
                    unit_digest=pred_digest,
                ),
                producer_digest=pred_digest,
                durable_hash=str(row.get("unit_hash") or ""),
                durable_status=str(row.get("status") or ""),
                completion_authorized=completion_authorized,
                allowed_interface_keys=consumed_by_producer[uid],
            )
        )
    card["predecessor_interfaces"] = predecessors
    card["predecessor_publish_interfaces"] = [
        dict(row)
        for pred in predecessors
        for row in (pred.get("publish_interfaces") or [])
        if isinstance(row, Mapping)
    ]
    return card


def compile_unit_scope_for_shot(
    shot,
    unit: WorkUnit,
    layer_id: str,
    *,
    units: Sequence[WorkUnit] = (),
    durable_state: Mapping[str, Any] | None = None,
    completion_authorization: AuthorizedUnitCompletionSet | None = None,
    selected_authority: ResolvedSelectedAuthority | None = None,
) -> dict[str, Any]:

    return compile_scope_with_predecessors(
        unit=unit,
        layer_id=str(layer_id),
        contracts=load_rows(shot.folder, selected_authority),
        units=units,
        durable_state=durable_state,
        completion_authorization=completion_authorization,
    )


def _format_bound_contract(row: Mapping[str, Any]) -> str:
    # These rows are already the exact active-unit closure. Preserve every evaluator
    # selector/bound in kickoff so graph, socket, data_path, and node-role details do not
    # require a failed implementation before the model discovers them through unit_scope.
    return "  - " + json.dumps(dict(row), sort_keys=True, separators=(",", ":"))


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
    forecasts = "\n".join(
        _format_bound_contract(row)
        for row in (card.get("deferred_subject_forecasts") or [])
        if isinstance(row, Mapping)
    ) or "  - (none)"
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
    fault_owners = "\n".join(
        f"  - `{row.get('id')}`"
        + (f" layer={row.get('layer')}" if row.get("layer") else "")
        + f" roles={','.join(row.get('roles') or []) or 'none'} "
        f"controls={','.join(row.get('controls') or []) or 'none'}"
        for row in (card.get("fault_owner_options") or [])
        if isinstance(row, Mapping) and row.get("id")
    ) or "  - (none; the defect is local or requires plan authority)"
    interfaces = "\n".join(
        f"  - `{row.get('id')}` kind={row.get('kind')} exports="
        + json.dumps(row.get("exports") or {}, sort_keys=True, separators=(",", ":"))
        for row in (card.get("publish_interfaces") or [])
        if isinstance(row, Mapping) and row.get("id")
    ) or "  - (none)"
    consumes = "\n".join(
        f"  - producer=`{row.get('producer')}` interface=`{row.get('interface_id')}` "
        f"kind={row.get('kind')}"
        for row in (card.get("consumes") or [])
        if isinstance(row, Mapping) and row.get("interface_id")
    ) or "  - (none)"
    predecessors = "\n".join(
        f"  - `{row.get('id')}` kind={row.get('kind')} producer="
        f"{(row.get('producer') or {}).get('unit_id', '')} digest="
        f"{(row.get('producer') or {}).get('unit_digest', '') or card.get('producer_unit_digest') or ''} "
        f"exports="
        + json.dumps(row.get("exports") or {}, sort_keys=True, separators=(",", ":"))
        for row in (card.get("predecessor_publish_interfaces") or [])
        if isinstance(row, Mapping) and row.get("id")
    ) or "  - (none)"
    return (
        f"UNIT SCOPE CARD — compiled from the active work unit "
        f"`{card.get('unit_id')}` on layer {card.get('layer_id')}. "
        f"Query `unit_scope` for this JSON. Do not inspect.getsource helpers, "
        f"and do not guess a sibling unit's roles.\n"
        f"producer_unit_digest: {card.get('producer_unit_digest') or 'none'}\n"
        f"mutates.roles: {', '.join(mutates.get('roles') or []) or 'none'}\n"
        f"mutates.controls: {', '.join(mutates.get('controls') or []) or 'none'}\n"
        f"mutates.dresses: {', '.join(mutates.get('dresses') or []) or 'none'}\n"
        f"mutates.script_spans: {', '.join(mutates.get('script_spans') or []) or 'none'}\n"
        f"judge frames: {frames or 'none'} (primary f{judge.get('primary')})\n"
        f"claims:\n{claims}\n"
        f"bound scene contracts:\n{contracts}\n"
        "deferred subject forecasts (diagnostic only; the complete-subject payer "
        f"alone can satisfy these rows):\n{forecasts}\n"
        f"owed image-contract debts (propose_checks, exact id/frame/property/axis):\n"
        f"{debts}\n"
        f"typed publish interfaces (role/control/contract-id exports only):\n"
        f"{interfaces}\n"
        f"declared consumes:\n{consumes}\n"
        f"construction.route: {card.get('construction', {}).get('route') or 'procedural'}\n"
        f"construction.witnesses: "
        f"{', '.join((card.get('construction') or {}).get('witnesses') or []) or 'none'}\n"
        f"predecessor publish interfaces (digest-matched, no producer scripts):\n"
        f"{predecessors}\n"
        f"legal upstream fault owners for cannot_express_in_scope:\n{fault_owners}\n"
        f"run_bpy helpers (injected):\n{helpers}"
    )
