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

import json
from pathlib import Path

from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.work_units import ready_units
from vfx_harness.evaluation.plan_gate.types import (
    Finding,
    _global_executable_checks_apply,
)
from vfx_harness.evaluation.plan_gate.unit_deps import _check_unit_dependencies
from vfx_harness.orchestration.layer_plans import (
    load_amendments,
    validate_work_unit_plan_authority,
    work_unit_plan_path,
)
from vfx_harness.orchestration.ledger import load_layers
from vfx_harness.orchestration.unit_state import digest_matched_passed, validate_current
from vfx_harness.orchestration.unit_state import load as load_unit_state


def _check_hierarchical_plans(folder: Path) -> tuple[list[Finding], dict]:
    """Require plans for dependency-ready units and validate the feedback ledger."""

    out: list[Finding] = []
    legacy = folder / "plan.md"
    if legacy.is_file():
        out.append(
            Finding(
                "hierarchy",
                True,
                "plan.md",
                "legacy monolithic plan coexists with strict work-unit plans and can poison builder retrieval",
                "archive it outside the shot folder; strict planning has no compatibility "
                "fallback and builders consume only schema-declared unit plans",
            )
        )
    global_path = folder / "plans" / "global.md"
    if not global_path.is_file():
        return [
            *out,
            Finding(
                "hierarchy",
                True,
                "plans/global.md",
                "strict global plan is missing",
                "plan.md is not supported; run `vfx plan <shot>` to migrate",
            ),
        ], {}
    try:
        load_amendments(folder)
    except (OSError, ValueError) as exc:
        out.append(
            Finding(
                "hierarchy",
                True,
                "plan_amendments.jsonl",
                str(exc),
                "repair the JSONL record; invalid feedback cannot be ignored",
            )
        )
    dependency_findings = _check_unit_dependencies(folder)
    if dependency_findings:
        # ``load_layers`` raises on the first invalid layer. Returning its exception
        # here would force one paid repair round per layer for the same repeated defect.
        # The raw dependency pass is exhaustive, so one repair brief can fix the whole
        # document before typed loading and claim closure resume.
        return [*out, *dependency_findings], {"unit_plans_required": 0, "layers_passed": 0}
    try:
        layers = load_layers(load_shot(folder))
    except Exception as exc:
        return [*out, Finding("hierarchy", True, "layers.json", str(exc))], {}

    passed: set[str] = set()
    outcomes = folder / "plans" / "outcomes"
    for path in sorted(outcomes.glob("*.json")) if outcomes.is_dir() else []:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("status") == "passed":
                passed.add(str(row.get("layer")))
        except (OSError, json.JSONDecodeError) as exc:
            out.append(Finding("hierarchy", True, str(path.relative_to(folder)), f"unreadable sealed outcome: {exc}"))
    ledger_passed: set[str] = set()
    ledger_path = folder / "shot.json"
    if ledger_path.is_file():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger_passed = {
                str(lid)
                for lid, row in (ledger.get("milestones") or {}).items()
                if (
                    isinstance(row, dict)
                    and row.get("status") == "passed"
                    and str(lid) in layers
                )
            }
        except (OSError, json.JSONDecodeError) as exc:
            out.append(Finding("hierarchy", True, "shot.json", f"unreadable: {exc}"))
    for lid in sorted(ledger_passed - passed):
        out.append(
            Finding(
                "hierarchy",
                True,
                f"plans/outcomes/{int(lid):02d}.json",
                f"ledger marks layer {lid} passed but its sealed planning outcome is missing",
                "revalidate that layer and publish its authoritative outcome before planning downstream work",
            )
        )
    next_layer = next((layer for lid, layer in layers.items() if lid not in passed), None)
    required = 0
    if next_layer is not None and not _global_executable_checks_apply(next_layer):
        # Deferred authority has no durable unit state until a pinned JIT overlay
        # materializes its full unit DAG. Requiring state here would invent a fake unit.
        return out, {"unit_plans_required": 0, "layers_passed": len(passed)}
    if next_layer is not None:
        try:
            state = load_unit_state(folder, str(next_layer.id))
            validate_current(state, str(next_layer.id), next_layer.stages)
        except ValueError as exc:
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    f"state/work-units/layer_{next_layer.id}.json",
                    str(exc),
                    "apply a transactional replan; stale unit state cannot authorize execution",
                    layer=str(next_layer.id),
                )
            )
            return out, {"unit_plans_required": 0, "layers_passed": len(passed)}
        unit_passed = {
            uid for uid, row in (state.get("units") or {}).items() if row.get("status") == "passed"
        }

        ready = ready_units(
            next_layer.stages,
            unit_passed,
            sealed_producers=digest_matched_passed(state, next_layer.stages),
        )
        required = len(ready)
        if not ready:
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    f"layer {next_layer.id} work-unit DAG",
                    "no work unit is ready although the layer has not passed",
                    "resolve a blocked dependency or apply a transactional replan",
                    layer=str(next_layer.id),
                )
            )
        for unit in ready:
            path = work_unit_plan_path(folder, unit)
            if not path.is_file():
                # HIR-0016 made the unit plan a BUILD-time artifact: generated, stamped,
                # gated, and attested inside the build flow, with consumers refusing
                # unattested authority. Before that flow runs, absence is the DESIGNED
                # state — blocking here deadlocked the first unit plan of any fresh-id
                # layer on its sibling's equally-designed absence (run 0b6849), while
                # stale unattested files from a superseded generation satisfied the old
                # existence check. Only state/artifact drift blocks: a unit whose state
                # claims progress must have its plan on disk.
                status = str(
                    ((state.get("units") or {}).get(str(unit.id)) or {}).get("status")
                    or "pending"
                )
                out.append(
                    Finding(
                        "hierarchy",
                        status != "pending",
                        str(path.relative_to(folder)),
                        f"ready unit {next_layer.id}.{unit.id} has no just-in-time plan"
                        + ("" if status == "pending" else f" although its state is {status!r}"),
                        "the build flow generates and gate-attests it at kickoff"
                        if status == "pending"
                        else f"run `vfx plan {folder} --layer {next_layer.id} --unit {unit.id}`",
                    )
                )
            else:
                try:
                    # integrity only: the gate is the authority that PRODUCES the gate
                    # attestation, so it cannot require one to exist yet
                    validate_work_unit_plan_authority(folder, path, require_gate=False)
                except ValueError as exc:
                    out.append(Finding(
                        "hierarchy", True, str(path.relative_to(folder)), str(exc),
                        "regenerate the JIT unit plan from the selected global bundle",
                    ))
                    continue
                plan_text = path.read_text(encoding="utf-8", errors="replace").strip()
                if len(plan_text) < 200:
                    out.append(
                        Finding(
                            "hierarchy", True, str(path.relative_to(folder)), "unit plan is too small to be executable"
                        )
                    )
                elif plan_text.count("\n") + 1 > 160:
                    out.append(
                        Finding(
                            "hierarchy",
                            True,
                            str(path.relative_to(folder)),
                            "unit plan exceeds the strict 160-line execution-index limit",
                            "move evidence/history into machine contracts and sealed outcomes; "
                            "keep only scope, controls, tickets, contract ids, and the stop rule",
                        )
                    )
    return out, {"unit_plans_required": required, "layers_passed": len(passed)}
