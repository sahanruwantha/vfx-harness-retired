"""Pinned materialization of one globally deferred layer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vfx_harness.domain.json_pointer import encode as json_ptr
from vfx_harness.domain.json_pointer import format_finding
from vfx_harness.domain.judgment_debts import (
    JudgmentDebtActivation,
    JudgmentDebtDefinition,
)
from vfx_harness.domain.plan_records import (
    load_active_structured_decisions,
    load_judgment_debt_catalog,
)
from vfx_harness.domain.work_units import (
    STRUCTURAL_CLAIM_DOMAINS,
    parse_evidence_domains,
    unit_requires_surface_visibility,
)
from vfx_harness.evidence.scene_checks import KIND_DOMAINS
from vfx_harness.orchestration.jit_materialization.judgment_authority import (
    compile_materialized_judgment_activations,
    compile_materialized_judgment_definition,
)
from vfx_harness.orchestration.jit_materialization.schema import (
    MaterializedLayer,
    _document,
    _matches_reserved,
    _rows,
)


def note_required_claim_metric_domains(*, note, unit, unit_index, claim, all_contracts) -> None:
    """Every required structural claim binding must certify that claim's domain."""
    if claim.asserts is None:
        note(
            json_ptr("layer", "stages", unit_index, "evaluation", "claims"),
            f"required claim {claim.id} must declare `asserts` — the evidence "
            "domain its proposition lives in — so its metrics can be checked "
            "against what they can certify",
        )
        return
    if claim.asserts not in STRUCTURAL_CLAIM_DOMAINS:
        return  # image/human evidence is candidate-bound, proved at build time

    def _domain_of(binding_id: str) -> str:
        entry = all_contracts.get(binding_id)
        if entry is None:
            return "unknown"
        return KIND_DOMAINS.get(str(entry[1].get("kind")), "unknown")

    bound = {binding.id: _domain_of(binding.id) for binding in claim.evidence}
    incompatible = {binding_id: domain for binding_id, domain in bound.items() if domain != claim.asserts}
    if incompatible:
        summary = ", ".join(f"{cid}={domain}" for cid, domain in incompatible.items())
        note(
            json_ptr("layer", "stages", unit_index, "evaluation", "claims"),
            f"claim {claim.id} asserts {claim.asserts!r} but carries padding "
            f"evidence that cannot certify that domain ({summary}); every "
            f"required evidence binding must be a {claim.asserts} metric. Put "
            "cross-domain observations in composition_context or split the "
            "proposition into separately typed claims",
        )


def recorded_vocabulary_gap_ids(root) -> dict[str, tuple[str, ...]]:
    """Vocabulary-gap ids this shot recorded, by requirement id.

    ``escalate_vocabulary_gap`` appends one JSON row per gap; a malformed or missing file
    means no gap, never a crash in a validator.
    """
    path = Path(root) / "state" / "plan-escalations" / "vocabulary-gaps.jsonl"
    gaps: dict[str, list[str]] = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("schema") != "vfx-harness.vocabulary-gap/v1":
            continue
        requirement_id = str(row.get("requirement_id") or "")
        gap_id = str(row.get("id") or "")
        if requirement_id and gap_id:
            gaps.setdefault(requirement_id, []).append(gap_id)
    return {key: tuple(value) for key, value in gaps.items()}


