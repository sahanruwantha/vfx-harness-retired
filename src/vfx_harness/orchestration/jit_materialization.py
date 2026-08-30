"""Pinned materialization of one globally deferred layer.

The global bundle reserves dependency and semantic authority without inventing future
units.  A JIT materialization may replace exactly one ``jit_deferred`` layer with a full
ready DAG and add its contracts, but only after every promised contract is concretely
bound.  The resulting consumer view is content-addressed and pinned to the selected
global bundle; it never mutates that bundle.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vfx_harness.domain.json_pointer import encode as json_ptr
from vfx_harness.domain.json_pointer import format_finding
from vfx_harness.domain.json_pointer import set_at as set_pointer
from vfx_harness.domain.json_pointer import split as split_pointer
from vfx_harness.domain.plan_records import load_active_structured_decisions
from vfx_harness.evidence.scene_checks import (
    KIND_DOMAINS,
    PROJECTED_ORIGIN_KINDS,
    SURFACE_PROJECTED_KINDS,
    validate_row,
    validate_row_set,
)
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.ledger import Layer, load_layers_from_path

MATERIALIZATION_SCHEMA = "vfx-harness.jit-layer-materialization/v1"
VIEW_SCHEMA = "vfx-harness.jit-layer-view/v1"
STATE_DIR = Path("state/jit-layers")
CURRENT = STATE_DIR / "current.json"
OVERLAY_ARTIFACTS = (
    "layers.json",
    "scene_checks.json",
    "checks.json",
    "requirements.json",
    "acceptance.json",
)
ROLE_SELECTOR_CLOSURE_RULE = (
    "required evidence and repair authority must close together: bind the contract "
    "on the unit that mutates or dresses those roles, or use typed control_roles/"
    "compare_control_roles for bvfx_control ids. A mutation-empty observer cannot "
    "pay bbox or other role selectors it does not own"
)
TWO_SIDED_MEASUREMENT_KINDS = frozenset({
    "path_clearance_min",
    "parallax_displacement_profile",
    "onset_order",
})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


class MaterializationRevisionConflict(ValueError):
    """The candidate changed after the caller observed it."""


@dataclass(frozen=True, slots=True)
class UnstagedMaterializationUnit:
    """One revision-checked removal from unpublished materialization scratch."""

    unit_id: str
    removed_contract_ids: tuple[str, ...]
    removed_requirement_ids: tuple[str, ...]


FINALIZATION_SCHEMA = "vfx-harness.materialization-finalization/v1"


def materialization_candidate_revision(path: str | Path) -> str:
    """Return the byte revision used by candidate compare-and-swap writes."""
    return _sha256(Path(path))


def materialization_finalization_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate.with_name(candidate.name + ".finalization.json")


def attest_materialization_finalization(
    path: str | Path,
    *,
    bundle_hash: str,
) -> Path:
    """Bind an explicit successful finalize call to the exact candidate revision."""
    candidate = Path(path)
    attestation = materialization_finalization_path(candidate)
    atomic_write(
        attestation,
        json.dumps(
            {
                "schema": FINALIZATION_SCHEMA,
                "bundle_hash": str(bundle_hash),
                "candidate_revision": materialization_candidate_revision(candidate),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return attestation


def materialization_finalization_attested(
    path: str | Path,
    *,
    bundle_hash: str,
) -> bool:
    """True only for the current bytes explicitly accepted by finalization."""
    candidate = Path(path)
    attestation = materialization_finalization_path(candidate)
    if not candidate.is_file() or not attestation.is_file():
        return False
    try:
        payload = json.loads(attestation.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("schema") == FINALIZATION_SCHEMA
        and payload.get("bundle_hash") == str(bundle_hash)
        and payload.get("candidate_revision")
        == materialization_candidate_revision(candidate)
    )


@contextmanager
def _materialization_candidate_lock(path: Path) -> Iterator[None]:
    """Serialize candidate read/validate/write across sessions and processes."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _mutate_materialization_candidate(
    path: Path,
    mutate: Callable[[dict[str, Any]], None],
    *,
    expected_revision: str | None,
) -> tuple[dict[str, Any], str]:
    """Run one locked compare-and-swap candidate mutation."""
    with _materialization_candidate_lock(path):
        raw = path.read_bytes()
        actual_revision = hashlib.sha256(raw).hexdigest()
        if expected_revision is not None and expected_revision != actual_revision:
            raise MaterializationRevisionConflict(
                "materialization candidate revision changed: expected "
                f"{expected_revision}, found {actual_revision}; inspect status and restart "
                "the materialization session from the current candidate"
            )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"{path} must contain an object")
        mutate(payload)
        encoded = (json.dumps(payload, indent=1) + "\n").encode("utf-8")
        atomic_write(path, encoded.decode("utf-8"))
        return payload, hashlib.sha256(encoded).hexdigest()


def _rows(document: dict[str, Any], key: str, where: str) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{where}.{key} must be a list of objects")
    return value


def _matches_reserved(role: str, reserved: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(role, pattern) or fnmatch.fnmatchcase(pattern, role)
        for pattern in reserved
    )


def _passed_evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        identifier = value.get("id")
        if identifier and value.get("pass") is True:
            found.add(str(identifier))
        for child in value.values():
            found.update(_passed_evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_passed_evidence_ids(child))
    return found


def _require_upstream_outcomes(shot: Path, layer: Layer) -> None:
    if layer.jit is None:
        raise ValueError(f"layer {layer.id} has no deferred JIT authority")
    passed: set[str] = set()
    for dependency in layer.jit.depends_on_layers:
        path = shot / "plans" / "outcomes" / f"{int(dependency):02d}.json"
        try:
            outcome = _document(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"layer {layer.id} cannot materialize before dependency {dependency} has a sealed outcome"
            ) from exc
        if outcome.get("status") != "passed" or str(outcome.get("layer")) != dependency:
            raise ValueError(
                f"layer {layer.id} dependency {dependency} does not have a passed sealed outcome"
            )
        passed.update(_passed_evidence_ids(outcome))
    missing = sorted(identifier for _kind, identifier in layer.jit.required_outcomes if identifier not in passed)
    if missing:
        raise ValueError(
            f"layer {layer.id} required upstream outcomes have not passed: " + ", ".join(missing)
        )


@dataclass(frozen=True, slots=True)
class MaterializedLayer:
    layer: Layer
    layer_row: dict[str, Any]
    scene_contracts: tuple[dict[str, Any], ...]
    image_contracts: tuple[dict[str, Any], ...]
    requirement_bindings: dict[str, tuple[str, ...]]
    requirement_decisions: dict[str, dict[str, str]]
    requirement_evidence_domains: dict[str, tuple[str, ...]]
    requirement_domain_bindings: dict[str, tuple[dict[str, Any], ...]]
    acceptance: tuple[dict[str, Any], ...]


