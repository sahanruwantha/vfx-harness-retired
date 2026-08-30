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

from pathlib import Path

from vfx_harness.evaluation.plan_gate.contracts import _check_contracts
from vfx_harness.evaluation.plan_gate.coverage import (
    _check_citations,
    _check_coverage,
    _check_done,
    _check_evidence,
    _check_grounded,
)
from vfx_harness.evaluation.plan_gate.evidence_coherence import _check_evidence_coherence
from vfx_harness.evaluation.plan_gate.hierarchical import _check_hierarchical_plans
from vfx_harness.evaluation.plan_gate.meta import _check_meta_records
from vfx_harness.evaluation.plan_gate.types import (
    Finding,
    GateResult,
)


def run(folder: Path, plan_name: str = "plans/global.md", *, require_scene_checks: bool = False) -> GateResult:
    folder = Path(folder)
    res = GateResult(shot=folder.name)
    plan_path = folder / plan_name
    if not plan_path.is_file():
        res.findings.append(
            Finding(
                "contracts",
                True,
                plan_name,
                "does not exist",
                "legacy plan.md is not supported; generate plans/global.md",
            )
        )
        return res
    plan = plan_path.read_text(encoding="utf-8", errors="replace")

    for finds, stats in (
        _check_hierarchical_plans(folder),
        _check_done(folder),
        _check_coverage(folder),
        _check_grounded(folder),
        _check_citations(folder, plan),
        _check_evidence(folder, plan),
        _check_contracts(folder, require_scene_checks=require_scene_checks),
        _check_evidence_coherence(folder),
        _check_meta_records(folder),
    ):
        res.findings += finds
        res.stats.update(stats)
    return res


def report(res: GateResult) -> str:
    n_b = len(res.blocking)
    head = f"── plan gate · {res.shot} · " + ("CLEAN" if res.clean else f"{n_b} blocking") + " ──"
    lines = [head]
    s = res.stats
    if s:
        lines.append(
            f"   {s.get('grounded', 0)}/{s.get('fingerprint_claims', 0)} fingerprint claims reproduce · "
            f"{s.get('citations_live', 0)}/{s.get('citations', 0)} citations resolve · "
            + (
                f"{s['checks']} executable checks ({s.get('checks_unadversaried', 0)} without a named adversary) · "
                if "checks" in s
                else f"{s.get('done_bound', 0)} done-checks bound (prose fallback) · "
            )
            + f"{s.get('tickets', 0)} tickets ({s.get('spiked', 0)} spiked, "
            f"{s.get('researched', 0)} researched) · "
            f"{s.get('layers', 0)} layers / {s.get('axes', 0)} axes / "
            f"{s.get('moments', 0)} moments / "
            f"{s.get('scene_checks', 0)} scene contracts"
        )
    if not res.findings:
        lines.append("   nothing to fix")
        return "\n".join(lines)
    lines.append("")
    for f in sorted(res.findings, key=lambda f: (not f.blocking, f.check)):
        lines.append("   " + str(f))
    if not n_b:
        lines.append("\n   no BLOCKING findings — a build on this plan aims at real targets")
    return "\n".join(lines)


def feedback(res: GateResult) -> str:
    """The findings as a repair brief for a planning pass. Blocking only: a warn-level
    finding is not worth another verify pass on its own."""
    b = res.blocking
    if not b:
        return ""
    lines = [
        f"A deterministic gate ran against your plan and found {len(b)} blocking "
        f"defect(s). These are MEASUREMENTS against the artifacts on disk, not "
        f"opinions, and each one means a layer would aim at something that is not "
        f"there. Fix every one, and change nothing else.",
        "",
    ]
    for i, f in enumerate(b, 1):
        lines.append(f"{i}. [{f.check}] {f.where} — {f.what}")
        if f.fix:
            lines.append(f"   {f.fix}")
    lines += [
        "",
        "Do not remove a target merely to silence the gate unless the finding "
        "explicitly says the target is unreachable. Re-measure and restate.",
    ]
    return "\n".join(lines)