def validate_requirement_closure(
    *,
    note,
    payload,
    scene_rows,
    all_contracts,
    image_debt_ids,
    requirement_contract_ids,
    reserved,
    jit,
    layer_id,
    layer,
    layer_row,
    global_layer_row,
    global_layers,
    parsed_layers,
    provider_scene_rows,
    root,
    resolutions_path,
    expected_bundle_hash,
    base_requirements_path,
):
    # A structured human decision whose roles live in this layer's reserved namespaces is
    # adopted HERE: a schema-5 global bundle publishes no contracts, so the global gate
    # only proves an owner exists. The exact executable copy — and its binding to a
    # required producing claim, enforced just above for every contract — lands at the
    # owner's materialization. Adoption is last-write-wins for the selected bundle
    # only: a prior generation's values.contract is inert, and a later superseded or
    # falsified row retires the id (HIR-0028).
    ledger_path = Path(resolutions_path) if resolutions_path is not None else root / "state" / "plan-resolutions.jsonl"
    active_decisions = load_active_structured_decisions(ledger_path, bundle_hash=expected_bundle_hash)
    for decision in active_decisions.values():
        roles = [str(role) for role in (decision.contract.get("roles") or [])]
        if not roles or not all(_matches_reserved(role, reserved) for role in roles):
            continue
        adopted = [
            row
            for row in scene_rows
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
    requirement_judgments: dict[str, dict[str, Any]] = {}
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
        if not requirement_id or requirement_id in seen_requirements:
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
                "hard_constraint",
                "approved_start",
                "planner_start",
                "confirmed_outcome",
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
            judgment = decision.get("judgment")
            if isinstance(judgment, dict):
                requirement_judgments[requirement_id] = dict(judgment)
            elif judgment is not None:
                note(
                    json_ptr("requirement_bindings", index, "decision", "judgment"),
                    f"requirement {requirement_id} decision.judgment must be an object",
                )
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
    register_path = Path(base_requirements_path) if base_requirements_path is not None else root / "requirements.json"
    register_document = _document(register_path)
    register_rows = {str(row.get("id")): row for row in _rows(register_document, "requirements", "requirements.json")}
    try:
        loaded_definitions, loaded_activations = load_judgment_debt_catalog(
            register_path,
            selected_bundle_digest=expected_bundle_hash,
        )
        existing_definitions = list(loaded_definitions)
        existing_activations = list(loaded_activations)
    except ValueError as exc:
        note(json_ptr("requirement_bindings"), str(exc))
        existing_definitions = []
        existing_activations = []
    register = {requirement_id: (row.get("resolution") or {}) for requirement_id, row in register_rows.items()}
    unknown_owned = sorted(rid for rid in owned if rid not in register)
    if unknown_owned:
        note(
            json_ptr("requirement_bindings"),
            "owned requirements missing from the register: " + ", ".join(unknown_owned),
        )
    concrete_owned = sorted(rid for rid in owned if rid in register and register[rid].get("kind") != "deferred_owner")
    if concrete_owned:
        note(
            json_ptr("requirement_bindings"),
            "owned requirements are already resolved concretely in the register: "
            + ", ".join(concrete_owned)
            + " — the bundle's ownership is inconsistent authority; republish the "
            "global plan instead of materializing around it",
        )
    foreign = sorted(
        rid for rid in owned if rid in register and str(register[rid].get("owner_layer") or "") != layer_id
    )
    if foreign:
        note(
            json_ptr("requirement_bindings"),
            "owned requirements are deferred to another layer in the register: " + ", ".join(foreign),
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
            "image" if binding_kind == "image_contract" else KIND_DOMAINS.get(str(row.get("kind") or ""), "unknown")
        )
    contract_domains.update(dict.fromkeys(image_debt_ids, "image"))
    qualitative_domains = {"image", "human"}
    # A recorded vocabulary gap is the harness's own evidence that no registry metric can
    # express a statement; without it in view this validator refused the decision path its
    # own escalate_vocabulary_gap tool had just prescribed, and the only shape that passed
    # bound twelve bbox_height rows to a text-absence proposition (caesar run
    # 20260904T143311Z-c0f282 R20, HIR-0202).
    vocabulary_gap_ids = recorded_vocabulary_gap_ids(root)
    judgment_debt_definitions: list[JudgmentDebtDefinition] = []
    judgment_definition_by_requirement: dict[str, JudgmentDebtDefinition] = {}
    known_debt_ids = {definition.debt_id for definition in existing_definitions}

    for requirement_id in sorted(owned & bound):
        resolution = register.get(requirement_id) or {}
        authored_statement = str((register_rows.get(requirement_id) or {}).get("statement") or "").strip()
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
            witnesses = ", ".join(f"{contract_id}={domain}" for contract_id, domain in undeclared_contracts.items())
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
        gap_ids = vocabulary_gap_ids.get(requirement_id, ())
        decision_domains = {domain for domain in declared if domain in qualitative_domains and not by_domain[domain]}
        if gap_ids:
            # The gap enumerates the kinds tried and why each cannot certify, which is a
            # stronger statement of inexpressibility than a bare decision: it lets the
            # decision pay every domain the requirement still owes, structural included.
            decision_domains |= {domain for domain in declared if not by_domain[domain]}
        if decision and not decision_domains:
            unpaid_qualitative = [domain for domain in declared if domain in qualitative_domains]
            if unpaid_qualitative:
                note(
                    json_ptr("requirement_bindings"),
                    f"requirement {requirement_id} carries a decision but every declared "
                    f"qualitative domain {unpaid_qualitative} already has contract evidence; "
                    "remove the padding decision",
                )
            else:
                note(
                    json_ptr("requirement_bindings"),
                    f"requirement {requirement_id} declares only structural domains "
                    f"{list(declared)}, which a decision cannot pay: bind a same-domain "
                    "registry contract that measures the statement, or, when no registry "
                    "metric can express it, call escalate_vocabulary_gap for this "
                    "requirement first — a recorded gap makes the decision legal here",
                )
        if gap_ids and not decision:
            note(
                json_ptr("requirement_bindings"),
                f"requirement {requirement_id} has recorded vocabulary gap(s) "
                f"{list(gap_ids)} asserting that no registry metric can express its "
                f"statement, so it cannot then be closed by contract bindings "
                f"{list(contract_ids) or '(none)'}: bind an approved_start or planner_start "
                "decision carrying the authored statement, or retract the gap if those "
                "metrics do measure the proposition",
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
        if decision and "image" in decision_domains and layer is not None:
            try:
                definition = compile_materialized_judgment_definition(
                    requirement_judgments.get(requirement_id),
                    requirement_id=requirement_id,
                    statement=authored_statement,
                    decision_strength=decision["decision_strength"],
                    layer=layer,
                    global_layer=global_layer_row,
                    global_layers=global_layers,
                    parsed_layers=parsed_layers,
                    scene_rows=provider_scene_rows,
                    bundle_digest=expected_bundle_hash,
                )
                if definition.debt_id in known_debt_ids:
                    raise ValueError(
                        f"judgment debt id {definition.debt_id} already exists in selected "
                        "authority; rematerialization must retire or preserve it through "
                        "the typed lineage transaction"
                    )
                known_debt_ids.add(definition.debt_id)
                judgment_debt_definitions.append(definition)
                judgment_definition_by_requirement[requirement_id] = definition
            except ValueError as exc:
                note(
                    json_ptr("requirement_bindings"),
                    f"requirement {requirement_id} judgment debt is invalid: {exc}",
                )
        covered = {domain for domain, ids in by_domain.items() if ids}
        if decision:
            covered.update(decision_domains)
        missing_domains = sorted(set(declared) - covered)
        if missing_domains:
            witnesses = (
                ", ".join(
                    f"{contract_id}={contract_domains.get(contract_id, 'unknown')}" for contract_id in contract_ids
                )
                or "no contract ids"
            )
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
                provisional_row = {
                    "domain": domain,
                    "kind": "provisional_decision",
                    "statement": authored_statement,
                    "decision_strength": decision["decision_strength"],
                }
                definition = judgment_definition_by_requirement.get(requirement_id)
                if domain == "image" and definition is not None:
                    provisional_row.update(
                        debt_id=definition.debt_id,
                        definition_digest=definition.digest,
                        activates_at=definition.binding.activates_at,
                    )
                domain_rows.append(provisional_row)
        requirement_evidence_domains[requirement_id] = declared
        requirement_domain_bindings[requirement_id] = tuple(domain_rows)

    definitions_by_digest = {
        definition.digest: definition for definition in (*existing_definitions, *judgment_debt_definitions)
    }
    for activation in existing_activations:
        definition = definitions_by_digest.get(activation.definition_digest)
        if definition is None:
            note(
                json_ptr("requirement_bindings"),
                "selected judgment-debt activation names an unknown definition: " + activation.definition_digest,
            )
            continue
        try:
            activation.assert_matches(definition)
        except ValueError as exc:
            note(json_ptr("requirement_bindings"), str(exc))
    judgment_debt_activations: tuple[JudgmentDebtActivation, ...] = ()
    if layer is not None:
        try:
            judgment_debt_activations = compile_materialized_judgment_activations(
                tuple(definitions_by_digest.values()),
                tuple(existing_activations),
                layer_id=layer_id,
                global_layers=global_layers,
                parsed_layers=parsed_layers,
                scene_rows=provider_scene_rows,
            )
        except ValueError as exc:
            note(
                json_ptr("requirement_bindings"),
                f"judgment-debt payer activation is invalid: {exc}",
            )

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
        str(row.get("id") or "<missing>") for row in acceptance if row.get("frame") not in judge_frames
    )
    if bad_acceptance:
        note(
            json_ptr("acceptance"),
            "materialized acceptance rows must use this layer's judge frames: " + ", ".join(bad_acceptance),
        )
    # A layer judged at a frame nobody proved shows its subject is judged on faith:
    # run 20260825 sealed a whole lookdev layer whose every judged surface sat behind
    # a solid proxy disc at both judge frames — projection-only bbox rows pass through
    # occluders and layer 2 carried no context rows at f72/f150 at all. Every judge
    # frame must carry occlusion-true visibility evidence for what the frame judges.

    surface_visibility_due = bool(
        layer is not None and any(unit_requires_surface_visibility(unit) for unit in layer.stages)
    )
    uncovered = (
        sorted(
            str(frame)
            for frame in judge_frames
            if not any(row.get("kind") == "visible_fraction" and row.get("frame") == frame for row in scene_rows)
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

    return (
        requirement_bindings,
        requirement_decisions,
        requirement_evidence_domains,
        requirement_domain_bindings,
        tuple(definition.as_dict() for definition in judgment_debt_definitions),
        tuple(activation.as_dict() for activation in judgment_debt_activations),
        acceptance,
    )


def complete_validated_layer(
    *,
    note,
    findings,
    payload,
    scene_rows,
    image_rows,
    all_contracts,
    image_debt_ids,
    requirement_contract_ids,
    reserved,
    jit,
    layer_id,
    layer,
    layer_row,
    global_layer_row,
    global_layers,
    parsed_layers,
    provider_scene_rows,
    root,
    resolutions_path,
    expected_bundle_hash,
    base_requirements_path,
):
    (
        requirement_bindings,
        requirement_decisions,
        requirement_evidence_domains,
        requirement_domain_bindings,
        judgment_debt_definitions,
        judgment_debt_activations,
        acceptance,
    ) = validate_requirement_closure(
        note=note,
        payload=payload,
        scene_rows=scene_rows,
        all_contracts=all_contracts,
        image_debt_ids=image_debt_ids,
        requirement_contract_ids=requirement_contract_ids,
        reserved=reserved,
        jit=jit,
        layer_id=layer_id,
        layer=layer,
        layer_row=layer_row,
        global_layer_row=global_layer_row,
        global_layers=global_layers,
        parsed_layers=parsed_layers,
        provider_scene_rows=provider_scene_rows,
        root=root,
        resolutions_path=resolutions_path,
        expected_bundle_hash=expected_bundle_hash,
        base_requirements_path=base_requirements_path,
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
        judgment_debt_definitions,
        judgment_debt_activations,
        tuple(acceptance),
    )