def validate_materialization(
    global_root: str | Path,
    materialization_path: str | Path,
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
    base_scene_checks_path: str | Path | None = None,
    resolutions_path: str | Path | None = None,
    base_requirements_path: str | Path | None = None,
) -> MaterializedLayer:
    """Validate one overlay without publishing or creating durable unit state.

    `resolutions_path` names the durable decision ledger; bundles do not freeze it, so
    production callers must pass the shot's `state/plan-resolutions.jsonl` explicitly or
    decision adoption cannot be enforced. `base_requirements_path` names the register the
    closure is judged against (the evolving selected view during production; the bundle's
    own register by default)."""
    root = Path(global_root)
    source = Path(materialization_path)
    payload = _document(source)
    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError(f"{source} has unsupported JIT materialization schema")
    if payload.get("bundle_hash") != expected_bundle_hash:
        raise ValueError("JIT materialization is pinned to another global bundle")
    layer_row = payload.get("layer")
    if not isinstance(layer_row, dict):
        raise ValueError("JIT materialization.layer must be an object")
    layer_id = str(layer_row.get("id") or "")

    global_layers = _rows(_document(root / "layers.json"), "layers", "layers.json")
    by_id = {str(row.get("id")): row for row in global_layers}
    global_row = by_id.get(layer_id)
    if global_row is None or global_row.get("execution") != "jit_deferred":
        raise ValueError(f"layer {layer_id!r} is not selected jit_deferred authority")

    findings: list[str] = []

    def note(pointer: str, message: str) -> None:
        findings.append(format_finding(pointer, message))

    if layer_row.get("execution") not in {None, "ready"}:
        note(json_ptr("layer", "execution"), "materialized layer execution must be ready")
    layer_row = dict(layer_row)
    layer_row["execution"] = "ready"
    layer_row.pop("jit", None)
    structural = ("id", "script", "title", "primary_judge", "judge", "owns", "evidence_domains", "reads")
    changed = [key for key in structural if layer_row.get(key) != global_row.get(key)]
    if changed:
        note(
            json_ptr("layer"),
            "JIT materialization changes global structural authority: " + ", ".join(changed),
        )

    base_document = (
        _document(Path(base_layers_path)) if base_layers_path else _document(root / "layers.json")
    )
    base_layers = (
        _rows(base_document, "layers", "layers.json") if base_layers_path else global_layers
    )
    combined_layers = [layer_row if str(row.get("id")) == layer_id else row for row in base_layers]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".layers.json", prefix=".jit-validate-", dir=root, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        # The combined view keeps the base document's schema: unit-first loader rules
        # (deferred consumers of a freshly-ready dependency keep empty outcomes) must
        # see schema 5, not a forced legacy 4.
        json.dump(
            {"schema": base_document.get("schema", 4), "layers": combined_layers}, handle
        )
    parsed = None
    layer = None
    try:
        parsed = load_layers_from_path(temp_path)
        layer = parsed[layer_id]
    except (ValueError, KeyError, TypeError) as exc:
        note(json_ptr("layer", "stages"), f"layer stages did not parse: {exc}")
    finally:
        temp_path.unlink(missing_ok=True)

    # Dressing closure (ADR-0007): a unit may declare appearance-assignment authority
    # only over selectors some OTHER layer explicitly marked dressable. Exact-string
    # match, not glob-vs-glob: the owner names the surface it exposes, the dresser
    # names the same surface.
    if parsed is not None and layer is not None:
        declared_dressable = {
            selector
            for other in parsed.values()
            if str(other.id) != str(layer_id)
            for selector in other.dressable
        }
        for unit_index, unit in enumerate(layer.stages):
            undeclared_dresses = sorted(set(unit.mutates.dresses) - declared_dressable)
            if undeclared_dresses:
                note(
                    json_ptr("layer", "stages", unit_index, "mutates", "dresses"),
                    f"unit {unit.id} dresses {', '.join(undeclared_dresses)} — no other "
                    "layer declares these selectors dressable; the owning layer's row must "
                    "list them under `dressable` before a dressing unit may claim them",
                )

    # Look scope is typed authority, and silence is not a declaration: a unit that omits
    # the key is indistinguishable from one that declares "no appearance", which is how
    # run 20260823T154920Z left an appearance-owning unit without image feedback. An
    # explicit empty list is the legal way to own no appearance.
    from vfx_harness.domain.work_units import LOOK_CAPABILITIES

    for stage_index, stage in enumerate(layer_row.get("stages") or []):
        if isinstance(stage, dict) and "look_capabilities" not in stage:
            note(
                json_ptr("layer", "stages", stage_index, "look_capabilities"),
                "materialized unit(s) must declare look_capabilities: "
                + str(stage.get("id") or "<unnamed>")
                + f" — a subset of {', '.join(sorted(LOOK_CAPABILITIES))}, or [] to own no "
                "appearance",
            )

    jit = global_row.get("jit") or {}
    raw_global_capabilities = jit.get("provides") or {}
    global_capabilities = (
        {
            str(capability): tuple(str(role) for role in roles)
            for capability, roles in raw_global_capabilities.items()
        }
        if isinstance(raw_global_capabilities, dict)
        else {}
    )
    reserved = tuple(map(str, jit.get("reserved_roles") or []))
    if layer is not None:
        from vfx_harness.domain.work_units import (
            CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
            allowed_unit_provides,
        )

        allowed_capabilities = allowed_unit_provides(global_row)
        unit_capabilities = {
            capability for unit in layer.stages for capability in unit.provides
        }
        missing_capabilities = sorted(set(global_capabilities) - unit_capabilities)
        if missing_capabilities:
            note(
                json_ptr("layer", "stages"),
                "materialized layer does not fulfill globally declared scene "
                "capabilities: " + ", ".join(missing_capabilities),
            )
        for unit_index, unit in enumerate(layer.stages):
            disallowed = sorted(set(unit.provides) - allowed_capabilities)
            if disallowed:
                note(
                    json_ptr("layer", "stages", unit_index, "provides"),
                    f"unit {unit.id} declares capabilities outside sparse global "
                    f"authority: {', '.join(disallowed)}; allowed here: "
                    f"{', '.join(sorted(allowed_capabilities)) or '(none)'}. "
                    + CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
                )
            if "camera" in unit.provides:
                camera_roles = global_capabilities.get("camera", ())
                if not camera_roles:
                    note(
                        json_ptr("layer", "stages", unit_index, "provides"),
                        f"unit {unit.id} declares camera capability, but global layer "
                        f"{layer_id} did not reserve it in jit.provides; camera ownership "
                        "must be decided in the sparse global DAG",
                    )
                elif not any(
                    _matches_reserved(role, camera_roles) for role in unit.mutates.roles
                ):
                    note(
                        json_ptr("layer", "stages", unit_index, "provides"),
                        f"unit {unit.id} declares camera capability without mutating any "
                        "globally reserved camera interface role: "
                        + ", ".join(camera_roles),
                    )
            escaped = sorted(
                role for role in unit.mutates.roles if not _matches_reserved(role, reserved)
            )
            if escaped:
                note(
                    json_ptr("layer", "stages", unit_index, "mutates", "roles"),
                    "materialized roles escape global namespace reservations: "
                    + ", ".join(escaped),
                )

    try:
        scene_rows = _rows(payload, "scene_contracts", "materialization")
    except ValueError as exc:
        note(json_ptr("scene_contracts"), str(exc))
        scene_rows = []
    try:
        image_rows = _rows(payload, "image_contracts", "materialization")
    except ValueError as exc:
        note(json_ptr("image_contracts"), str(exc))
        image_rows = []
    if image_rows:
        note(
            json_ptr("image_contracts"),
            "materialization.image_contracts must be empty; candidate-sensitive image "
            "checks are proposed after the producing unit mutates the cumulative scene",
        )
    seen_ids: dict[str, int] = {}
    for index, row in enumerate(scene_rows):
        cid = str(row.get("id") or "")
        if not cid or cid in seen_ids:
            note(
                json_ptr("scene_contracts", index, "id"),
                "materialized contract ids must be present and unique",
            )
        else:
            seen_ids[cid] = index
    for index, row in enumerate(image_rows):
        cid = str(row.get("id") or "")
        if not cid or cid in seen_ids:
            note(
                json_ptr("image_contracts", index, "id"),
                "materialized contract ids must be present and unique",
            )
        else:
            seen_ids[cid] = index
    all_contracts = {
        str(row.get("id")): ("scene_contract", row) for row in scene_rows if row.get("id")
    }
    all_contracts.update({
        str(row.get("id")): ("image_contract", row) for row in image_rows if row.get("id")
    })
    if layer is not None:
        from vfx_harness.domain.work_units import (
            DEFERRED_CONTRACT_CONTEXT_RULE,
            deferred_claim_binding_gaps,
        )

        unit_index_by_id = {unit.id: index for index, unit in enumerate(layer.stages)}
        for gap in deferred_claim_binding_gaps(layer.stages, scene_rows):
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "evaluation",
                    "claims",
                ),
                f"unit {gap.unit_id} claim {gap.claim_id} directly binds deferred "
                f"scene contract {gap.contract_id} (owner_layer={gap.owner_layer}, "
                f"activates_at={gap.activates_at}). "
                + DEFERRED_CONTRACT_CONTEXT_RULE,
            )
    # HIR-0122: JIT materialization deliberately publishes no candidate-sensitive
    # image rows. Required image bindings on the typed work-unit claims are the
    # authoritative build-time debt catalog until propose_checks can measure a live
    # candidate. Requirement closure must therefore resolve those ids from the same
    # compiler used by builder payment rather than treating the intentionally-empty
    # image_contracts array as absence.
    image_debt_ids: set[str] = set()
    if layer is not None:
        from vfx_harness.domain.image_debts import image_contract_debt_cards

        image_debt_ids = {
            debt.id
            for unit in layer.stages
            for debt in image_contract_debt_cards(unit)
        }
    requirement_contract_ids = set(all_contracts) | image_debt_ids
    for index, row in enumerate(scene_rows):
        error = validate_row(row)
        if error:
            note(
                json_ptr("scene_contracts", index),
                f"scene contract {row.get('id', '<missing>')}: {error}",
            )
        if str(row.get("owner_layer") or row.get("activates_at") or "") != layer_id:
            # Run 20260825 (bb54f1): a correctly-designed cross-layer visibility row
            # died here as owner_layer=2 inside layer 1's materialization, and the bare
            # "must be owned" message cost the whole session. Name the legal form.
            note(
                json_ptr("scene_contracts", index, "owner_layer"),
                f"scene contract {row.get('id')} must be owned by layer {layer_id} "
                f"(it declares owner_layer={row.get('owner_layer')!r}). A contract this "
                "layer authors but a LATER layer evaluates keeps "
                f"owner_layer={layer_id!r} and sets activates_at to the later layer "
                "(fault_owner may still name this layer); only rows this layer owns "
                "may publish here",
            )
    from vfx_harness.domain.work_units import (
        DEFERRED_SUBJECT_ACTIVATION_RULE,
        compile_deferred_subject_activation,
        deferred_subject_activation_gaps,
    )

    activation_card = compile_deferred_subject_activation(global_layers, layer_id)
    for gap in deferred_subject_activation_gaps(activation_card, scene_rows):
        note(
            json_ptr("scene_contracts", gap.index, "activates_at"),
            f"scene contract {gap.contract_id} activates_at={gap.found!r}; compiled "
            f"earliest_geometry_layer is {gap.expected!r}. "
            + DEFERRED_SUBJECT_ACTIVATION_RULE,
        )
    for index, row in enumerate(image_rows):
        if str(row.get("owner_layer") or "") != layer_id:
            note(
                json_ptr("image_contracts", index, "owner_layer"),
                f"image contract {row.get('id')} must be owned by layer {layer_id}",
            )
    for cross_row_finding in validate_row_set(scene_rows):
        note(json_ptr("scene_contracts"), f"scene contract {cross_row_finding}")

    # HIR-0110: candidate-sensitive image debt may be due only after the cumulative
    # dependency closure has a typed way to affect optical signal.  Read earlier-layer
    # rows from the same selected consumer view as base layers; do not infer capability
    # from look labels, role names, or warm Blender state.
    base_scene_path = (
        Path(base_scene_checks_path)
        if base_scene_checks_path is not None
        else root / "scene_checks.json"
    )
    try:
        base_scene_rows = _rows(
            _document(base_scene_path), "contracts", "scene_checks.json"
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        note(
            json_ptr("scene_contracts"),
            f"cannot resolve earlier-layer optical-signal authority: {exc}",
        )
        base_scene_rows = []
    combined_scene_rows = [
        row
        for row in base_scene_rows
        if str(row.get("owner_layer") or "") != layer_id
    ] + list(scene_rows)

    # HIR-0132: a required visibility row activates at its repair-owner unit. Later
    # geometry must depend on and protect that owner; future surfaces are not due early.
    if layer is not None:
        from vfx_harness.domain.work_units import (
            GEOMETRY_VIS_CYCLE_RULE,
            GEOMETRY_VIS_DEPENDENCY_RULE,
            PROJECTED_ORIGIN_REPAIR_RULE,
            geometry_vis_dependency_cycles,
            geometry_vis_dependency_gaps,
            point_projection_interface_gaps,
        )
        from vfx_harness.evidence.scene_checks import (
            deferred_subject_composition_payment_gaps,
        )

        unit_index_by_id = {unit.id: index for index, unit in enumerate(layer.stages)}
        for gap in deferred_subject_composition_payment_gaps(
            combined_scene_rows, layer.stages, layer_id
        ):
            note(
                json_ptr("layer", "stages"),
                f"deferred subject composition {gap.contract_id} selects roles "
                f"{list(gap.roles)} but no geometry unit has every overlapping producer "
                f"{list(gap.producer_ids)} in its dependency closure. Order the truthful "
                "write clusters so the first complete cumulative subject pays the "
                "camera-owned bbox; do not evaluate the row before its subject exists.",
            )
        vis_gaps = geometry_vis_dependency_gaps(layer.stages, scene_rows, layer_id)
        vis_cycles = geometry_vis_dependency_cycles(layer.stages, scene_rows, layer_id)
        cyclic_edges = {edge for cycle in vis_cycles for edge in cycle.edges}
        for cycle in vis_cycles:
            edge_text = ", ".join(f"{source}->{target}" for source, target in cycle.edges)
            note(
                json_ptr("layer", "stages"),
                f"mutual geometry visibility cycle among units {list(cycle.unit_ids)}; "
                f"protected contracts {list(cycle.contract_ids)} select roles "
                f"{list(cycle.roles)}; producer edges are [{edge_text}]. "
                + GEOMETRY_VIS_CYCLE_RULE,
            )
        for gap in vis_gaps:
            if any((gap.unit_id, producer) in cyclic_edges for producer in gap.producer_ids):
                continue
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "provides",
                ),
                f"geometry unit {gap.unit_id} protects visible_fraction "
                f"{gap.contract_id}, but its typed repair owner(s) / role producer(s) "
                f"{list(gap.producer_ids)} are outside the dependency closure for "
                f"role {gap.role!r}. "
                + GEOMETRY_VIS_DEPENDENCY_RULE,
            )
        for gap in point_projection_interface_gaps(layer.stages, scene_rows):
            if gap.reason == "owner_mutation":
                detail = (
                    f"camera owner also mutates observed selector {gap.selector!r}"
                )
            else:
                detail = (
                    f"selector {gap.selector!r} is produced by "
                    f"{list(gap.producer_ids)}, but the camera owner consumes no "
                    "compatible typed interface"
                )
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "consumes",
                ),
                f"point-projection contract {gap.contract_id}: {detail}. "
                + PROJECTED_ORIGIN_REPAIR_RULE,
            )

        from vfx_harness.domain.atomicity import ATOMICITY_RULE, atomicity_gaps

        raw_stages = tuple(
            row
            for row in (layer_row.get("stages") or [])
            if isinstance(row, dict)
        )
        for gap in atomicity_gaps(
            layer.stages, scene_rows, layer_id=layer_id, raw_stages=raw_stages
        ):
            pointer_field = {
                "padding": "id",
                "mixed_clusters": "mutates",
                "missing_interface": "id",
                "authored_exports": "publishes",
                "unknown_kind": "evaluation",
                "repair_owners": "evaluation",
                "consumed_mutation": "mutates",
                "invalid_coordination": "evaluation",
                "unresolved_family": "mutates",
                "missing_consumption": "consumes",
                "incompatible_interface": "consumes",
            }.get(gap.code, "mutates")
            extra = ""
            if gap.considered_exceptions:
                extra = (
                    " Exceptions considered and insufficient: "
                    + ", ".join(gap.considered_exceptions)
                    + "."
                )
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    pointer_field,
                ),
                f"unit {gap.unit_id}: {gap.detail}{extra} Legal next actions: "
                "split the unit, consume a typed assembly interface, bind dressing, "
                "or reassign evidence. " + ATOMICITY_RULE,
            )

        from vfx_harness.domain.image_signal import (
            IMAGE_SIGNAL_DEPENDENCY_RULE,
            image_signal_dependency_gaps,
            image_signal_provider_ids,
            image_signal_witness_guidance,
        )

        target_index = next(
            (
                index
                for index, row in enumerate(combined_layers)
                if str(row.get("id") or "") == layer_id
            ),
            0,
        )
        earlier_signal_available = any(
            str(row.get("execution") or "") == "ready"
            and str(row.get("id") or "") in parsed
            and image_signal_provider_ids(
                parsed[str(row.get("id"))].stages, combined_scene_rows
            )
            for row in combined_layers[:target_index]
        )
        for gap in image_signal_dependency_gaps(
            layer.stages,
            combined_scene_rows,
            earlier_signal_available=earlier_signal_available,
        ):
            available = (
                " Same-layer signal provider(s) exist but are outside the dependency "
                f"closure: {list(gap.available_provider_ids)}."
                if gap.available_provider_ids
                else " No same-layer unit currently derives a signal family."
            )
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "depends_on",
                ),
                f"unit {gap.unit_id} owes image-contract debt "
                f"{list(gap.contract_ids)} before optical signal is available."
                + available
                + " Registered write-kind witnesses: "
                + image_signal_witness_guidance()
                + ". "
                + IMAGE_SIGNAL_DEPENDENCY_RULE,
            )
    # A binding that declares its moments must include the bound contract's own frame:
    # declaring moments [150] for a frame-72 contract authors evidence that can never
    # be produced when it is due.
    frame_by_id = {
        str(row.get("id")): int(row["frame"])
        for row in scene_rows
        if isinstance(row, dict) and row.get("id") and row.get("frame") is not None
    }
    if layer is not None:
        from vfx_harness.domain.work_units import (
            LOOK_REQUIRES_IMAGE_DOMAIN_RULE,
            PROJECTED_ORIGIN_REPAIR_RULE,
            UNIT_JUDGE_CLAIM_COVERAGE_RULE,
            VIS_REPAIR_OWNER_RULE,
            uncovered_unit_judge_frames,
            unearned_look_judge_frames,
            vis_roles_unrepairable_by,
        )

        for unit_index, unit in enumerate(layer.stages):
            missing_frames = uncovered_unit_judge_frames(unit)
            if missing_frames:
                note(
                    json_ptr("layer", "stages", unit_index, "evaluation", "judge"),
                    f"unit {unit.id} judges frame(s) {list(missing_frames)} with no "
                    f"required claim moment. {UNIT_JUDGE_CLAIM_COVERAGE_RULE}",
                )
            unearned_frames = unearned_look_judge_frames(unit)
            if unearned_frames:
                note(
                    json_ptr("layer", "stages", unit_index, "look_capabilities"),
                    f"unit {unit.id} declares look_capabilities "
                    f"{list(unit.look_capabilities)} but judge frame(s) "
                    f"{list(unearned_frames)} have no required image-domain claim. "
                    f"{LOOK_REQUIRES_IMAGE_DOMAIN_RULE}",
                )
            for claim in unit.evaluation.claims:
                for binding in claim.evidence:
                    declared = getattr(binding, "moments", None)
                    contract_frame = frame_by_id.get(str(binding.id))
                    if (
                        declared is not None
                        and contract_frame is not None
                        and contract_frame not in declared
                    ):
                        note(
                            json_ptr("layer", "stages", unit_index, "evaluation", "claims"),
                            f"unit {unit.id} claim {claim.id} binds {binding.id} at moments "
                            f"{sorted(declared)}, but that contract measures frame "
                            f"{contract_frame} — a binding due when it cannot be produced",
                        )

    required_bindings: set[tuple[str, str]] = set()
    if layer is not None:
        required_bindings = {
            (binding.kind, binding.id)
            for unit in layer.stages
            for claim in unit.evaluation.claims
            if claim.required
            for binding in claim.evidence
        }
        # Composition context (judge-frame visibility/framing rows) is a producer binding
        # too: the gate already reads it as coverage, and the unit that stages the judged
        # content answers for its context rows the same way it answers for claim evidence.
        required_bindings.update(
            ("scene_contract", str(contract_id))
            for unit in layer.stages
            if unit.evaluation.composition_context is not None
            for contract_id in unit.evaluation.composition_context.contract_ids
        )
    missing_claims = sorted(
        contract_id
        for contract_id, (kind, _row) in all_contracts.items()
        if (kind, contract_id) not in required_bindings
    )
    if missing_claims and layer is not None:
        note(
            json_ptr("layer", "stages"),
            "materialized contracts lack required producing claims: "
            + ", ".join(missing_claims),
        )

    # A mesh metric needs polygons under the roles it selects. Units declare `geometry`
    # when their roles carry meshes; a mesh metric aimed anywhere else reads None
    # forever, which is a binding defect no build can repair. Only enforced once some
    # unit in the layer declares anything, so legacy units are not judged on a
    # declaration they never had the chance to make.
    if layer is not None:
        MESH_KINDS = {"smooth_fraction", "mesh_vertex_count", "radial_inward_fraction"}
        declares_anything = any(unit.provides for unit in layer.stages)
        geometry_roles = {
            role
            for unit in layer.stages
            if "geometry" in unit.provides
            for role in unit.mutates.roles
        }
        if declares_anything:
            for index, row in enumerate(scene_rows):
                if str(row.get("kind")) not in MESH_KINDS:
                    continue
                roles = [str(r) for r in (row.get("roles") or [])]
                if roles and not any(
                    any(
                        fnmatch.fnmatchcase(role, owned) or fnmatch.fnmatchcase(owned, role)
                        for owned in geometry_roles
                    )
                    for role in roles
                ):
                    note(
                        json_ptr("scene_contracts", index, "kind"),
                        f"contract {row.get('id')} uses mesh metric {row.get('kind')!r} on "
                        f"roles {roles}, but no unit declaring provides:[\"geometry\"] owns "
                        f"them (geometry roles: {sorted(geometry_roles) or 'none declared'}); "
                        "it can only read None. Bind a metric that applies to these roles, "
                        "or declare the unit that gives them polygons.",
                    )

    # A metric may only close a claim it can actually support. Counting rim modules
    # proves they exist, not that they chase; radial closure proves an aperture is shut,
    # not that it reads as machined metal. Run 20260823T154920Z shipped both.
    from vfx_harness.domain.work_units import (
        STRUCTURAL_CLAIM_DOMAINS,
        plan_selector_declared,
    )

    if layer is not None:
        from vfx_harness.domain.image_debts import (
            IMAGE_PROPERTY_VOCABULARY_RULE,
            image_property_vocabulary_gaps,
            payable_image_property_kinds,
        )
        from vfx_harness.evidence.checks import METRICS

        unit_index_by_id = {unit.id: index for index, unit in enumerate(layer.stages)}
        payable_properties = sorted(payable_image_property_kinds(METRICS))
        for gap in image_property_vocabulary_gaps(layer.stages, METRICS):
            note(
                json_ptr(
                    "layer",
                    "stages",
                    unit_index_by_id[gap.unit_id],
                    "evaluation",
                    "claims",
                ),
                f"unit {gap.unit_id} required image claim {gap.claim_id} uses "
                f"unpayable property {gap.property!r} for {list(gap.contract_ids)}. "
                f"Accepted image properties: {payable_properties}. "
                + IMAGE_PROPERTY_VOCABULARY_RULE,
            )
        for unit_index, unit in enumerate(layer.stages):
            # Every mutated role needs a required claim answering for it — the gate's
            # claim-closure rule, enforced HERE so the write-hook reports it in-session.
            # Run 20260825T015307Z-bc9109 published a materialization the validator called
            # clean and the gate then blocked with 7 role-closure findings the session
            # could no longer see.
            closure_roles = {
                role
                for claim in unit.evaluation.claims
                if claim.required
                for role in claim.subject_roles
            }
            for mutation_role in [*unit.mutates.roles, *unit.mutates.dresses]:
                if not any(
                    fnmatch.fnmatchcase(role, mutation_role)
                    or fnmatch.fnmatchcase(mutation_role, role)
                    for role in closure_roles
                ):
                    note(
                        json_ptr("layer", "stages", unit_index, "mutates"),
                        f"unit {unit.id}: mutation role {mutation_role!r} has no required "
                        "claim; every mutated or dressed role needs a required claim whose "
                        "subject_roles cover it, or must be dropped from mutates",
                    )
            for claim in unit.evaluation.claims:
                if not claim.required:
                    continue
                owner = next(
                    (item for item in layer.stages if item.id == claim.repair_owner),
                    unit,
                )
                for binding in claim.evidence:
                    if binding.kind != "scene_contract":
                        continue
                    bound = all_contracts.get(binding.id)
                    if not bound or bound[0] != "scene_contract":
                        continue
                    vis_row = bound[1]
                    if (
                        str(vis_row.get("kind") or "") in PROJECTED_ORIGIN_KINDS
                        and "camera" not in owner.provides
                    ):
                        note(
                            json_ptr(
                                "layer", "stages", unit_index, "evaluation", "claims"
                            ),
                            f"unit {unit.id} binds point-projection metric "
                            f"{vis_row.get('kind')} {binding.id}, but repair_owner "
                            f"{owner.id} does not provide camera. "
                            + PROJECTED_ORIGIN_REPAIR_RULE,
                        )
                    if str(vis_row.get("kind") or "") in SURFACE_PROJECTED_KINDS:
                        roles = [str(value) for value in vis_row.get("roles") or []]
                        mutates_measured_role = any(
                            fnmatch.fnmatchcase(role, selector)
                            or fnmatch.fnmatchcase(selector, role)
                            for role in roles
                            for selector in owner.mutates.roles
                        )
                        dresses_measured_role = any(
                            fnmatch.fnmatchcase(role, selector)
                            or fnmatch.fnmatchcase(selector, role)
                            for role in roles
                            for selector in owner.mutates.dresses
                        )
                        if (
                            mutates_measured_role
                            and "geometry" not in owner.provides
                            and not dresses_measured_role
                        ):
                            note(
                                json_ptr(
                                    "layer", "stages", unit_index, "evaluation", "claims"
                                ),
                                f"unit {unit.id} binds surface metric "
                                f"{vis_row.get('kind')} {binding.id} on its mutated roles "
                                f"{roles}, but repair_owner {owner.id} does not provide "
                                "geometry or dress those roles. A control/Empty host has "
                                "no rendered surface. Use projected_origin_x/"
                                "projected_origin_y for a point interface, or split a "
                                "genuine geometry provider; do not add proxy mesh only "
                                "to satisfy bbox/visible_fraction.",
                            )
                    kind = str(vis_row.get("kind") or "")
                    owner_layer = str(vis_row.get("owner_layer") or "")
                    activates_at = str(vis_row.get("activates_at") or owner_layer)
                    deferred_row = bool(
                        owner_layer and activates_at and activates_at != owner_layer
                    )
                    observation_only = "camera" in owner.provides and (
                        kind == "visible_fraction" or kind in PROJECTED_ORIGIN_KINDS
                    )
                    if not deferred_row:
                        if observation_only:
                            primary_keys: tuple[str, ...] = ()
                        elif kind in TWO_SIDED_MEASUREMENT_KINDS:
                            primary_keys = ("roles",)
                        else:
                            primary_keys = ("roles", "compare_roles")
                        selected_roles = {
                            str(value)
                            for key in primary_keys
                            for value in vis_row.get(key) or []
                        }
                        mutable_roles = [*unit.mutates.roles, *unit.mutates.dresses]
                        undeclared_roles = sorted(
                            selector
                            for selector in selected_roles
                            if not plan_selector_declared(selector, mutable_roles)
                        )
                        if undeclared_roles:
                            note(
                                json_ptr(
                                    "layer", "stages", unit_index, "evaluation", "claims"
                                ),
                                f"unit {unit.id} contract {binding.id} selects roles "
                                "outside mutation authority: "
                                + ", ".join(undeclared_roles)
                                + ". "
                                + ROLE_SELECTOR_CLOSURE_RULE,
                            )
                    if kind != "visible_fraction":
                        continue
                    unrepaired = vis_roles_unrepairable_by(
                        provides=owner.provides,
                        mutation_roles=[*owner.mutates.roles, *owner.mutates.dresses],
                        vis_roles=vis_row.get("roles") or [],
                    )
                    if unrepaired:
                        note(
                            json_ptr(
                                "layer", "stages", unit_index, "evaluation", "claims"
                            ),
                            f"unit {unit.id} claim {claim.id} binds visible_fraction "
                            f"{binding.id} on {list(unrepaired)}; repair_owner "
                            f"{owner.id} neither provides camera nor mutates/dresses "
                            f"those roles. {VIS_REPAIR_OWNER_RULE}",
                        )
                if claim.asserts is None:
                    note(
                        json_ptr("layer", "stages", unit_index, "evaluation", "claims"),
                        f"required claim {claim.id} must declare `asserts` — the evidence "
                        "domain its proposition lives in — so its metrics can be checked "
                        "against what they can certify",
                    )
                    continue
                if claim.asserts not in STRUCTURAL_CLAIM_DOMAINS:
                    continue  # image/human evidence is candidate-bound, proved at build time

                def _domain_of(binding_id: str) -> str:
                    entry = all_contracts.get(binding_id)
                    if entry is None:
                        return "unknown"
                    return KIND_DOMAINS.get(str(entry[1].get("kind")), "unknown")

                bound = {binding.id: _domain_of(binding.id) for binding in claim.evidence}
                incompatible = {
                    binding_id: domain
                    for binding_id, domain in bound.items()
                    if domain != claim.asserts
                }
                if incompatible:
                    summary = ", ".join(
                        f"{cid}={domain}" for cid, domain in incompatible.items()
                    )
                    note(
                        json_ptr("layer", "stages", unit_index, "evaluation", "claims"),
                        f"claim {claim.id} asserts {claim.asserts!r} but carries padding "
                        f"evidence that cannot certify that domain ({summary}); every "
                        f"required evidence binding must be a {claim.asserts} metric. Put "
                        "cross-domain observations in composition_context or split the "
                        "proposition into separately typed claims",
                    )

    # A structured human decision whose roles live in this layer's reserved namespaces is
    # adopted HERE: a schema-5 global bundle publishes no contracts, so the global gate
    # only proves an owner exists. The exact executable copy — and its binding to a
    # required producing claim, enforced just above for every contract — lands at the
    # owner's materialization. Adoption is last-write-wins for the selected bundle
    # only: a prior generation's values.contract is inert, and a later superseded or
    # falsified row retires the id (HIR-0028).
    ledger_path = (
        Path(resolutions_path)
        if resolutions_path is not None
        else root / "state" / "plan-resolutions.jsonl"
    )
    active_decisions = load_active_structured_decisions(
        ledger_path, bundle_hash=expected_bundle_hash
    )
    for decision in active_decisions.values():
        roles = [str(role) for role in (decision.contract.get("roles") or [])]
        if not roles or not all(_matches_reserved(role, reserved) for role in roles):
            continue
        adopted = [
            row for row in scene_rows
            if str(row.get("decision_id") or "") == decision.id
            and all(row.get(key) == value for key, value in decision.contract.items())
        ]
        if not adopted:
            note(
                json_ptr("scene_contracts"),
                f"materialization must adopt structured decision {decision.id}: copy "
                "values.contract exactly into scene_contracts, keep decision_id, and "
                "bind it to a required claim",
            )
    for index, row in enumerate(scene_rows):
        if not isinstance(row, dict):
            continue
        decision_id = str(row.get("decision_id") or "").strip()
        if decision_id and decision_id not in active_decisions:
            note(
                json_ptr("scene_contracts", index, "decision_id"),
                f"scene contract adopts decision {decision_id!r} which is not active "
                "on the selected bundle; copy only the compiled binding set, or omit "
                "decision_id for a newly authored contract",
            )

    raw_bindings = payload.get("requirement_bindings")
    if not isinstance(raw_bindings, list):
        note(json_ptr("requirement_bindings"), "materialization.requirement_bindings must be a list")
        raw_bindings = []
    requirement_bindings: dict[str, tuple[str, ...]] = {}
    requirement_decisions: dict[str, dict[str, str]] = {}
    requirement_evidence_domains: dict[str, tuple[str, ...]] = {}
    requirement_domain_bindings: dict[str, tuple[dict[str, Any], ...]] = {}
    seen_requirements: set[str] = set()
    for index, binding in enumerate(raw_bindings):
        if not isinstance(binding, dict):
            note(
                json_ptr("requirement_bindings", index),
                f"requirement_bindings[{index}] must be an object",
            )
            continue
        requirement_id = str(binding.get("requirement_id") or "")
        if (
            not requirement_id
            or requirement_id in seen_requirements
        ):
            note(
                json_ptr("requirement_bindings", index, "requirement_id"),
                f"requirement_bindings[{index}].requirement_id must be unique",
            )
            continue
        seen_requirements.add(requirement_id)
        contract_ids = tuple(map(str, binding.get("contract_ids") or []))
        decision = binding.get("decision")
        if contract_ids:
            if len(set(contract_ids)) != len(contract_ids):
                note(
                    json_ptr("requirement_bindings", index, "contract_ids"),
                    f"requirement {requirement_id} contains duplicate contract ids",
                )
                continue
            missing = sorted(set(contract_ids) - requirement_contract_ids)
            if missing:
                note(
                    json_ptr("requirement_bindings", index, "contract_ids"),
                    f"requirement {requirement_id} names absent contracts: {', '.join(missing)}",
                )
                continue
            requirement_bindings[requirement_id] = contract_ids
        if isinstance(decision, dict):
            statement = str(decision.get("statement") or "").strip()
            strength = str(decision.get("decision_strength") or "").strip()
            if not statement or strength not in {
                "hard_constraint", "approved_start", "planner_start", "confirmed_outcome"
            }:
                note(
                    json_ptr("requirement_bindings", index, "decision"),
                    f"requirement {requirement_id} decision must have statement and decision_strength",
                )
                continue
            requirement_decisions[requirement_id] = {
                "statement": statement,
                "decision_strength": strength,
            }
        elif decision is not None:
            note(
                json_ptr("requirement_bindings", index, "decision"),
                f"requirement {requirement_id} decision must be an object",
            )
        if not contract_ids and decision is None:
            note(
                json_ptr("requirement_bindings", index),
                f"requirement {requirement_id} must bind contracts or an explicit typed decision",
            )

    owned = set(map(str, jit.get("owned_requirements") or []))
    bound = set(requirement_bindings) | set(requirement_decisions)
    # `owned_requirements` means debt: rows the register defers to exactly this layer.
    # A bundle whose owned list carries concretely-resolved or foreign rows is
    # inconsistent authority — fail closed and route to republication. Bending the
    # closure here to tolerate one published bundle's shape would generalize that
    # shot's accident into the contract.
    register_path = (
        Path(base_requirements_path)
        if base_requirements_path is not None
        else root / "requirements.json"
    )
    register_rows = {
        str(row.get("id")): row
        for row in _rows(_document(register_path), "requirements", "requirements.json")
    }
    register = {
        requirement_id: (row.get("resolution") or {})
        for requirement_id, row in register_rows.items()
    }
    unknown_owned = sorted(rid for rid in owned if rid not in register)
    if unknown_owned:
        note(
            json_ptr("requirement_bindings"),
            "owned requirements missing from the register: " + ", ".join(unknown_owned),
        )
    concrete_owned = sorted(
        rid for rid in owned if rid in register and register[rid].get("kind") != "deferred_owner"
    )
    if concrete_owned:
        note(
            json_ptr("requirement_bindings"),
            "owned requirements are already resolved concretely in the register: "
            + ", ".join(concrete_owned)
            + " — the bundle's ownership is inconsistent authority; republish the "
            "global plan instead of materializing around it",
        )
    foreign = sorted(
        rid for rid in owned
        if rid in register and str(register[rid].get("owner_layer") or "") != layer_id
    )
    if foreign:
        note(
            json_ptr("requirement_bindings"),
            "owned requirements are deferred to another layer in the register: "
            + ", ".join(foreign),
        )
    missing_requirements = sorted(owned - bound)
    extra_requirements = sorted(bound - owned)
    if missing_requirements or extra_requirements:
        note(
            json_ptr("requirement_bindings"),
            "JIT owned-requirement closure is incomplete"
            + (f"; missing {', '.join(missing_requirements)}" if missing_requirements else "")
            + (f"; unknown {', '.join(extra_requirements)}" if extra_requirements else ""),
        )

    contract_domains: dict[str, str] = {}
    for contract_id, (binding_kind, row) in all_contracts.items():
        contract_domains[contract_id] = (
            "image"
            if binding_kind == "image_contract"
            else KIND_DOMAINS.get(str(row.get("kind") or ""), "unknown")
        )
    contract_domains.update(dict.fromkeys(image_debt_ids, "image"))
    qualitative_domains = {"image", "human"}
    from vfx_harness.domain.work_units import parse_evidence_domains

    for requirement_id in sorted(owned & bound):
        resolution = register.get(requirement_id) or {}
        authored_statement = str(
            (register_rows.get(requirement_id) or {}).get("statement") or ""
        ).strip()
        try:
            declared = parse_evidence_domains(
                resolution.get("evidence_domains"),
                f"requirements.json requirement {requirement_id}.resolution.evidence_domains",
            )
        except ValueError as exc:
            note(json_ptr("requirement_bindings"), str(exc))
            continue
        contract_ids = requirement_bindings.get(requirement_id, ())
        undeclared_contracts = {
            contract_id: contract_domains.get(contract_id, "unknown")
            for contract_id in contract_ids
            if contract_domains.get(contract_id) not in declared
        }
        if undeclared_contracts:
            witnesses = ", ".join(
                f"{contract_id}={domain}"
                for contract_id, domain in undeclared_contracts.items()
            )
            note(
                json_ptr("requirement_bindings"),
                f"requirement {requirement_id} carries padding contract bindings outside "
                f"its declared AND domains {list(declared)}: {witnesses}. Remove those ids "
                "from this requirement binding; each witness may pay only its canonical "
                "registry domain",
            )
        by_domain = {
            domain: tuple(sorted(cid for cid in contract_ids if contract_domains.get(cid) == domain))
            for domain in declared
        }
        decision = requirement_decisions.get(requirement_id)
        if decision and str(decision.get("statement") or "").strip() != authored_statement:
            note(
                json_ptr("requirement_bindings"),
                f"requirement {requirement_id} provisional decision must preserve the "
                f"authored requirement statement exactly; expected {authored_statement!r}, "
                f"found {str(decision.get('statement') or '').strip()!r}. Materialization "
                "may classify the debt strength but cannot rewrite the proposition",
            )
        decision_domains = {
            domain for domain in declared if domain in qualitative_domains and not by_domain[domain]
        }
        if decision and not decision_domains:
            note(
                json_ptr("requirement_bindings"),
                f"requirement {requirement_id} carries a decision but every declared domain "
                "already has contract evidence; remove the padding decision",
            )
        if decision and str(decision.get("decision_strength") or "") not in {
            "approved_start",
            "planner_start",
        }:
            note(
                json_ptr("requirement_bindings"),
                f"requirement {requirement_id} qualitative domain debt must use "
                "approved_start or planner_start; materialization cannot invent a "
                "confirmed outcome",
            )
        covered = {domain for domain, ids in by_domain.items() if ids}
        if decision:
            covered.update(decision_domains)
        missing_domains = sorted(set(declared) - covered)
        if missing_domains:
            witnesses = ", ".join(
                f"{contract_id}={contract_domains.get(contract_id, 'unknown')}"
                for contract_id in contract_ids
            ) or "no contract ids"
            note(
                json_ptr("requirement_bindings"),
                f"requirement {requirement_id} declares AND domains {list(declared)} but "
                f"does not pay {missing_domains}; bound witnesses: {witnesses}. Bind a "
                "same-domain contract for structural domains and an explicit "
                "approved_start/planner_start decision for unpaid image or human debt",
            )
            continue
        domain_rows: list[dict[str, Any]] = []
        for domain in declared:
            ids = by_domain[domain]
            if ids:
                domain_rows.append({"domain": domain, "kind": "contract", "ids": list(ids)})
            else:
                domain_rows.append({
                    "domain": domain,
                    "kind": "provisional_decision",
                    "statement": authored_statement,
                    "decision_strength": decision["decision_strength"],
                })
        requirement_evidence_domains[requirement_id] = declared
        requirement_domain_bindings[requirement_id] = tuple(domain_rows)

    acceptance = payload.get("acceptance", [])
    if not isinstance(acceptance, list) or any(not isinstance(row, dict) for row in acceptance):
        note(json_ptr("acceptance"), "materialization.acceptance must be a list of objects")
        acceptance = []
    if layer is not None:
        judge_frames = {frame for frame, _ref in layer.judges}
    else:
        judge_frames = {
            int(row["frame"])
            for row in (layer_row.get("judge") or [])
            if isinstance(row, dict) and isinstance(row.get("frame"), int)
        }
    bad_acceptance = sorted(
        str(row.get("id") or "<missing>")
        for row in acceptance
        if row.get("frame") not in judge_frames
    )
    if bad_acceptance:
        note(
            json_ptr("acceptance"),
            "materialized acceptance rows must use this layer's judge frames: "
            + ", ".join(bad_acceptance),
        )
    # A layer judged at a frame nobody proved shows its subject is judged on faith:
    # run 20260825 sealed a whole lookdev layer whose every judged surface sat behind
    # a solid proxy disc at both judge frames — projection-only bbox rows pass through
    # occluders and layer 2 carried no context rows at f72/f150 at all. Every judge
    # frame must carry occlusion-true visibility evidence for what the frame judges.
    from vfx_harness.domain.work_units import unit_requires_surface_visibility

    surface_visibility_due = bool(
        layer is not None
        and any(unit_requires_surface_visibility(unit) for unit in layer.stages)
    )
    uncovered = (
        sorted(
            str(frame)
            for frame in judge_frames
            if not any(
                row.get("kind") == "visible_fraction" and row.get("frame") == frame
                for row in scene_rows
            )
        )
        if surface_visibility_due
        else []
    )
    if uncovered:
        note(
            json_ptr("scene_contracts"),
            "every judge frame needs a visible_fraction contract for the roles that "
            "frame judges; missing at frame(s): " + ", ".join(uncovered),
        )

    if findings:
        raise ValueError("\n".join(findings))
    if layer is None:
        raise ValueError(format_finding(json_ptr("layer", "stages"), "layer stages did not parse"))
    return MaterializedLayer(
        layer,
        layer_row,
        tuple(scene_rows),
        tuple(image_rows),
        requirement_bindings,
        requirement_decisions,
        requirement_evidence_domains,
        requirement_domain_bindings,
        tuple(acceptance),
    )


