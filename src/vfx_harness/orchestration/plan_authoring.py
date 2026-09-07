"""Mechanical global-plan authoring: the harness owns everything derivable.

Run 6281c8 spent ~$5.74/24.5min authoring duplicated authority across nine files, and
the twelve-row owned-means-owed deadlock shipped precisely because `owned_requirements`
was hand-copied instead of derived. Under this module the model returns ONE compact
ownership/DAG mapping; the harness mechanically generates clause IDs, citations and
exact brief text, empty evidence documents, schema boilerplate, `plans/global.md`, and —
decisively — `owned_requirements` derived from the register, which makes that whole
defect class unrepresentable.

The expansion targets the existing publication contract: an expanded workspace from a
valid mapping passes the deterministic plan gate by construction wherever the gate
checks structure, and the gate's remaining findings (motion-law clauses resolved as
decisions, unowned decision roles) steer the MAPPING, which stays the only surface the
model edits.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from vfx_harness.domain.plan_records import (
    DECISION_STRENGTHS,
    brief_clause_spans,
)
from vfx_harness.domain.work_units import (
    CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE,
    EVIDENCE_DOMAINS,
    GLOBAL_SCENE_CAPABILITIES,
    REQUIREMENT_DOMAIN_COVERAGE_FIX,
    extra_reserved_roles_on_camera_layer,
    layers_covering_evidence_domains,
    parse_evidence_domains,
    requirement_domain_coverage_what,
    uncovered_evidence_domains,
)

MAPPING_SCHEMA = "vfx-harness.ownership-mapping/v1"
MAPPING_ARTIFACTS = (
    "requirements.json", "layers.json", "critic_axes.json", "acceptance.json",
    "checks.json", "scene_checks.json", "obligations.json", "assumptions.json",
    "plans/global.md", "plans/ownership_mapping.json",
)


def ownership_mapping_authoring_schema() -> dict[str, Any]:
    """Closed JSON schema for the planner's ``ownership_mapping.json`` ticket.

    Authoritative validation remains ``validate_mapping`` plus ``load_requirements``.
    The schema enumerates evidence-domain identity before generation (HIR-0124).
    """
    text = {"type": "string", "minLength": 1}
    domain = {"type": "string", "enum": sorted(EVIDENCE_DOMAINS)}
    domains = {
        "type": "array",
        "items": domain,
        "minItems": 1,
        "uniqueItems": True,
        "description": (
            "AND coverage: the owner layer's evidence_domains must include every "
            "value declared here."
        ),
    }
    deferred = {
        "type": "object",
        "properties": {
            "kind": {"const": "deferred_owner"},
            "owner_layer": text,
            "evidence_domains": domains,
        },
        "required": ["kind", "owner_layer", "evidence_domains"],
        "additionalProperties": False,
    }
    decision = {
        "type": "object",
        "properties": {
            "kind": {"const": "decision"},
            "statement": text,
            "decision_strength": {
                "type": "string",
                "enum": sorted(DECISION_STRENGTHS),
            },
        },
        "required": ["kind", "statement", "decision_strength"],
        "additionalProperties": False,
    }
    strings = {"type": "array", "items": text, "uniqueItems": True}
    layer_properties = {
        "id": text, "title": text, "script": text, "charter": text,
        "primary_judge": {"type": "integer"},
        "judge": {"type": "array", "minItems": 1, "items": {
            "type": "object", "properties": {"frame": {"type": "integer"}, "ref": text},
            "required": ["frame", "ref"], "additionalProperties": False,
        }},
        "owns": {**strings, "minItems": 1}, "evidence_domains": domains,
        "depends_on": strings, "reserved_roles": {**strings, "minItems": 1},
        "provides": {
            "type": "object",
            "properties": {key: {**strings, "minItems": 1} for key in sorted(GLOBAL_SCENE_CAPABILITIES)},
            "additionalProperties": False,
        },
    }
    return {
        "type": "object",
        "properties": {
            "schema": {"const": MAPPING_SCHEMA},
            "layers": {"type": "array", "minItems": 1, "items": {
                "type": "object", "properties": layer_properties,
                "required": list(layer_properties), "additionalProperties": False,
            }},
            "axes": {"type": "array", "minItems": 1, "items": {
                "type": "object", "properties": {"key": text, "desc": text},
                "required": ["key", "desc"], "additionalProperties": False,
            }},
            "resolutions": {
                "type": "object",
                "additionalProperties": {
                    "oneOf": [deferred, decision],
                },
            },
            "blockers": {"type": "array", "items": text},
        },
        "required": ["schema", "layers", "axes", "resolutions", "blockers"],
        "additionalProperties": False,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clause_registry(brief_path: str | Path) -> tuple[dict[str, Any], ...]:
    """Every substantive brief clause with a stable mechanical id and citation."""
    brief_path = Path(brief_path)
    digest = _sha256(brief_path)
    return tuple(
        {
            "id": f"R{index + 1}",
            "statement": text,
            "citation": {
                "source": "brief.md",
                "sha256": digest,
                "line_start": start,
                "line_end": end,
            },
        }
        for index, (start, end, text) in enumerate(brief_clause_spans(brief_path))
    )


def registry_prompt_block(registry: tuple[dict[str, Any], ...]) -> str:
    """The registry as the model sees it: ids and exact text, nothing to re-author."""
    lines = [
        f"{row['id']} [lines {row['citation']['line_start']}-"
        f"{row['citation']['line_end']}]: {row['statement']}"
        for row in registry
    ]
    return "\n".join(lines)


def validate_mapping(
    mapping: Any,
    registry: tuple[dict[str, Any], ...],
    refs_dir: str | Path,
) -> list[str]:
    """Mechanical validation with every accepted value named in the error."""
    errors: list[str] = []
    if not isinstance(mapping, dict):
        return ["mapping must be a JSON object"]
    if mapping.get("schema") != MAPPING_SCHEMA:
        errors.append(f"schema must be {MAPPING_SCHEMA!r}")

    layers = mapping.get("layers")
    layer_ids: list[str] = []
    axis_keys: set[str] = set()
    axes = mapping.get("axes")
    if not isinstance(axes, list) or not axes:
        errors.append("axes must be a non-empty list of {key, desc}")
        axes = []
    for index, axis in enumerate(axes):
        if not isinstance(axis, dict) or not str(axis.get("key") or "").strip():
            errors.append(f"axes[{index}].key must be a non-empty string")
            continue
        key = str(axis["key"]).strip()
        if key in axis_keys:
            errors.append(f"axes[{index}].key duplicates {key!r}")
        axis_keys.add(key)

    if not isinstance(layers, list) or not layers:
        errors.append("layers must be a non-empty list")
        layers = []
    reserved_seen: list[tuple[str, str]] = []
    capability_closure: dict[str, set[str]] = {}
    layer_domains: dict[str, tuple[str, ...]] = {}
    refs_dir = Path(refs_dir)
    for index, layer in enumerate(layers):
        where = f"layers[{index}]"
        if not isinstance(layer, dict):
            errors.append(f"{where} must be an object")
            continue
        lid = str(layer.get("id") or "").strip()
        expected = str(index + 1)
        if lid != expected:
            errors.append(
                f"{where}.id must be {expected!r} (contiguous string ids in build order)"
            )
        layer_ids.append(lid)
        for field in ("title", "script", "charter"):
            if not str(layer.get(field) or "").strip():
                errors.append(f"{where}.{field} must be a non-empty string")
        judge = layer.get("judge")
        frames: set[int] = set()
        if not isinstance(judge, list) or not judge:
            errors.append(f"{where}.judge must be a non-empty list of {{frame, ref}}")
        else:
            for jindex, row in enumerate(judge):
                if not isinstance(row, dict) or not isinstance(row.get("frame"), int):
                    errors.append(f"{where}.judge[{jindex}].frame must be an integer")
                    continue
                frames.add(int(row["frame"]))
                ref = str(row.get("ref") or "")
                if not ref.startswith("refs/") or not (refs_dir / Path(ref).name).is_file():
                    errors.append(
                        f"{where}.judge[{jindex}].ref must name an existing refs/ file"
                    )
        primary = layer.get("primary_judge")
        if not isinstance(primary, int) or primary not in frames:
            errors.append(
                f"{where}.primary_judge must be one of the layer's judge frames "
                f"{sorted(frames)}"
            )
        owns = layer.get("owns")
        if not isinstance(owns, list) or not owns:
            errors.append(f"{where}.owns must be a non-empty list of axis keys")
        else:
            unknown = sorted(set(map(str, owns)) - axis_keys)
            if unknown:
                errors.append(
                    f"{where}.owns names axes missing from axes[]: {', '.join(unknown)}"
                )
        domains = layer.get("evidence_domains")
        if not isinstance(domains, list) or not domains or not set(map(str, domains)) <= EVIDENCE_DOMAINS:
            errors.append(
                f"{where}.evidence_domains must be a non-empty subset of "
                f"{sorted(EVIDENCE_DOMAINS)}"
            )
        else:
            try:
                layer_domains[lid] = parse_evidence_domains(
                    domains, f"{where}.evidence_domains"
                )
            except ValueError as exc:
                errors.append(str(exc))
        depends = layer.get("depends_on")
        if not isinstance(depends, list):
            errors.append(f"{where}.depends_on must be a list of earlier layer ids")
        else:
            for dep in map(str, depends):
                if dep not in layer_ids[:-1]:
                    errors.append(
                        f"{where}.depends_on names {dep!r}, which is not an earlier layer"
                    )
        roles = layer.get("reserved_roles")
        if not isinstance(roles, list) or not roles or any(not str(r).strip() for r in roles):
            errors.append(f"{where}.reserved_roles must be a non-empty list of namespaces")
        else:
            for role in map(str, roles):
                for prior_role, prior_layer in reserved_seen:
                    if prior_layer != lid and (
                        Path(role).match(prior_role) or Path(prior_role).match(role)
                    ):
                        errors.append(
                            f"{where}.reserved_roles {role!r} overlaps layer "
                            f"{prior_layer}'s {prior_role!r}"
                        )
                reserved_seen.append((role, lid))
        provides = layer.get("provides")
        if not isinstance(provides, dict):
            errors.append(
                f"{where}.provides must map capabilities to reserved role selectors "
                f"drawn from {sorted(GLOBAL_SCENE_CAPABILITIES)} (use {{}} when none)"
            )
            provided = set()
        else:
            provided = {str(item) for item in provides}
            unknown = sorted(provided - GLOBAL_SCENE_CAPABILITIES)
            if unknown:
                errors.append(
                    f"{where}.provides names unknown global scene capabilities: "
                    + ", ".join(unknown)
                )
            for capability, selectors in provides.items():
                if (
                    not isinstance(selectors, list)
                    or not selectors
                    or any(not str(selector).strip() for selector in selectors)
                ):
                    errors.append(
                        f"{where}.provides.{capability} must be a non-empty list of "
                        "reserved role selectors"
                    )
                    continue
                undeclared = sorted(
                    str(selector)
                    for selector in selectors
                    if str(selector) not in {str(role) for role in roles or []}
                )
                if undeclared:
                    errors.append(
                        f"{where}.provides.{capability} names roles not present verbatim "
                        "in reserved_roles: " + ", ".join(undeclared)
                    )
            extra_form = extra_reserved_roles_on_camera_layer(
                provided_capabilities=provided,
                camera_selectors=(
                    list(provides.get("camera") or [])
                    if isinstance(provides.get("camera"), list)
                    else []
                ),
                reserved_roles=roles or [],
            )
            if extra_form:
                errors.append(
                    f"{where}.reserved_roles names form selectors a camera-providing "
                    "layer cannot mutate as geometry: "
                    + ", ".join(extra_form)
                    + f" — {CAMERA_LAYER_DEFERS_SUBJECT_FORM_RULE} Split camera provide "
                    "onto its own layer; reserve those selectors on a later layer that "
                    "does not provide camera."
                )
        dependency_capabilities = {
            capability
            for dependency in (depends if isinstance(depends, list) else [])
            for capability in capability_closure.get(str(dependency), set())
        }
        capability_closure[lid] = provided | dependency_capabilities
        if "camera" not in capability_closure[lid]:
            errors.append(
                f"{where} is judged before a camera capability is available; declare "
                "provides: {\"camera\": [\"<reserved role>\"]} on this layer or "
                "depend on an earlier layer whose capability closure provides it"
            )

    resolutions = mapping.get("resolutions")
    if not isinstance(resolutions, dict):
        errors.append("resolutions must be an object mapping clause id -> resolution")
        resolutions = {}
    registry_ids = [row["id"] for row in registry]
    missing = sorted(set(registry_ids) - set(map(str, resolutions)))
    if missing:
        errors.append(
            "every brief clause must be resolved exactly once; missing: "
            + ", ".join(missing)
        )
    unknown = sorted(set(map(str, resolutions)) - set(registry_ids))
    if unknown:
        errors.append("resolutions name unknown clause ids: " + ", ".join(unknown))
    for rid in registry_ids:
        resolution = resolutions.get(rid)
        if resolution is None:
            continue
        where = f"resolutions[{rid}]"
        if not isinstance(resolution, dict):
            errors.append(f"{where} must be an object")
            continue
        kind = str(resolution.get("kind") or "")
        if kind == "decision":
            if not str(resolution.get("statement") or "").strip():
                errors.append(f"{where}.statement must be a non-empty string")
            if resolution.get("decision_strength") not in DECISION_STRENGTHS:
                errors.append(
                    f"{where}.decision_strength must be one of "
                    f"{sorted(DECISION_STRENGTHS)}"
                )
            if resolution.get("evidence_domains") is not None:
                errors.append(f"{where} decision must omit evidence_domains")
        elif kind == "deferred_owner":
            owner = str(resolution.get("owner_layer") or "")
            if owner not in layer_ids:
                errors.append(
                    f"{where}.owner_layer must name a declared layer "
                    f"({', '.join(layer_ids) or 'none declared'})"
                )
            try:
                declared = parse_evidence_domains(
                    resolution.get("evidence_domains"), f"{where}.evidence_domains"
                )
            except ValueError as exc:
                errors.append(str(exc))
                declared = ()
            owner_cov = layer_domains.get(owner, ())
            if declared and owner in layer_domains and uncovered_evidence_domains(
                declared, owner_cov
            ):
                covering = layers_covering_evidence_domains(declared, layer_domains)
                errors.append(
                    requirement_domain_coverage_what(
                        rid, declared, owner, owner_cov, covering
                    )
                    + f". {REQUIREMENT_DOMAIN_COVERAGE_FIX}"
                )
        else:
            errors.append(
                f"{where}.kind must be 'decision' or 'deferred_owner' — contracts and "
                "obligations are materialization-time authority"
            )

    blockers = mapping.get("blockers", [])
    if not isinstance(blockers, list) or any(not str(b).strip() for b in blockers):
        errors.append("blockers must be a list of non-empty strings")
    return errors


def derive_owned_requirements(mapping: dict[str, Any]) -> dict[str, list[str]]:
    """owned means owed: the inverse of the deferred_owner mapping, and nothing else."""
    owned: dict[str, list[str]] = {
        str(layer.get("id")): [] for layer in mapping.get("layers", [])
    }

    def _numeric(rid: str) -> tuple[int, str]:
        digits = "".join(ch for ch in rid if ch.isdigit())
        return (int(digits) if digits else 0, rid)

    for rid, resolution in sorted(
        mapping.get("resolutions", {}).items(), key=lambda kv: _numeric(kv[0])
    ):
        if isinstance(resolution, dict) and resolution.get("kind") == "deferred_owner":
            owner = str(resolution.get("owner_layer"))
            if owner in owned:
                owned[owner].append(str(rid))
    return owned


def expand_mapping(
    workspace: str | Path, mapping: dict[str, Any]
) -> dict[str, Path]:
    """Write the complete authority surface from the compact mapping. Deterministic:
    identical inputs produce identical bytes."""
    workspace = Path(workspace)
    registry = clause_registry(workspace / "brief.md")
    errors = validate_mapping(mapping, registry, workspace / "refs")
    if errors:
        raise ValueError("mapping is invalid:\n" + "\n".join(f"- {e}" for e in errors))

    written: dict[str, Path] = {}

    def _write(name: str, payload: Any) -> None:
        if name not in MAPPING_ARTIFACTS:
            raise ValueError(f"unregistered mapping artifact: {name}")
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        written[name] = path

    resolutions = mapping["resolutions"]
    requirement_rows = []
    for row in registry:
        resolution = resolutions[row["id"]]
        if resolution["kind"] == "decision":
            resolved = {
                "kind": "decision",
                "ids": [],
                "decision": str(resolution["statement"]).strip(),
                "decision_strength": str(resolution["decision_strength"]),
            }
        else:
            owner = str(resolution["owner_layer"])
            resolved = {
                "kind": "deferred_owner",
                "ids": [],
                "owner_layer": owner,
                "due": {"kind": "before_layer", "layer": owner},
                "evidence_domains": list(parse_evidence_domains(
                    resolution.get("evidence_domains"),
                    f"resolutions[{row['id']}].evidence_domains",
                )),
            }
        requirement_rows.append({**row, "resolution": resolved})
    _write("requirements.json", {
        "schema": "vfx-harness.requirements/v2",
        "requirements": requirement_rows,
        "judgment_debt_definitions": [],
        "judgment_debt_activations": [],
    })

    owned = derive_owned_requirements(mapping)
    layer_rows = []
    for layer in mapping["layers"]:
        lid = str(layer["id"])
        layer_rows.append({
            "id": lid,
            "script": str(layer["script"]),
            "title": str(layer["title"]),
            "primary_judge": int(layer["primary_judge"]),
            "judge": [
                {"frame": int(row["frame"]), "ref": str(row["ref"])}
                for row in layer["judge"]
            ],
            "owns": [str(key) for key in layer["owns"]],
            "evidence_domains": [str(d) for d in layer["evidence_domains"]],
            "reads": str(layer["charter"]),
            "execution": "jit_deferred",
            "stages": [],
            "jit": {
                "depends_on_layers": [str(d) for d in layer.get("depends_on", [])],
                "required_outcomes": [],
                "provides": {
                    str(capability): [str(role) for role in roles]
                    for capability, roles in layer.get("provides", {}).items()
                },
                "reserved_roles": [str(r) for r in layer["reserved_roles"]],
                "owned_requirements": owned[lid],
            },
        })
    _write("layers.json", {"schema": 5, "layers": layer_rows})

    _write("critic_axes.json", [
        {"key": str(axis["key"]), "desc": str(axis.get("desc") or axis["key"])}
        for axis in mapping["axes"]
    ])
    _write("acceptance.json", [])
    _write("checks.json", {"schema": 2, "checks": []})
    _write("scene_checks.json", {"schema": 2, "contracts": []})
    _write("obligations.json", {"schema": "vfx-harness.obligations/v1", "obligations": []})
    _write("assumptions.json", {"schema": "vfx-harness.assumptions/v1", "assumptions": []})
    _write("plans/global.md", _render_global(workspace, mapping, registry, owned))
    # Provenance: the compact source document rides into published bundles through the
    # supplemental plans/ mechanism without changing the bundle-completeness contract.
    _write("plans/ownership_mapping.json", mapping)
    return written


def _render_global(
    workspace: Path,
    mapping: dict[str, Any],
    registry: tuple[dict[str, Any], ...],
    owned: dict[str, list[str]],
) -> str:
    brief_hash = _sha256(workspace / "brief.md")
    decisions = [
        (rid, res)
        for rid, res in sorted(mapping["resolutions"].items())
        if res.get("kind") == "decision"
    ]
    deferred = len(registry) - len(decisions)
    lines = [
        "# Global plan (machine-rendered from the ownership mapping)",
        "",
        "Sparse global authority only: layer DAG, ownership register, durable",
        "decisions, and blockers. Every layer materializes just in time; this file is",
        "generated from the accepted machine records and is not hand-edited.",
        "",
        f"Brief: `brief.md`, sha256 `{brief_hash}`.",
        "",
        "## Layer DAG",
        "",
        "| Layer | Script | Charter | Depends on | Primary judge |",
        "|---|---|---|---|---|",
    ]
    for layer in mapping["layers"]:
        depends = ", ".join(map(str, layer.get("depends_on", []))) or "—"
        lines.append(
            f"| {layer['id']} | `{layer['script']}` | {layer['title']} | "
            f"{depends} | f{layer['primary_judge']} |"
        )
    lines += [
        "",
        "## Ownership",
        "",
        f"{len(registry)} brief clauses registered: {len(decisions)} durable decisions, "
        f"{deferred} deferred to exactly one owner layer.",
        "",
    ]
    for layer in mapping["layers"]:
        lid = str(layer["id"])
        lines.append(f"- Layer {lid} owes: {', '.join(owned[lid]) or '(nothing)'}")
    lines += ["", "## Durable decisions", ""]
    for rid, res in decisions:
        lines.append(
            f"- {rid} [{res['decision_strength']}]: {str(res['statement']).strip()}"
        )
    blockers = mapping.get("blockers", [])
    lines += ["", "## Blockers", ""]
    lines += [f"- {b}" for b in blockers] or ["- none"]
    lines += ["", "## Judge references", "", "| Frame | Ref | Layer |", "|---:|---|---|"]
    for layer in mapping["layers"]:
        for row in layer["judge"]:
            lines.append(f"| {row['frame']} | `{row['ref']}` | {layer['id']} |")
    return "\n".join(lines) + "\n"
