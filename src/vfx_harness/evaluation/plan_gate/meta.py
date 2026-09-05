"""The plan gate — the deterministic bar a plan must clear before anything is built on it.

The plan is the specification every later stage is judged against, and it was the only
artifact nothing checked. `ledger.load_layers` reads `layers.json` for shape; past that, a
plan could claim anything and the first thing to notice would be a layer thrashing against
a target that was never reachable.

The organising finding, measured across both shots and all four plan documents:

    spike citations      PRECISE and TRUE. `spike_05.out` line 9 is cited for "the default
                         is 100" and line 9 reads `volumetric_start/end: ... 100.0`.
    web research         0 URLs. Step 6 of the planner prompt requires "source links in
                         the ticket". Not one plan contains a link.
    ask_supervisor       never fired across two shots.
    [unknown] tags       0, in every document, and research is gated on that tag.

The difference is not diligence. `spike` WRITES A FILE to the lab directory; `WebSearch`
leaves nothing behind unless the model chooses to type a URL. Evidence a tool physically
deposits survives and stays checkable. Evidence the model is merely asked to record does
not. Every check here is therefore a check on an ARTIFACT, never on a claim about one.

The checks, each free and each with a known-bad fixture in the test suite:

    grounded      every stated fingerprint number re-derives from the plate it describes
                  (eval.grounding — 153/160 on the shipped plans; the 7 failures were one
                  broken metric, not an inventive planner)
    citations     every file a plan cites RESOLVES, and a cited line number exists in it.
                  Not fabrication — link rot: `server_to_hansa/plan.md` cites
                  `../barrel_roll/logs/plan_lab_draft/spike_05.out` five times and that
                  path stopped existing when the shot was archived. The claims are true and
                  a build agent reading them today gets nothing.
    evidence      a ticket claiming `✓spiked` must cite a lab file; a ticket claiming
                  research must leave a source link. A confidence tag is a self-assessment
                  and worth exactly the artifact under it.
    done-checks   do the plan's own checks WORK — is each satisfiable by the reference it
                  names, and does it REJECT a known-bad render? Grounding asks whether the
                  numbers are real; this asks whether the checks can fail anything. Both are
                  needed: this plan scored 95/95 on grounding while carrying a check its own
                  plate cannot pass. See checks.py — the contract, re-run by the gate.
    contracts     plans/global.md, the next just-in-time work-unit plan, layers.json,
                  acceptance.json, critic_axes.json and live-scene
                  contracts must agree: an axis a layer OWNS must exist in the rubric,
                  every ref must be real, and a scene fact must belong to a frame/axis its
                  layer actually judges. A layer owning an axis the critic does not score
                  cannot be judged on it, and nothing else in the pipeline notices.

Severity is either `blocking` (a build on this plan is aiming at something that is not
there) or `warn` (the plan is weaker than it claims, but buildable).

Exit codes: 0 clean · 3 at least one blocking finding
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.contracts import load_document
from vfx_harness.domain.plan_records import (
    brief_clause_spans,
    load_active_structured_decisions,
    load_assumptions,
    load_judgment_debt_activations,
    load_judgment_debt_definitions,
    load_obligations,
    load_requirements,
    read_selected_bundle_hash,
    resolution_decision_strength,
)
from vfx_harness.domain.vocabulary_gaps import (
    decision_may_pay_domain,
    recorded_vocabulary_gap_ids,
)
from vfx_harness.domain.work_units import (
    EVIDENCE_DOMAINS,
    REQUIREMENT_DOMAIN_COVERAGE_FIX,
    layers_covering_evidence_domains,
    parse_evidence_domains,
    read_document,
    requirement_domain_coverage_what,
    uncovered_evidence_domains,
)
from vfx_harness.evaluation.plan_gate.types import (
    Finding,
    _materialized_view,
)
from vfx_harness.evidence.scene_checks import KIND_DOMAINS, TEMPORAL_KINDS


def _check_meta_records(folder: Path) -> tuple[list[Finding], dict]:
    """Close every registered brief requirement through typed authority."""

    names = ("requirements.json", "obligations.json", "assumptions.json")
    missing = [name for name in names if not (folder / name).is_file()]
    if missing:
        return [Finding(
            "requirement-closure",
            True,
            ", ".join(missing),
            "typed planning meta-records are missing",
            "extract normative brief requirements and resolve each to exact contracts, "
            "deferred obligations, or an explicit decision",
        )], {}
    try:
        requirements = load_requirements(folder)
        selected_bundle = read_selected_bundle_hash(folder)
        judgment_debt_definitions = load_judgment_debt_definitions(
            folder,
            requirements=requirements,
            selected_bundle_digest=selected_bundle,
        )
        load_judgment_debt_activations(
            folder,
            definitions=judgment_debt_definitions,
        )
        obligations = load_obligations(folder)
        assumptions = load_assumptions(folder)
        scene_rows = load_document(folder / "scene_checks.json", "contracts")
        image_rows = load_document(folder / "checks.json", "checks")
        scene_ids = {str(row.get("id")) for row in scene_rows}
        image_ids = {str(row.get("id")) for row in image_rows}
        layers = read_document(folder / "layers.json")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [Finding("requirement-closure", True, "typed meta-records", str(exc))], {}

    findings: list[Finding] = []
    cited_lines = {
        line
        for requirement in requirements
        for line in range(requirement.line_start, requirement.line_end + 1)
    }
    uncovered_clauses = [
        (start, end, text)
        for start, end, text in brief_clause_spans(folder / "brief.md")
        if not any(line in cited_lines for line in range(start, end + 1))
    ]
    if uncovered_clauses:
        spans = ", ".join(
            str(start) if start == end else f"{start}-{end}"
            for start, end, _text in uncovered_clauses
        )
        findings.append(Finding(
            "requirement-completeness",
            True,
            "requirements.json",
            f"substantive brief clauses have no register citation: lines {spans}",
            "add one or more typed requirement entries whose citations cover every listed "
            "brief clause, then resolve each through a contract, obligation, or decision",
        ))
    requirement_ids = {row.id for row in requirements}
    obligation_ids = {row.id for row in obligations}
    contract_ids = scene_ids | image_ids
    contract_owner_layers = {
        str(row.get("id")): str(row.get("owner_layer"))
        for row in (*scene_rows, *image_rows)
        if row.get("id") is not None and row.get("owner_layer") is not None
    }
    layer_units = {
        str(layer.get("id")): {
            str(unit.get("id"))
            for unit in layer.get("stages") or []
            if isinstance(unit, dict)
        }
        for layer in layers
        if isinstance(layer, dict)
    }
    deferred_layers = {
        str(layer.get("id")): layer
        for layer in layers
        if isinstance(layer, dict) and layer.get("execution") == "jit_deferred"
    }
    requirement_owners: dict[str, list[str]] = {}
    for layer_id, layer in deferred_layers.items():
        for requirement_id in map(
            str, (layer.get("jit") or {}).get("owned_requirements") or []
        ):
            requirement_owners.setdefault(requirement_id, []).append(layer_id)
    unit_dependencies = {
        (str(layer.get("id")), str(unit.get("id"))): set(map(str, unit.get("depends_on") or []))
        for layer in layers
        if isinstance(layer, dict)
        for unit in layer.get("stages") or []
        if isinstance(unit, dict)
    }
    contract_producers = {
        str(binding.get("id")): (str(layer.get("id")), str(unit.get("id")))
        for layer in layers
        if isinstance(layer, dict)
        for unit in layer.get("stages") or []
        if isinstance(unit, dict)
        for claim in (unit.get("evaluation") or {}).get("claims") or []
        if isinstance(claim, dict)
        for binding in claim.get("evidence") or []
        if isinstance(binding, dict) and binding.get("id") is not None
    }
    required_contract_producers = {
        str(binding.get("id")): (str(layer.get("id")), str(unit.get("id")))
        for layer in layers
        if isinstance(layer, dict)
        for unit in layer.get("stages") or []
        if isinstance(unit, dict)
        for claim in (unit.get("evaluation") or {}).get("claims") or []
        if isinstance(claim, dict) and claim.get("required") is True
        for binding in claim.get("evidence") or []
        if isinstance(binding, dict) and binding.get("id") is not None
    }

    for assumption in assumptions:
        if assumption.decision_strength not in {"approved_start", "planner_start"}:
            continue
        owner = str(assumption.falsification_owner or "")
        if "." not in owner:
            findings.append(Finding(
                "decision-strength", True, assumption.id,
                f"falsification owner {owner!r} is not a layer.unit owner",
                "name the earliest producing work unit that evaluates the runtime contracts",
            ))
            continue
        owner_layer, owner_unit = owner.split(".", 1)
        if owner_unit not in layer_units.get(owner_layer, set()):
            findings.append(Finding(
                "decision-strength", True, assumption.id,
                f"falsification owner {owner!r} does not exist in the work-unit DAG",
                "bind the provisional start to a real producing unit",
            ))
        unknown = sorted(set(assumption.falsification_contract_ids) - contract_ids)
        if unknown:
            findings.append(Finding(
                "decision-strength", True, assumption.id,
                "falsification path names unknown contracts: " + ", ".join(unknown),
                "declare the exact runtime contracts and bind them to required unit claims",
            ))
        wrong_owner = sorted(
            contract_id
            for contract_id in assumption.falsification_contract_ids
            if contract_id in contract_producers and contract_producers[contract_id] != (owner_layer, owner_unit)
        )
        if wrong_owner:
            findings.append(Finding(
                "decision-strength", True, assumption.id,
                "falsification contracts are produced by another unit: " + ", ".join(wrong_owner),
                "move the falsification owner or contract bindings so one unit owns first contact",
            ))

    # A human-approved calibration is durable authority only when its operative values
    # are self-contained.  Structured values in the append-only resolution ledger must
    # be copied exactly into one required executable contract; a prose paraphrase or a
    # pointer to an unavailable prior plan is not an adoption mechanism. Adoption is
    # last-write-wins for the selected bundle: another generation's values.contract is
    # inert, and a later superseded or falsified row retires the id (HIR-0028).
    recorded_gaps = recorded_vocabulary_gap_ids(folder)
    decision_path = folder / "state" / "plan-resolutions.jsonl"

    if decision_path.is_file():
        for line_no, line in enumerate(
            decision_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                findings.append(Finding(
                    "decision-adoption", True, f"state/plan-resolutions.jsonl:{line_no}",
                    f"invalid JSON: {exc}",
                ))
                continue
            try:
                resolution_decision_strength(
                    row, f"state/plan-resolutions.jsonl:{line_no}"
                )
            except ValueError as exc:
                findings.append(Finding(
                    "decision-strength", True,
                    f"state/plan-resolutions.jsonl:{line_no}", str(exc),
                ))
                continue
            if str(row.get("bundle_hash") or "") != (selected_bundle or ""):
                continue
            if row.get("status") != "satisfied" or not isinstance(row.get("values"), dict):
                continue
            contract = row["values"].get("contract")
            if isinstance(contract, dict) and contract:
                continue
            findings.append(Finding(
                "decision-adoption", True, f"state/plan-resolutions.jsonl:{line_no}",
                "structured resolution must provide values.contract",
                "embed the complete executable contract fields in the approved resolution",
            ))

    structured_decisions: dict[str, tuple[int, dict]] = {}
    if selected_bundle:
        for decision in load_active_structured_decisions(
            decision_path, bundle_hash=selected_bundle
        ).values():
            structured_decisions[decision.id] = (decision.line_no, decision.contract)

    try:
        unit_first_layers = json.loads((folder / "layers.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        unit_first_layers = None
    unit_first = isinstance(unit_first_layers, dict) and unit_first_layers.get("schema") == 5

    materialized_ids, pinned_overlays = _materialized_view(folder) if unit_first else (set(), set())
    for decision_id, (line_no, expected) in structured_decisions.items():
        if unit_first:
            # A schema-5 bundle publishes no contracts, so exact adoption cannot happen
            # here — validate_materialization enforces the verbatim copy when the owning
            # layer materializes. The global obligation is ownership: some deferred
            # layer's reserved namespaces must cover every role the decision mutates,
            # or the approved values have nowhere to land and would silently vanish.
            # Once the reserving layer HAS materialized it is no longer deferred, and
            # the obligation transfers to the adoption itself: a pinned materialized
            # contract carrying this decision_id with the exact approved values.
            roles = [str(role) for role in (expected.get("roles") or [])]
            deferred_reserved = {
                str(row.get("id")): [
                    str(pattern)
                    for pattern in ((row.get("jit") or {}).get("reserved_roles") or [])
                ]
                for row in layers
                if isinstance(row, dict) and row.get("execution") == "jit_deferred"
            }
            owners = sorted(
                layer_id
                for layer_id, patterns in deferred_reserved.items()
                if roles
                and all(
                    any(fnmatch.fnmatchcase(role, pattern) for pattern in patterns)
                    for role in roles
                )
            )
            adopted = "scene_checks.json" in pinned_overlays and any(
                isinstance(row, dict)
                and str(row.get("decision_id") or "") == decision_id
                and str(row.get("owner_layer") or "") in materialized_ids
                and all(row.get(key) == value for key, value in expected.items())
                for row in scene_rows
            )
            if not owners and not adopted:
                findings.append(Finding(
                    "decision-adoption",
                    True,
                    f"state/plan-resolutions.jsonl:{line_no} ({decision_id})",
                    "no deferred layer reserves this decision's roles and no materialized "
                    "layer adopts it verbatim: " + (", ".join(roles) or "(none declared)"),
                    "reserve the decision's roles in the owning deferred layer; its "
                    "materialization must copy values.contract into scene_contracts "
                    "with decision_id",
                ))
            continue
        candidates = [
            row for row in scene_rows
            if isinstance(row, dict) and str(row.get("decision_id") or "") == decision_id
        ]
        exact = [
            row for row in candidates
            if all(row.get(key) == value for key, value in expected.items())
        ]
        if not exact:
            detail = (
                "has no scene contract carrying decision_id and the exact approved values"
                if not candidates
                else "was altered while being copied into its scene contract"
            )
            findings.append(Finding(
                "decision-adoption",
                True,
                f"state/plan-resolutions.jsonl:{line_no} ({decision_id})",
                detail,
                "copy values.contract exactly into a scene contract, preserve decision_id, "
                "and bind that contract to a required claim in its owning unit",
            ))
            continue
        unbound = sorted(
            str(row.get("id") or "<missing>")
            for row in exact
            if str(row.get("id") or "") not in required_contract_producers
        )
        if unbound:
            findings.append(Finding(
                "decision-adoption",
                True,
                f"structured decision {decision_id}",
                "approved values are not required unit evidence: " + ", ".join(unbound),
                "bind the adopted contract to a required executable claim in its producing unit",
            ))

    obligation_by_id = {record.id: record for record in obligations}
    brief_lines = (folder / "brief.md").read_text(encoding="utf-8").splitlines()
    try:

        terminal_frame = int(load_shot(folder).frames)
    except (OSError, ValueError, TypeError):
        terminal_frame = 0

    def terminal_hold_window(text: str) -> tuple[int, int] | None:
        normalized = " ".join(text.lower().split())
        explicit = re.search(
            r"unchanged\s+from\s+frame\s+(\d+)\s+(?:to|through|[-–—])\s*(?:frame\s+)?(\d+)",
            normalized,
        )
        if explicit:
            return int(explicit.group(1)), int(explicit.group(2))
        count_match = re.search(
            r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)[ -]frame\b"
            r".{0,80}\b(?:visual\s+)?(?:lock|hold)\b",
            normalized,
        )
        if not count_match or not re.search(r"\b(?:end|ends|ending|final)\b", normalized):
            return None
        words = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        count_text = count_match.group(1)
        count = words.get(count_text, int(count_text) if count_text.isdigit() else 0)
        if count < 2 or terminal_frame < count:
            return None
        return terminal_frame - count + 1, terminal_frame

    clauses = brief_clause_spans(folder / "brief.md")

    def resolved_scene_ids(start: int, end: int) -> set[str]:
        resolved: set[str] = set()
        for requirement in requirements:
            if requirement.line_end < start or requirement.line_start > end:
                continue
            if requirement.resolution_kind == "contract":
                resolved.update(requirement.resolution_ids)
            elif requirement.resolution_kind == "obligation":
                for obligation_id in requirement.resolution_ids:
                    obligation = obligation_by_id.get(obligation_id)
                    if obligation is not None:
                        resolved.update(
                            evidence_id
                            for kind, evidence_id in obligation.evidence
                            if kind == "scene_contract"
                        )
        return resolved

    def has_deferred_owner(start: int, end: int) -> bool:
        return any(
            requirement.resolution_kind == "deferred_owner"
            for requirement in requirements
            if requirement.line_end >= start and requirement.line_start <= end
        )

    for start, end, clause in clauses:
        window = terminal_hold_window(clause)
        if window is None:
            continue
        resolved_ids = resolved_scene_ids(start, end)
        matching = {
            contract_id
            for contract_id in resolved_ids
            if contract_id in scene_ids
            and next(
                (
                    row.get("kind") == "frame_delta"
                    and tuple(row.get("frames") or ()) == window
                    for row in scene_rows
                    if str(row.get("id")) == contract_id
                ),
                False,
            )
            and contract_id in required_contract_producers
        }
        if not matching and not has_deferred_owner(start, end):
            source = " ".join(brief_lines[start - 1:end]).strip()
            findings.append(Finding(
                "temporal-requirement",
                True,
                f"brief.md lines {start}-{end}",
                f"terminal hold {window[0]}→{window[1]} is not resolved by a required "
                f"frame_delta contract ({source[:120]})",
                "bind the cited requirement directly, or through an obligation, to a "
                "frame_delta contract over the derived terminal window and make that "
                "contract required evidence in its producing unit. In a sparse bundle that "
                "publishes no contracts, resolve the clause as a deferred_owner requirement "
                "naming the owning layer",
            ))

    motion_language = re.compile(
        r"(?:\bchase\b|\bstaggered\s+sequence\b|\bcontinuous\s+(?:camera\s+)?(?:move|path)\b|"
        r"\bdependency\s+order\b|\btravels?\s+through\b|\bmoving\s+front\b|"
        r"\brather\s+than\s+all\s+at\s+once\b|\bmove\w*\s+outward\s+first\b|"
        r"\breverse\w*\s+(?:the\s+)?(?:debris\s+)?trajector\w*\b|"
        r"\binherit\w*\s+the\s+direction\s+and\s+timing\b)",
        re.IGNORECASE,
    )
    executable_temporal_ids = {
        str(row.get("id"))
        for row in scene_rows
        if isinstance(row, dict)
        and row.get("kind") in TEMPORAL_KINDS | {"frame_delta"}
        and str(row.get("id") or "") in required_contract_producers
    }
    for start, end, clause in clauses:
        if not motion_language.search(" ".join(clause.split())):
            continue
        resolved_ids = resolved_scene_ids(start, end)
        if resolved_ids & executable_temporal_ids:
            continue
        if has_deferred_owner(start, end):
            continue
        source = " ".join(brief_lines[start - 1:end]).strip()
        findings.append(Finding(
            "temporal-requirement",
            True,
            f"brief.md lines {start}-{end}",
            f"explicit motion law has no required temporal contract ({source[:120]})",
            "bind the cited requirement directly, or through an obligation, to required "
            "onset_order, radial_distance_trend, transform_return_delta, keyframe_schedule, "
            "or frame_delta evidence; a unit evidence label or single-frame proxy cannot "
            "prove motion. In a sparse bundle that publishes no contracts, resolve the "
            "clause as a deferred_owner requirement naming the layer that will bind this "
            "evidence at its materialization",
        ))

    fracture_start = 0
    for _start, _end, clause in clauses:
        normalized = " ".join(clause.lower().split())
        frame_range = re.search(r"\b(\d+)\s*[–—-]\s*(\d+)\b", normalized)
        if frame_range and re.search(r"\bfractur(?:e|es|ed|ing)\b", normalized):
            fracture_start = int(frame_range.group(1))
            break
    if fracture_start > 1 and terminal_frame >= fracture_start:
        return_window = (fracture_start - 1, terminal_frame)
        exact_return = re.compile(
            r"(?:return\w*\s+to\s+(?:their\s+)?exact\s+structural\s+positions|"
            r"return\w*\s+to\s+unrelated\s+locations|reassembl\w*\s+into\s+the\s+same\s+architecture)",
            re.IGNORECASE,
        )
        for start, end, clause in clauses:
            if not exact_return.search(" ".join(clause.split())):
                continue
            resolved_ids = resolved_scene_ids(start, end)
            matching = {
                contract_id
                for contract_id in resolved_ids
                if contract_id in scene_ids
                and next(
                    (
                        row.get("kind") == "transform_return_delta"
                        and tuple(row.get("frames") or ()) == return_window
                        for row in scene_rows
                        if str(row.get("id")) == contract_id
                    ),
                    False,
                )
                and contract_id in required_contract_producers
            }
            if not matching and not has_deferred_owner(start, end):
                source = " ".join(brief_lines[start - 1:end]).strip()
                findings.append(Finding(
                    "temporal-requirement",
                    True,
                    f"brief.md lines {start}-{end}",
                    f"exact reassembly is not resolved by a required transform_return_delta "
                    f"from the pre-fracture baseline {return_window[0]} to final frame "
                    f"{return_window[1]} ({source[:120]})",
                    "bind the cited requirement directly, or through an obligation, to a "
                    "transform_return_delta contract over the derived pre-fracture-to-final "
                    "window; a partial-reassembly frame cannot establish exact return. In a "
                    "sparse bundle that publishes no contracts, resolve the clause as a "
                    "deferred_owner requirement naming the owning layer",
                ))

    def upstream_units(layer_id: str, unit_id: str) -> set[str]:
        out: set[str] = set()
        pending = list(unit_dependencies.get((layer_id, unit_id), set()))
        while pending:
            candidate = pending.pop()
            if candidate in out:
                continue
            out.add(candidate)
            pending.extend(unit_dependencies.get((layer_id, candidate), set()))
        return out


    layer_domains: dict[str, tuple[str, ...]] = {}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        domains = layer.get("evidence_domains")
        lid = str(layer.get("id") or "")
        if not isinstance(domains, list) or not domains:
            findings.append(Finding.in_layer(
                "requirement-closure", True, layer.get('id', '?'), "",
                "evidence_domains is missing",
                "declare the typed evidence domains this layer requires; composition "
                "coverage must not be inferred from axis-name keywords",
            ))
        elif unknown_domains := sorted(set(map(str, domains)) - EVIDENCE_DOMAINS):
            findings.append(Finding.in_layer(
                "requirement-closure", True, layer.get('id', '?'), "",
                "unknown evidence domains: " + ", ".join(unknown_domains),
            ))
        elif lid:
            try:
                layer_domains[lid] = parse_evidence_domains(
                    domains, f"layer {lid}.evidence_domains"
                )
            except ValueError as exc:
                findings.append(Finding.in_layer(
                    "requirement-closure", True, lid, "", str(exc),
                ))
    for requirement in requirements:
        if requirement.resolution_kind != "deferred_owner":
            continue
        declared = requirement.evidence_domains
        owner = str(requirement.owner_layer or "")
        owner_cov = layer_domains.get(owner, ())
        if not declared:
            findings.append(Finding(
                "requirement-domain-coverage",
                True,
                requirement.id,
                f"deferred_owner {requirement.id} is missing evidence_domains",
                f"declare a non-empty subset of {sorted(EVIDENCE_DOMAINS)}",
            ))
            continue
        if uncovered_evidence_domains(declared, owner_cov):
            covering = layers_covering_evidence_domains(declared, layer_domains)
            findings.append(Finding(
                "requirement-domain-coverage",
                True,
                requirement.id,
                requirement_domain_coverage_what(
                    requirement.id, declared, owner, owner_cov, covering
                ),
                REQUIREMENT_DOMAIN_COVERAGE_FIX,
                layer=owner or None,
            ))
    for requirement in requirements:
        known = obligation_ids if requirement.resolution_kind == "obligation" else contract_ids
        if requirement.resolution_kind in {"contract", "obligation"}:
            unknown = sorted(set(requirement.resolution_ids) - known)
            if unknown:
                findings.append(Finding(
                    "requirement-closure",
                    True,
                    requirement.id,
                    f"resolution names unknown {requirement.resolution_kind} ids: {', '.join(unknown)}",
                    "bind the requirement to exact records present in this candidate bundle",
                ))
        if requirement.domain_bindings:
            actual_domains = {
                str(row.get("id")): KIND_DOMAINS.get(
                    str(row.get("kind") or ""), "unknown"
                )
                for row in scene_rows
                if row.get("id")
            }
            actual_domains.update(dict.fromkeys(image_ids, "image"))
            for domain, binding_kind, binding_ids in requirement.domain_bindings:
                if binding_kind != "contract":
                    if not decision_may_pay_domain(
                        domain, gap_ids=recorded_gaps.get(requirement.id, ())
                    ):
                        findings.append(Finding(
                            "requirement-domain-binding",
                            True,
                            requirement.id,
                            f"provisional decision cannot pay structural domain {domain!r} "
                            "without a recorded vocabulary gap",
                            "bind a registry-backed contract whose metric certifies that "
                            "domain, or record the gap that says none can",
                        ))
                    continue
                mismatched = [
                    f"{contract_id}={actual_domains.get(contract_id, 'missing')}"
                    for contract_id in binding_ids
                    if actual_domains.get(contract_id) != domain
                ]
                if mismatched:
                    findings.append(Finding(
                        "requirement-domain-binding",
                        True,
                        requirement.id,
                        f"domain {domain!r} is bound by incompatible witnesses: "
                        + ", ".join(mismatched),
                        "bind ids whose canonical registry domain exactly matches the "
                        "declared requirement domain; do not relabel a metric",
                    ))
    for records in (obligations, assumptions):
        for record in records:
            unknown_requirements = sorted(set(record.requirement_ids) - requirement_ids)
            if unknown_requirements:
                findings.append(Finding(
                    "requirement-closure", True, record.id,
                    "references unknown requirements: " + ", ".join(unknown_requirements),
                ))
            due = record.due
            if due.kind != "before_acceptance" and due.layer not in layer_units:
                findings.append(Finding(
                    "requirement-closure", True, record.id,
                    f"due gate names unknown layer {due.layer!r}",
                ))
            elif (
                due.kind in {"before_unit", "unit_completion"}
                and due.unit not in layer_units.get(str(due.layer), set())
            ):
                findings.append(Finding(
                    "requirement-closure", True, record.id,
                    f"due gate names unknown unit {due.layer}.{due.unit}",
                ))
            if str(due.layer) in deferred_layers and due.kind != "before_layer":
                findings.append(Finding(
                    "requirement-closure",
                    True,
                    record.id,
                    "deferred-layer debt must be due at the layer boundary, not a future unit",
                    "use before_layer for independently owned upstream debt; JIT promise "
                    "requirements link directly and do not use obligations",
                ))
    for record in assumptions:
        if record.due.kind == "unit_completion" and record.decision_strength not in {
            "approved_start",
            "planner_start",
        }:
            findings.append(Finding(
                "requirement-closure", True, record.id,
                "assumptions cannot be machine-resolved at unit completion",
                "use an obligation with executable evidence, or keep a human-decision assumption due before execution",
            ))
    for requirement_id, owner_layers in requirement_owners.items():
        if requirement_id not in requirement_ids:
            findings.append(Finding(
                "requirement-closure",
                True,
                requirement_id,
                "deferred layer owns an unknown requirement",
            ))
        if len(owner_layers) > 1:
            findings.append(Finding(
                "requirement-closure", True, requirement_id,
                "deferred requirement has multiple owner layers: " + ", ".join(sorted(owner_layers)),
                "assign exactly one owner and due boundary",
            ))
    for requirement in requirements:
        if requirement.resolution_kind != "deferred_owner":
            continue
        owners = requirement_owners.get(requirement.id, [])
        if owners != [str(requirement.owner_layer)]:
            findings.append(Finding(
                "requirement-closure", True, requirement.id,
                f"ownership register names layer {requirement.owner_layer}, but JIT ownership is "
                + (", ".join(owners) if owners else "missing"),
                "list the requirement exactly once in that deferred layer's owned_requirements",
            ))
    # The inverse direction: owned means owed. A concretely-resolved row in a layer's
    # owned_requirements carries no debt, and downstream closure would demand a binding
    # the publish check forbids — bundle a5692e9f shipped this deadlock because only the
    # deferred_owner direction was verified at publication.
    requirements_by_id = {requirement.id: requirement for requirement in requirements}
    for requirement_id in requirement_owners:
        requirement = requirements_by_id.get(requirement_id)
        if requirement is None or requirement.resolution_kind == "deferred_owner":
            continue
        findings.append(Finding(
            "requirement-closure", True, requirement_id,
            "owned_requirements lists a requirement the register already resolves as "
            f"'{requirement.resolution_kind}'; owned means owed and this row carries no debt",
            "remove it from the layer's owned_requirements, or resolve the clause as "
            "deferred_owner naming that layer",
        ))
    for row in (*scene_rows, *image_rows):
        owner = str(row.get("owner_layer") or row.get("activates_at") or "")
        if owner in deferred_layers:
            findings.append(Finding(
                "deferred-overplanning",
                True,
                str(row.get("id") or owner),
                f"deferred layer {owner} carries an executable contract before JIT materialization",
                "replace it with a typed JIT promise linked directly to requirements and "
                "materialize the exact contract "
                "only after upstream checkpoints exist",
            ))
    for record in obligations:
        due = record.due
        if due.kind == "unit_completion":
            expected_owner = (str(due.layer), str(due.unit))
            unavailable = sorted(
                evidence_id
                for kind, evidence_id in record.evidence
                if kind != "replay" and required_contract_producers.get(evidence_id) != expected_owner
            )
            if unavailable:
                findings.append(Finding(
                    "requirement-closure", True, record.id,
                    "unit-completion evidence is not bound to required claims in "
                    f"{due.layer}.{due.unit}: " + ", ".join(unavailable),
                    "bind every completion evidence id to a required claim in the exact "
                    "owning unit; advisory or foreign evidence cannot discharge the gate",
                ))
        if due.kind not in {"before_layer", "before_unit"}:
            continue
        if due.kind == "before_layer":
            circular = sorted(
                evidence_id
                for _kind, evidence_id in record.evidence
                if contract_owner_layers.get(evidence_id) == str(due.layer)
            )
        else:
            upstream = upstream_units(str(due.layer), str(due.unit))
            circular = sorted(
                evidence_id
                for _kind, evidence_id in record.evidence
                if contract_owner_layers.get(evidence_id) == str(due.layer)
                and contract_producers.get(evidence_id, (None, None))[1] not in upstream
            )
        if circular:
            findings.append(Finding(
                "requirement-closure", True, record.id,
                "entry gate depends on evidence produced by the gated layer: " + ", ".join(circular),
                "move machine-verifiable evidence to a unit_completion obligation; "
                "entry gates may depend only on already-produced upstream evidence",
            ))
    return findings, {
        "requirements": len(requirements),
        "open_obligations": len(obligations),
        "open_assumptions": len(assumptions),
    }