def inspect_materialization(
    global_root: str | Path,
    materialization_path: str | Path,
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
    base_scene_checks_path: str | Path | None = None,
    resolutions_path: str | Path | None = None,
    base_requirements_path: str | Path | None = None,
) -> tuple[list[str], MaterializedLayer | None]:
    """Return every collectable finding without requiring the caller to catch ValueError."""
    try:
        return [], validate_materialization(
            global_root,
            materialization_path,
            expected_bundle_hash=expected_bundle_hash,
            base_layers_path=base_layers_path,
            base_scene_checks_path=base_scene_checks_path,
            resolutions_path=resolutions_path,
            base_requirements_path=base_requirements_path,
        )
    except ValueError as exc:
        return [line for line in str(exc).split("\n") if line], None


def seed_materialization_candidate(
    global_root: str | Path,
    materialization_path: str | Path,
    *,
    layer_id: str,
    bundle_hash: str,
) -> Path:
    """Create the deterministic wrapper for an incrementally staged layer design.

    The model owns unit decomposition and evidence, but not schema wrappers, bundle
    identity, global structural fields, or output paths. Seeding those facts lets the
    materializer publish one bounded unit at a time instead of generating one monolithic
    first-write document before the harness can observe any progress.
    """
    root = Path(global_root)
    rows = _rows(_document(root / "layers.json"), "layers", "layers.json")
    source = next((row for row in rows if str(row.get("id")) == str(layer_id)), None)
    if source is None or source.get("execution") != "jit_deferred":
        raise ValueError(f"layer {layer_id!r} is not selected jit_deferred authority")
    layer = dict(source)
    layer.pop("jit", None)
    layer["execution"] = "ready"
    layer["stages"] = []
    payload = {
        "schema": MATERIALIZATION_SCHEMA,
        "bundle_hash": bundle_hash,
        "layer": layer,
        "scene_contracts": [],
        "image_contracts": [],
        "requirement_bindings": [],
        "acceptance": [],
    }
    target = Path(materialization_path)
    atomic_write(target, json.dumps(payload, indent=1) + "\n")
    return target


def _validate_local_staged_units(
    payload: dict[str, Any],
    *,
    allowed_provides: frozenset[str] | None = None,
) -> None:
    """Enforce unit-local publication predicates on an in-memory candidate."""
    from vfx_harness.domain.atomicity import atomicity_gaps
    from vfx_harness.domain.work_units import (
        PROJECTED_ORIGIN_REPAIR_RULE,
        WorkUnit,
        point_projection_interface_gaps,
        validate_unit_script_path,
    )

    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError("candidate has unsupported materialization schema")
    stages = _rows(payload.get("layer") or {}, "stages", "candidate.layer")
    contracts = _rows(payload, "scene_contracts", "candidate")
    bindings = _rows(payload, "requirement_bindings", "candidate")
    parsed_units = [
        WorkUnit.parse(row, f"staged unit[{index}]") for index, row in enumerate(stages)
    ]
    from vfx_harness.domain.work_units import (
        DEFERRED_CONTRACT_CONTEXT_RULE,
        deferred_claim_binding_gaps,
    )

    deferred_gaps = deferred_claim_binding_gaps(parsed_units, contracts)
    if deferred_gaps:
        detail = "; ".join(
            f"unit {gap.unit_id} claim {gap.claim_id} binds {gap.contract_id} "
            f"(owner_layer={gap.owner_layer}, activates_at={gap.activates_at})"
            for gap in deferred_gaps
        )
        raise ValueError(
            "deferred contract claim binding refused before candidate write: "
            + detail
            + ". "
            + DEFERRED_CONTRACT_CONTEXT_RULE
        )
    if allowed_provides is not None:
        from vfx_harness.domain.work_units import CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE

        for unit in parsed_units:
            disallowed = sorted(set(unit.provides) - allowed_provides)
            if disallowed:
                raise ValueError(
                    "global capability boundary refused before candidate write: "
                    f"unit {unit.id} declares {disallowed}; allowed here: "
                    f"{sorted(allowed_provides)}. "
                    + CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE
                )
    layer_id = str((payload.get("layer") or {}).get("id") or "")
    for unit_index, unit in enumerate(parsed_units):
        validate_unit_script_path(layer_id, unit, f"staged unit[{unit_index}]")
    from vfx_harness.domain.image_debts import (
        IMAGE_PROPERTY_VOCABULARY_RULE,
        image_property_vocabulary_gaps,
    )
    from vfx_harness.evidence.checks import METRICS

    image_property_gaps = image_property_vocabulary_gaps(parsed_units, METRICS)
    if image_property_gaps:
        detail = "; ".join(
            f"unit {gap.unit_id} claim {gap.claim_id} property {gap.property!r} "
            f"for {list(gap.contract_ids)}"
            for gap in image_property_gaps
        )
        raise ValueError(
            "image property vocabulary refused before candidate write: "
            + detail
            + ". "
            + IMAGE_PROPERTY_VOCABULARY_RULE
        )
    unit_ids = [unit.id for unit in parsed_units]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("staged unit ids must be unique before candidate write")
    contract_ids = [str(row.get("id") or "") for row in contracts]
    if any(not identifier for identifier in contract_ids) or len(contract_ids) != len(
        set(contract_ids)
    ):
        raise ValueError(
            "staged scene contract ids must be non-empty and unique before candidate write"
        )
    for row in contracts:
        if error := validate_row(row):
            raise ValueError(f"scene contract {row.get('id', '<missing>')}: {error}")
    requirement_ids = [str(row.get("requirement_id") or "") for row in bindings]
    if any(not identifier for identifier in requirement_ids) or len(requirement_ids) != len(
        set(requirement_ids)
    ):
        raise ValueError(
            "staged requirement ids must be non-empty and unique before candidate write"
        )
    units_by_id = {item.id: item for item in parsed_units}
    contracts_by_id = {str(row.get("id")): row for row in contracts}
    for staged_unit in parsed_units:
        for claim in staged_unit.evaluation.claims:
            if not claim.required:
                continue
            owner = units_by_id.get(claim.repair_owner)
            for binding in claim.evidence:
                row = contracts_by_id.get(binding.id)
                if (
                    binding.kind == "scene_contract"
                    and row is not None
                    and str(row.get("kind") or "") in PROJECTED_ORIGIN_KINDS
                    and owner is not None
                    and "camera" not in owner.provides
                ):
                    raise ValueError(
                        "point-projection ownership refused before candidate write: "
                        f"unit {staged_unit.id} claim {claim.id} names repair_owner "
                        f"{owner.id}, which does not provide camera. "
                        + PROJECTED_ORIGIN_REPAIR_RULE
                    )
    interface_gaps = point_projection_interface_gaps(parsed_units, contracts)
    if interface_gaps:
        gap = interface_gaps[0]
        if gap.reason == "owner_mutation":
            detail = f"camera owner mutates observed selector {gap.selector!r}"
        else:
            detail = (
                f"selector {gap.selector!r} is produced by {list(gap.producer_ids)} "
                "without a compatible consumed interface"
            )
        raise ValueError(
            "point-projection interface refused before candidate write: "
            f"unit {gap.unit_id} contract {gap.contract_id}: {detail}. "
            + PROJECTED_ORIGIN_REPAIR_RULE
        )
    gaps = atomicity_gaps(
        parsed_units,
        contracts,
        layer_id=layer_id,
        raw_stages=stages,
    )
    if gaps:
        detail = "; ".join(
            f"unit {gap.unit_id} {gap.code}: {gap.detail}" for gap in gaps
        )
        raise ValueError("unit atomicity refused before candidate write: " + detail)


def _stage_materialization_payload(
    payload: dict[str, Any],
    *,
    unit: dict[str, Any],
    scene_contracts: list[dict[str, Any]],
    requirement_bindings: list[dict[str, Any]],
    layer_updates: dict[str, Any] | None,
    allowed_provides: frozenset[str] | None,
) -> None:
    """Apply one stage operation and validate it before the transaction writes."""
    from vfx_harness.domain.work_units import WorkUnit

    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError("candidate has unsupported materialization schema")
    if not isinstance(unit, dict):
        raise ValueError("unit must be an object")
    parsed = WorkUnit.parse(unit, "staged unit")
    contracts = list(scene_contracts)
    bindings = list(requirement_bindings)
    if any(not isinstance(row, dict) for row in contracts):
        raise ValueError("scene_contracts must contain objects")
    if any(not isinstance(row, dict) for row in bindings):
        raise ValueError("requirement_bindings must contain objects")
    for row in contracts:
        if error := validate_row(row):
            raise ValueError(
                f"scene contract {row.get('id', '<missing>')}: {error}"
            )
    stages = _rows(payload.get("layer") or {}, "stages", "candidate.layer")
    existing_units = {str(row.get("id")) for row in stages}
    if parsed.id in existing_units:
        raise ValueError(f"unit {parsed.id!r} is already staged")
    existing_contracts = {
        str(row.get("id")) for row in _rows(payload, "scene_contracts", "candidate")
    }
    incoming_contracts = [str(row.get("id") or "") for row in contracts]
    if any(not item for item in incoming_contracts):
        raise ValueError("every staged scene contract needs an id")
    duplicate_contracts = sorted(
        existing_contracts.intersection(incoming_contracts)
        | {item for item in incoming_contracts if incoming_contracts.count(item) > 1}
    )
    if duplicate_contracts:
        raise ValueError(
            "staged scene contract ids are not unique: " + ", ".join(duplicate_contracts)
        )
    existing_requirements = {
        str(row.get("requirement_id"))
        for row in _rows(payload, "requirement_bindings", "candidate")
    }
    incoming_requirements = [str(row.get("requirement_id") or "") for row in bindings]
    if any(not item for item in incoming_requirements):
        raise ValueError("every staged requirement binding needs requirement_id")
    duplicate_requirements = sorted(
        existing_requirements.intersection(incoming_requirements)
        | {item for item in incoming_requirements if incoming_requirements.count(item) > 1}
    )
    if duplicate_requirements:
        raise ValueError(
            "staged requirement ids are not unique: " + ", ".join(duplicate_requirements)
        )
    updates = layer_updates or {}
    unknown_updates = sorted(set(updates) - {"dressable"})
    if unknown_updates:
        raise ValueError(
            "layer_updates may contain only dressable; got " + ", ".join(unknown_updates)
        )
    if "dressable" in updates:
        dressable = updates["dressable"]
        if not isinstance(dressable, list) or any(
            not isinstance(value, str) or not value.strip() for value in dressable
        ):
            raise ValueError("layer_updates.dressable must be a list of non-empty strings")
        payload["layer"]["dressable"] = dressable
    stages.append(unit)
    payload["scene_contracts"].extend(contracts)
    payload["requirement_bindings"].extend(bindings)
    _validate_local_staged_units(payload, allowed_provides=allowed_provides)


def stage_materialization_unit(
    materialization_path: str | Path,
    *,
    unit: dict[str, Any],
    scene_contracts: list[dict[str, Any]],
    requirement_bindings: list[dict[str, Any]],
    layer_updates: dict[str, Any] | None = None,
    allowed_provides: frozenset[str] | None = None,
    expected_revision: str | None = None,
) -> Path:
    """Append one bounded unit through the serialized candidate transaction."""
    path = Path(materialization_path)
    _mutate_materialization_candidate(
        path,
        lambda payload: _stage_materialization_payload(
            payload,
            unit=unit,
            scene_contracts=scene_contracts,
            requirement_bindings=requirement_bindings,
            layer_updates=layer_updates,
            allowed_provides=allowed_provides,
        ),
        expected_revision=expected_revision,
    )
    return path


def _unstage_materialization_payload(
    payload: dict[str, Any],
    *,
    unit_id: str,
) -> UnstagedMaterializationUnit:
    """Remove one scratch unit and rows that no surviving unit can consume."""
    from vfx_harness.domain.work_units import WorkUnit, bound_claim_contract_ids

    if payload.get("schema") != MATERIALIZATION_SCHEMA:
        raise ValueError("candidate has unsupported materialization schema")
    stages = _rows(payload.get("layer") or {}, "stages", "candidate.layer")
    parsed = [
        WorkUnit.parse(row, f"staged unit[{index}]")
        for index, row in enumerate(stages)
    ]
    target_index = next(
        (index for index, unit in enumerate(parsed) if unit.id == unit_id), None
    )
    if target_index is None:
        available = ", ".join(unit.id for unit in parsed) or "(none)"
        raise ValueError(
            f"cannot unstage unknown unit {unit_id!r}; staged unit ids: {available}"
        )
    dependants = sorted(
        unit.id
        for unit in parsed
        if unit.id != unit_id
        and (
            unit_id in unit.depends_on
            or any(consume.producer == unit_id for consume in unit.consumes)
        )
    )
    if dependants:
        raise ValueError(
            f"cannot unstage unit {unit_id!r}; remaining unit(s) "
            + ", ".join(dependants)
            + " depend on or consume it. Unstage dependants first, or patch their exact "
            "depends_on/consumes fields before retrying."
        )

    target = parsed[target_index]
    surviving = tuple(unit for unit in parsed if unit.id != unit_id)
    surviving_contract_ids = {
        contract_id
        for unit in surviving
        for contract_id in bound_claim_contract_ids(unit)
    }
    removed_contract_ids = tuple(
        contract_id
        for contract_id in bound_claim_contract_ids(target)
        if contract_id not in surviving_contract_ids
    )
    removed_contract_set = set(removed_contract_ids)

    del stages[target_index]
    contracts = _rows(payload, "scene_contracts", "candidate")
    payload["scene_contracts"] = [
        row
        for row in contracts
        if str(row.get("id") or "") not in removed_contract_set
    ]

    removed_requirement_ids: list[str] = []
    retained_bindings: list[dict[str, Any]] = []
    for row in _rows(payload, "requirement_bindings", "candidate"):
        contract_ids = [
            str(contract_id)
            for contract_id in row.get("contract_ids") or []
            if str(contract_id) not in removed_contract_set
        ]
        if row.get("contract_ids") is not None:
            if not contract_ids:
                removed_requirement_ids.append(str(row.get("requirement_id") or ""))
                continue
            row = {**row, "contract_ids": contract_ids}
        retained_bindings.append(row)
    payload["requirement_bindings"] = retained_bindings
    _validate_local_staged_units(payload)
    return UnstagedMaterializationUnit(
        unit_id=unit_id,
        removed_contract_ids=removed_contract_ids,
        removed_requirement_ids=tuple(removed_requirement_ids),
    )


def unstage_materialization_unit(
    materialization_path: str | Path,
    *,
    unit_id: str,
    expected_revision: str | None = None,
) -> UnstagedMaterializationUnit:
    """Retire one unit from unpublished scratch through the candidate transaction."""
    token = str(unit_id).strip()
    if not token:
        raise ValueError("unit_id is required")
    result: UnstagedMaterializationUnit | None = None

    def mutate(payload: dict[str, Any]) -> None:
        nonlocal result
        result = _unstage_materialization_payload(payload, unit_id=token)

    _mutate_materialization_candidate(
        Path(materialization_path),
        mutate,
        expected_revision=expected_revision,
    )
    assert result is not None
    return result


def apply_materialization_patch(
    global_root: str | Path,
    materialization_path: str | Path,
    pointer: str,
    value: Any,
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
    base_scene_checks_path: str | Path | None = None,
    resolutions_path: str | Path | None = None,
    base_requirements_path: str | Path | None = None,
    expected_revision: str | None = None,
) -> list[str]:
    """Set one JSON pointer on the candidate file and return remaining findings."""
    return apply_materialization_patches(
        global_root,
        materialization_path,
        ((pointer, value),),
        expected_bundle_hash=expected_bundle_hash,
        base_layers_path=base_layers_path,
        base_scene_checks_path=base_scene_checks_path,
        resolutions_path=resolutions_path,
        base_requirements_path=base_requirements_path,
        expected_revision=expected_revision,
    )


def apply_materialization_patches(
    global_root: str | Path,
    materialization_path: str | Path,
    patches: tuple[tuple[str, Any], ...] | list[tuple[str, Any]],
    *,
    expected_bundle_hash: str,
    base_layers_path: str | Path | None = None,
    base_scene_checks_path: str | Path | None = None,
    resolutions_path: str | Path | None = None,
    base_requirements_path: str | Path | None = None,
    expected_revision: str | None = None,
) -> list[str]:
    """Atomically set several JSON pointers and validate the resulting candidate once.

    All pointer operations happen on an in-memory document before the first write. A bad
    pointer therefore leaves the candidate unchanged instead of publishing a partial
    repair. The single-patch API delegates here so both paths have identical semantics.
    """
    if not patches:
        raise ValueError("materialization patch transaction must contain at least one patch")
    path = Path(materialization_path)
    parsed_pointers: list[list[str]] = []
    for pointer, _value in patches:
        tokens = split_pointer(pointer)
        if tokens == ["layer"] or (
            tokens[:2] == ["layer", "stages"]
            and (len(tokens) <= 3 or tokens[2] == "-")
        ):
            raise ValueError(
                "patch_materialization cannot add, replace, or reorder staged units; "
                "add each unit through stage_materialization_unit and patch only fields "
                "inside an existing staged unit"
            )
        parsed_pointers.append(tokens)

    findings: list[str] = []

    def mutate(payload: dict[str, Any]) -> None:
        nonlocal findings
        for (pointer, value), _tokens in zip(patches, parsed_pointers, strict=True):
            set_pointer(payload, pointer, value)
        if any(
            tokens[:2] == ["layer", "stages"]
            or tokens[:1] == ["scene_contracts"]
            or tokens[:1] == ["requirement_bindings"]
            for tokens in parsed_pointers
        ):
            from vfx_harness.domain.work_units import allowed_unit_provides

            layer_id = str((payload.get("layer") or {}).get("id") or "")
            global_layers = _rows(
                _document(Path(global_root) / "layers.json"), "layers", "layers.json"
            )
            global_row = next(
                (row for row in global_layers if str(row.get("id") or "") == layer_id),
                None,
            )
            if global_row is None:
                raise ValueError(
                    f"layer {layer_id!r} has no sparse global authority for candidate repair"
                )
            _validate_local_staged_units(
                payload,
                allowed_provides=allowed_unit_provides(global_row),
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".materialization.json",
            prefix=".candidate-validate-",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(json.dumps(payload, indent=1) + "\n")
            proposed_path = Path(handle.name)
        try:
            findings, _materialized = inspect_materialization(
                global_root,
                proposed_path,
                expected_bundle_hash=expected_bundle_hash,
                base_layers_path=base_layers_path,
                base_scene_checks_path=base_scene_checks_path,
                resolutions_path=resolutions_path,
                base_requirements_path=base_requirements_path,
            )
        finally:
            proposed_path.unlink(missing_ok=True)

    _mutate_materialization_candidate(
        path,
        mutate,
        expected_revision=expected_revision,
    )
    return findings


def selected_view_artifact(
    shot_folder: str | Path,
    name: str,
    bundle_hash: str,
    *,
    overlay_root: str | Path | None = None,
) -> Path | None:
    """Return a materialized consumer artifact.

    ``overlay_root`` is an unpublished view directory used as a remat design base.
    It is never the live pointer: a crash must not leave the shot on a hole the
    replacement never published.
    """
    if name not in OVERLAY_ARTIFACTS:
        return None
    if overlay_root is not None:
        path = Path(overlay_root) / name
        if not path.is_file():
            raise ValueError(f"overlay base is missing {name}")
        return path
    shot = Path(shot_folder)
    pointer = shot / CURRENT
    if not pointer.is_file():
        return None
    value = _document(pointer)
    if value.get("schema") != VIEW_SCHEMA:
        raise ValueError("selected JIT layer view is malformed")
    if value.get("bundle_hash") != bundle_hash:
        # A view pinned to another generation is superseded state, not authority for the
        # currently selected bundle — serve the bundle's own deferred artifact and let
        # the next materialization write this generation's view. Republication (run
        # 20260824T150358Z-3bc39c) used to leave every consumer — including the replan
        # transaction meant to reconcile the change — failing on the prior view.
        return None
    relative = (value.get("artifacts") or {}).get(name)
    expected = (value.get("hashes") or {}).get(name)
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ValueError(f"selected JIT layer view is missing {name}")
    path = shot / relative
    if not path.is_file() or _sha256(path) != expected:
        raise ValueError(f"selected JIT layer view artifact {name} is stale")
    return path


def _consumer_base_artifact(
    shot_folder: str | Path,
    name: str,
    bundle,
    *,
    overlay_root: str | Path | None = None,
) -> Path:
    """Resolve one materialization base from exactly the selected generation.

    A live JIT pointer may still name the previous plan generation immediately after
    global republication.  That view is superseded state, so the verified sparse bundle
    is the base until this generation publishes a materialized replacement.  Remat and
    ordinary materialization must share this rule; neither may turn the intentionally
    absent superseded-view result into ``Path(None)``.
    """
    selected = selected_view_artifact(
        shot_folder,
        name,
        bundle.content_hash,
        overlay_root=overlay_root,
    )
    return selected if selected is not None else bundle.root / name


def revert_materialization(
    shot_folder: str | Path, layer_id: str, *, select: bool = True
) -> Path | None:
    """Compose a view with one layer restored to global deferred authority.

    Re-materialization must design against GLOBAL authority, not against the view it is
    replacing. Without this, the discarded view's register — where this layer's owned
    requirements were already resolved concretely — is the base, and the replacement
    trips the owned-means-owed rule for requirements its predecessor closed. The layer's
    rows revert to the bundle's deferred authority; every other layer's materialization
    is preserved untouched.

    ``select=True`` (tests, explicit discard) writes the live pointer.
    ``select=False`` writes the overlay directory only: remat uses it as the design
    base and selects the replacement, or the previous pointer stays in force.
    """
    from vfx_harness.orchestration.plan_authority import resolve_current

    shot = Path(shot_folder).resolve()
    pointer_path = shot / CURRENT
    if not pointer_path.is_file():
        return None
    bundle = resolve_current(shot)
    layer_id = str(layer_id)

    def _bundle_doc(name: str) -> dict:
        return _document(bundle.root / name)

    view_docs = {
        name: json.loads(
            _consumer_base_artifact(shot, name, bundle).read_text(encoding="utf-8")
        )
        for name in OVERLAY_ARTIFACTS
    }
    # acceptance.json is a bare list; only materialization adds rows to it, so this
    # layer's fingerprints leave with its view.
    view_docs["acceptance.json"] = [
        row
        for row in view_docs["acceptance.json"]
        if not isinstance(row, dict) or str(row.get("layer") or "") != layer_id
    ]
    bundle_layers = {str(r.get("id")): r for r in _bundle_doc("layers.json")["layers"]}
    view_docs["layers.json"]["layers"] = [
        bundle_layers.get(layer_id, row) if str(row.get("id")) == layer_id else row
        for row in view_docs["layers.json"]["layers"]
    ]
    bundle_requirements = {
        str(r.get("id")): r for r in _bundle_doc("requirements.json")["requirements"]
    }
    view_docs["requirements.json"]["requirements"] = [
        bundle_requirements.get(str(row.get("id")), row)
        if str((row.get("resolution") or {}).get("owner_layer") or "") == layer_id
        or str(row.get("id")) in _owned_by(bundle_layers.get(layer_id))
        else row
        for row in view_docs["requirements.json"]["requirements"]
    ]
    for name, key in (("scene_checks.json", "contracts"), ("checks.json", "checks")):
        view_docs[name][key] = [
            row
            for row in view_docs[name][key]
            if str(row.get("owner_layer") or row.get("activates_at") or "") != layer_id
        ]
    still_materialized = sorted(
        str(row.get("id"))
        for row in view_docs["layers.json"]["layers"]
        if row.get("execution") != "jit_deferred"
    )
    view_hash = hashlib.sha256(
        json.dumps(view_docs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    view = shot / STATE_DIR / "views" / view_hash
    view.mkdir(parents=True, exist_ok=True)
    for name in OVERLAY_ARTIFACTS:
        atomic_write(view / name, json.dumps(view_docs[name], indent=2, sort_keys=True) + "\n")
    if not select:
        return view
    if not still_materialized:
        pointer_path.unlink()
        return None
    atomic_write(
        pointer_path,
        json.dumps(
            {
                "schema": VIEW_SCHEMA,
                "bundle_hash": bundle.content_hash,
                "view_hash": view_hash,
                "materialized_layers": still_materialized,
                "artifacts": {
                    name: (view / name).relative_to(shot).as_posix()
                    for name in OVERLAY_ARTIFACTS
                },
                "hashes": {name: _sha256(view / name) for name in OVERLAY_ARTIFACTS},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return pointer_path


def _owned_by(layer_row: dict | None) -> set[str]:
    jit = (layer_row or {}).get("jit") or {}
    return {str(rid) for rid in (jit.get("owned_requirements") or [])}


def _composed_documents(
    shot: Path, materialization_path: str | Path, *, overlay_root: str | Path | None = None
):
    """Validate a payload and compose the five overlaid consumer documents.

    Shared by publication (which writes the durable view + pointer) and the in-session
    candidate preview (which stages the same documents into a throwaway consumer view)
    — one composition, so the preview cannot diverge from what publication produces.
    Runs 20260825T015307Z and 034337Z each published on a preview that had shown the
    PRE-publication view: a false CLEAN, a retracted generation each."""
    from vfx_harness.orchestration.plan_authority import resolve_current

    shot = Path(shot).resolve()
    bundle = resolve_current(shot)
    global_layers = load_layers_from_path(bundle.root / "layers.json")
    payload = _document(Path(materialization_path))
    layer_id = str((payload.get("layer") or {}).get("id") or "")
    if layer_id not in global_layers:
        raise ValueError(f"JIT materialization names unknown layer {layer_id!r}")
    _require_upstream_outcomes(shot, global_layers[layer_id])
    base_layers = _consumer_base_artifact(
        shot, "layers.json", bundle, overlay_root=overlay_root
    )
    base_scene = _consumer_base_artifact(
        shot, "scene_checks.json", bundle, overlay_root=overlay_root
    )
    base_checks = _consumer_base_artifact(
        shot, "checks.json", bundle, overlay_root=overlay_root
    )
    base_requirements = _consumer_base_artifact(
        shot, "requirements.json", bundle, overlay_root=overlay_root
    )
    base_acceptance = _consumer_base_artifact(
        shot, "acceptance.json", bundle, overlay_root=overlay_root
    )
    materialized = validate_materialization(
        bundle.root,
        materialization_path,
        expected_bundle_hash=bundle.content_hash,
        base_layers_path=base_layers,
        base_scene_checks_path=base_scene,
        resolutions_path=shot / "state" / "plan-resolutions.jsonl",
        base_requirements_path=base_requirements,
    )
    return (
        bundle,
        materialized,
        {
            "layers.json": base_layers,
            "scene_checks.json": base_scene,
            "checks.json": base_checks,
            "requirements.json": base_requirements,
            "acceptance.json": base_acceptance,
        },
    )


def _overlay_documents(materialized, bases: dict) -> dict:
    """Apply one validated materialization to its base documents, in memory."""
    layer_id = str(materialized.layer.id)
    layers_doc = _document(bases["layers.json"])
    layers_doc["layers"] = [
        materialized.layer_row if str(row.get("id")) == layer_id else row
        for row in _rows(layers_doc, "layers", "layers.json")
    ]
    scene_doc = _document(bases["scene_checks.json"])
    scene_doc["contracts"] = [
        *_rows(scene_doc, "contracts", "scene_checks.json"),
        *materialized.scene_contracts,
    ]
    checks_doc = _document(bases["checks.json"])
    checks_doc["checks"] = [
        *_rows(checks_doc, "checks", "checks.json"),
        *materialized.image_contracts,
    ]
    requirements_doc = _document(bases["requirements.json"])
    for row in _rows(requirements_doc, "requirements", "requirements.json"):
        requirement_id = str(row.get("id") or "")
        contract_ids = materialized.requirement_bindings.get(requirement_id)
        decision = materialized.requirement_decisions.get(requirement_id)
        evidence_domains = materialized.requirement_evidence_domains.get(requirement_id)
        domain_bindings = materialized.requirement_domain_bindings.get(requirement_id)
        if not contract_ids and not decision:
            continue
        resolution = row.get("resolution") or {}
        if (
            resolution.get("kind") != "deferred_owner"
            or str(resolution.get("owner_layer") or "") != layer_id
        ):
            raise ValueError(
                f"requirement {requirement_id} is not owned by materialized layer {layer_id}"
            )
        if contract_ids:
            row["resolution"] = {
                "kind": "contract",
                "ids": sorted(set(contract_ids)),
                "evidence_domains": list(evidence_domains or ()),
                "domain_bindings": list(domain_bindings or ()),
            }
        else:
            row["resolution"] = {
                "kind": "decision",
                "ids": [],
                "decision": decision["statement"],
                "decision_strength": decision["decision_strength"],
                "evidence_domains": list(evidence_domains or ()),
                "domain_bindings": list(domain_bindings or ()),
            }
    acceptance_doc = json.loads(Path(bases["acceptance.json"]).read_text(encoding="utf-8"))
    if not isinstance(acceptance_doc, list):
        raise ValueError("acceptance.json must contain a list")
    acceptance_ids = {str(row.get("id")) for row in acceptance_doc if isinstance(row, dict)}
    duplicate_acceptance = sorted(
        str(row.get("id")) for row in materialized.acceptance if str(row.get("id")) in acceptance_ids
    )
    if duplicate_acceptance:
        raise ValueError("materialized acceptance ids already exist: " + ", ".join(duplicate_acceptance))
    acceptance_doc.extend(materialized.acceptance)
    return {
        "layers.json": layers_doc,
        "scene_checks.json": scene_doc,
        "checks.json": checks_doc,
        "requirements.json": requirements_doc,
        "acceptance.json": acceptance_doc,
    }


def stage_candidate_view(
    shot_folder: str | Path,
    materialization_path: str | Path,
    view: str | Path,
    *,
    overlay_root: str | Path | None = None,
) -> None:
    """Overlay an UNPUBLISHED materialization candidate onto a prepared consumer view.

    The view then looks exactly as it would after publication — overlaid documents plus
    a synthetic hash-pinned pointer — so the deterministic gate previews the candidate's
    real consequences instead of the pre-publication world."""
    shot = Path(shot_folder).resolve()
    view = Path(view).resolve()
    bundle, materialized, bases = _composed_documents(
        shot, materialization_path, overlay_root=overlay_root
    )
    documents = _overlay_documents(materialized, bases)
    for name, document in documents.items():
        target = view / name
        if target.is_symlink() or target.exists():
            target.unlink()
        target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # the view's state/ is a symlink to the shot's; replace it with a copy whose
    # jit-layers pointer pins the candidate documents just written
    state_link = view / "state"
    if state_link.is_symlink():
        real_state = state_link.resolve()
        state_link.unlink()
        state_link.mkdir()
        if real_state.is_dir():
            for child in real_state.iterdir():
                if child.name != "jit-layers":
                    (state_link / child.name).symlink_to(child)
    pointer_dir = view / "state" / "jit-layers"
    pointer_dir.mkdir(parents=True, exist_ok=True)
    pointer = {
        "schema": VIEW_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "view_hash": "candidate-preview",
        "materialized_layers": sorted(
            str(row.get("id"))
            for row in documents["layers.json"]["layers"]
            if row.get("execution") != "jit_deferred"
        ),
        "artifacts": {name: name for name in OVERLAY_ARTIFACTS},
        "hashes": {name: _sha256(view / name) for name in OVERLAY_ARTIFACTS},
    }
    (pointer_dir / "current.json").write_text(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _project_candidate_unit_state(shot, view, materialized)


def _project_candidate_unit_state(
    shot: Path,
    view: Path,
    materialized: MaterializedLayer,
) -> None:
    """Apply the exact state-backed replan to preview-local durable state.

    A replacement candidate necessarily names a different unit DAG before publication,
    while the selected shot state must continue to name the accepted predecessor until
    publication succeeds.  The terminal gate reads both surfaces.  Projecting through
    ``apply_replan`` inside the isolated consumer view lets it validate the same digest
    schema, preservation set, invalidation closure, and resulting unit identities that
    the outer transaction will apply, without mutating selected authority.
    """
    from vfx_harness.orchestration.unit_state import apply_replan, load

    layer_id = str(materialized.layer.id)
    state = load(shot, layer_id)
    if not state:
        return
    old_plan_hash = str(state.get("plan_hash") or "")
    if not old_plan_hash:
        raise ValueError(
            f"layer {layer_id} candidate preview cannot project unit state without "
            "the durable predecessor plan_hash"
        )

    source_dir = shot / "state" / "work-units"
    preview_dir = view / "state" / "work-units"
    if preview_dir.is_symlink():
        preview_dir.unlink()
        preview_dir.mkdir(parents=True)
        for child in source_dir.iterdir():
            (preview_dir / child.name).symlink_to(child)
    else:
        preview_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / f"layer_{layer_id}.json"
    target = preview_dir / source.name
    if target.is_symlink() or target.exists():
        target.unlink()
    atomic_write(target, source.read_text(encoding="utf-8"))

    apply_replan(
        view,
        layer_id,
        (),
        materialized.layer.stages,
        old_plan_hash=old_plan_hash,
        new_plan_hash=_sha256(view / "layers.json"),
        owner="vfx-harness.candidate-preview",
        trigger="project unpublished materialization through the transactional replan",
        evidence=["scratch/candidate-materialization"],
        state_backed_base=True,
    )


def finalize_materialization_candidate(
    shot_folder: str | Path,
    materialization_path: str | Path,
    consumer_view: str | Path,
    *,
    overlay_root: str | Path | None = None,
):
    """Run the terminal gate and attest only its exact clean candidate revision.

    Local materialization validation, post-publication authority staging, projected
    unit-state reconciliation, the deterministic plan gate, and final attestation are
    one operation. Callers cannot observe CLEAN and then forget to attest the same
    bytes after a patch invalidated an earlier attestation.
    """
    from vfx_harness.evaluation import plan_gate

    shot = Path(shot_folder).resolve()
    candidate = Path(materialization_path)
    stage_candidate_view(
        shot,
        candidate,
        consumer_view,
        overlay_root=overlay_root,
    )
    result = plan_gate.run(Path(consumer_view), require_scene_checks=False)
    if result.clean:
        bundle, _materialized, _bases = _composed_documents(
            shot,
            candidate,
            overlay_root=overlay_root,
        )
        attest_materialization_finalization(
            candidate,
            bundle_hash=bundle.content_hash,
        )
    return result


def publish_materialization(
    shot_folder: str | Path,
    materialization_path: str | Path,
    *,
    overlay_root: str | Path | None = None,
) -> Path:
    """Validate and atomically select one cumulative materialized consumer view."""
    shot = Path(shot_folder).resolve()
    bundle, materialized, bases = _composed_documents(
        shot, materialization_path, overlay_root=overlay_root
    )
    base_layers = bases["layers.json"]
    base_scene = bases["scene_checks.json"]
    base_checks = bases["checks.json"]
    base_requirements = bases["requirements.json"]
    base_acceptance = bases["acceptance.json"]

    documents = _overlay_documents(materialized, bases)
    layers_doc = documents["layers.json"]
    scene_doc = documents["scene_checks.json"]
    checks_doc = documents["checks.json"]
    requirements_doc = documents["requirements.json"]
    acceptance_doc = documents["acceptance.json"]
    digest_payload = json.dumps(
        {
            "bundle": bundle.content_hash,
            "base_artifacts": {
                "layers.json": _sha256(base_layers),
                "scene_checks.json": _sha256(base_scene),
                "checks.json": _sha256(base_checks),
                "requirements.json": _sha256(base_requirements),
                "acceptance.json": _sha256(base_acceptance),
            },
            "layer": materialized.layer_row,
            "scene": materialized.scene_contracts,
            "image": materialized.image_contracts,
            "acceptance": materialized.acceptance,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    view_hash = hashlib.sha256(digest_payload).hexdigest()
    view = shot / STATE_DIR / "views" / view_hash
    view.mkdir(parents=True, exist_ok=True)
    for name, document in (
        ("layers.json", layers_doc),
        ("scene_checks.json", scene_doc),
        ("checks.json", checks_doc),
        ("requirements.json", requirements_doc),
        ("acceptance.json", acceptance_doc),
    ):
        atomic_write(view / name, json.dumps(document, indent=2, sort_keys=True) + "\n")
    pointer = {
        "schema": VIEW_SCHEMA,
        "bundle_hash": bundle.content_hash,
        "view_hash": view_hash,
        "materialized_layers": sorted(
            str(row.get("id"))
            for row in layers_doc["layers"]
            if row.get("execution") != "jit_deferred"
        ),
        "artifacts": {
            name: (view / name).relative_to(shot).as_posix() for name in OVERLAY_ARTIFACTS
        },
        "hashes": {name: _sha256(view / name) for name in OVERLAY_ARTIFACTS},
    }
    atomic_write(shot / CURRENT, json.dumps(pointer, indent=2, sort_keys=True) + "\n")
    return shot / CURRENT
