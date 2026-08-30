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

import hashlib
import json
from pathlib import Path

from vfx_harness.domain.contracts import load_document
from vfx_harness.domain.work_units import read_document
from vfx_harness.evaluation import grounding as _grounding
from vfx_harness.evaluation.plan_gate.types import (
    _CITE,
    _RECIPE,
    _SPIKE,
    _SPIKE_EXT,
    _TICKET,
    _URL,
    Finding,
    _global_executable_checks_apply,
    _planned_outputs,
    _resolve,
)
from vfx_harness.evidence.checks import load, verify


def _check_done(folder: Path) -> tuple[list[Finding], dict]:
    """Do the plan's own done-checks work? Every check in checks.json is RE-RUN here.

    Grounding asks whether the plan's NUMBERS are real; this asks whether its CHECKS can
    fail anything. Both are needed: the current plan scored 95/95 on grounding and still
    carried a check its own reference plate cannot pass.
    """
    # A plan that ships checks.json is verified against the CONTRACT — every check re-run,
    # its proof recomputed, the three rules applied. The prose parser below is the fallback
    # for plans written before the contract existed, and is strictly weaker: it binds two
    # predicates where the contract binds all of them.

    spec = folder / "checks.json"
    if not spec.is_file():
        # A contract nothing enforces is a suggestion. Without this the planner can satisfy
        # every other gate and simply not emit the file, which is how `[unknown]` reached
        # zero across four documents and `ask_supervisor` never fired: both were asked for
        # and neither was required.
        return [
            Finding(
                "done-checks",
                True,
                "checks.json",
                "the plan states numeric done-checks and emits no checks.json",
                "every numeric done-check must be a record that was RUN — author each with "
                "`measure_check` and write the fourth companion. A check nobody executed is "
                "a sentence, and three of those shipped in this plan",
            )
        ], {"checks": 0}
    if spec.is_file():
        corpus = sorted(
            p
            for sib in folder.parent.glob("*/renders")
            if sib.parent.name != folder.name and not sib.parent.name.startswith("_")
            for p in sib.glob("*_best.png")
        )
        out, n_weak = [], 0

        try:
            raw_checks = load_document(spec, "checks")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return [Finding("done-checks", True, "checks.json", f"unreadable: {str(exc)[:120]}")], {}
        for row in raw_checks:
            if not isinstance(row, dict):
                continue
            for reject in row.get("rejects") or []:
                candidate = Path(str(reject))
                if candidate.is_absolute():
                    out.append(Finding(
                        "done-checks",
                        True,
                        str(row.get("id") or "?"),
                        f"names absolute adversary path {reject!r}",
                        "use a candidate-relative authored ref or a persisted plans/evidence "
                        "artifact; process-local /tmp evidence cannot survive publication",
                    ))
                    continue
                resolved = (folder / candidate).resolve()
                try:
                    resolved.relative_to(folder.resolve())
                except ValueError:
                    out.append(Finding(
                        "done-checks",
                        True,
                        str(row.get("id") or "?"),
                        f"adversary path escapes candidate authority: {reject!r}",
                        "persist the adversary inside authored refs or plans/evidence",
                    ))
                    continue
                if not resolved.is_file():
                    out.append(Finding(
                        "done-checks",
                        True,
                        str(row.get("id") or "?"),
                        f"adversary is not a persisted candidate artifact: {reject!r}",
                        "persist the exact measured adversary before binding it to a check",
                    ))
        mixed = [str(row.get("id", "?")) for row in raw_checks if (row.get("origin") or "planner") != "planner"]
        if mixed:
            return [
                Finding(
                    "done-checks",
                    True,
                    "checks.json",
                    f"contains non-planner rows: {', '.join(mixed[:8])}",
                    "planner contracts are immutable; move builder-authored evidence to runtime_checks.json",
                )
            ], {"checks": len(raw_checks)}
        try:
            checks = load(spec)
        except Exception as e:
            return [Finding("done-checks", True, "checks.json", f"unreadable: {str(e)[:80]}")], {}
        for c in checks:
            ref = folder / c.ref
            if not ref.is_file():
                out.append(Finding("done-checks", True, c.id, f"names reference '{c.ref}', which does not exist"))
                continue
            v = verify(c, ref, corpus, root=Path.cwd())
            if not c.rejects:
                n_weak += 1
            if not v.ok:
                out.append(
                    Finding(
                        "done-checks",
                        True,
                        f"{c.id} ({c.metric})",
                        v.reasons[0] if v.reasons else "failed verification",
                        "; ".join(v.reasons[1:]),
                    )
                )
        return out, {"checks": len(checks), "checks_unadversaried": n_weak}

    # The prose fallback that lived here is gone: a real plan now ships checks.json (52
    # records on barrel_roll_v2, 51 of which survive independent re-verification), so
    # parsing done-conditions out of English is dead weight. It was scaffolding that proved
    # the defects were real and machine-detectable; checks.py is the mechanism.
    return [], {}


def _check_coverage(folder: Path) -> tuple[list[Finding], dict]:
    """Every layer needs at least one executable contract at its own stage.

    Three times on this plan, a layer's success criteria described the FINISHED FRAME
    rather than that layer's output:

      L1f-1            tagged post_grade on a pre-grade layer — unrunnable until layer 7
      camera_framing   "sign 0.25-0.30W at f001" — unmeasurable on a layout render whose
                       brightest pixel is 46/255
      motion_smear     the critic refused it outright: "smear is unverifiable from the
                       supplied strip: frames 1 and 20 are both in the slow pre-roll"

    The cause is that the only ground truth is the final plate, so every criterion gets
    written against it — hence 42 of 52 checks tagged post_grade and layout shipping with a
    single check that could not fire. A layer with nothing runnable is judged by a VLM
    reading prose, which is how layer 1 took three builder attempts to pass.

    This rule was deliberately NOT added earlier. With only image-vs-plate metrics available
    it would have forced the planner to manufacture a check where no ground truth existed —
    a coverage requirement without the evidence to satisfy it honestly, which is Goodhart.
    It is safe now because `propose_checks` + `verify_necessity` give a layer a way to be
    checked against its OWN render and the state before it ran.
    """
    try:
        layers = read_document(folder / "layers.json")

        specs = load_document(folder / "checks.json", "checks")
    except (OSError, ValueError):
        return [], {}
    if not layers:
        return [], {}
    last = str(layers[-1].get("id"))  # post_grade is only runnable once graded
    runnable: dict[str, int] = {}
    for c in specs:
        lid = str(c.get("activates_at") or "")
        ok = c.get("stage") != "post_grade" or lid == last
        runnable[lid] = runnable.get(lid, 0) + (1 if ok else 0)
    # Schema-4 claims may bind exact layout/material/animation facts to live-scene
    # contracts instead of inventing a pixel metric against a finished plate. Those
    # contracts are executable as soon as their activation layer exists and satisfy the
    # same coverage requirement. Ignoring them forced early layers to manufacture
    # decorative image thresholds despite already having stronger typed evidence.
    try:
        scene_specs = load_document(folder / "scene_checks.json", "contracts")
    except (OSError, ValueError):
        scene_specs = []
    scene_runnable: dict[str, int] = {}
    for c in scene_specs:
        if not isinstance(c, dict):
            continue
        lid = str(c.get("activates_at") or "")
        scene_runnable[lid] = scene_runnable.get(lid, 0) + 1
    out = []
    for lay in layers:
        lid = str(lay.get("id"))
        if not _global_executable_checks_apply(lay):
            # A deferred layer deliberately has no executable checks yet; its contracts
            # arrive through the JIT materialization gate, which requires every one of
            # them to carry a producing claim. Demanding coverage here re-creates the
            # pre-build overplanning this rule exists to prevent.
            continue
        if runnable.get(lid, 0) == 0 and scene_runnable.get(lid, 0) == 0:
            n = sum(1 for c in specs if str(c.get("activates_at") or "") == lid)
            out.append(
                Finding(
                    "coverage",
                    True,
                    f"layer {lid}",
                    f"has {n} image check(s), 0 scene contract(s), and NONE runnable at its own stage",
                    "a layer whose only checks need the final grade is judged on prose until "
                    "layer 7 exists. State what changes WHEN THIS LAYER RUNS: a pre_grade "
                    "target, a typed live-scene contract, or a check against its own render "
                    "and the state before it (propose_checks does this at build time)",
                )
            )
    return out, {"layers_uncovered": len(out)}


def _check_grounded(folder: Path) -> tuple[list[Finding], dict]:
    try:
        acceptance = json.loads((folder / "acceptance.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        acceptance = None
    if acceptance == []:
        return [], {"fingerprint_claims": 0, "grounded": 0}
    rec = _grounding.audit(folder)
    if "error" in rec:
        return [Finding("grounded", True, "acceptance.json", rec["error"])], {}
    out = []
    for mo in rec["moments"]:
        if "error" in mo:
            out.append(Finding("grounded", True, f"{mo['id']}", mo["error"]))
            continue
        for error in mo.get("schema_errors", []):
            out.append(
                Finding(
                    "grounded",
                    True,
                    f"{mo['id']} fingerprint",
                    error,
                    "copy the canonical metric_set and metric ids returned by measure_ref",
                )
            )
        for r in mo["claims"]:
            if r["verdict"] == "MISMATCH":
                out.append(
                    Finding(
                        "grounded",
                        True,
                        f"{mo['id']} {r['key']}",
                        f"plan claims {r['claimed']:g}, the plate reads {r['actual']:g} ({r['rel'] * 100:.0f}% off)",
                        "re-measure the plate and restate the target, or point the moment at "
                        "the plate the number actually came from",
                    )
                )
            elif r["verdict"] == "UNMEASURABLE":
                out.append(
                    Finding(
                        "grounded",
                        True,
                        f"{mo['id']} {r['key']}",
                        f"plan claims {r['claimed']:g} but {r['why']}",
                        "drop this target — it cannot be hit or missed, so a layer aiming at "
                        "it converges on nothing and spends its whole budget doing so",
                    )
                )
    return out, {"fingerprint_claims": rec["n_claims"], "grounded": rec["n_ok"]}


def _check_citations(folder: Path, plan: str) -> tuple[list[Finding], dict]:
    out, n_ok, seen, dead = [], 0, set(), set()
    planned = _planned_outputs(folder)
    n_fwd = 0
    for m in _CITE.finditer(plan):
        rel, lineno = m.group(1), m.group(2)
        if rel.replace("\\", "/").lstrip("./") in planned:
            n_fwd += 1  # a deliverable, not a citation — see _planned_outputs
            continue
        # A glob is a SEARCH INSTRUCTION, not a citation. The planner prompt itself tells
        # the verifier to look at `../*/plan.md` and `../*/build/*.py`, and quoting that
        # back is not a claim about a file. Reporting them as rot would train the reader
        # to skim this check, which is how a check stops being read at all.
        if any(ch in rel for ch in "*?["):
            continue
        if (rel, lineno) in seen:
            continue
        seen.add((rel, lineno))
        target = _resolve(folder, rel)
        if target is None:
            # One finding per dead path, however many times it is cited — the repair is
            # per path, and five copies of it would crowd out the other checks.
            if rel in dead:
                continue
            dead.add(rel)
            out.append(
                Finding(
                    "citations",
                    True,
                    rel,
                    "cited as evidence and does not resolve",
                    "the claim may still be true — this is usually link rot from an archived "
                    "or reset shot. Re-point it at a live path or restate the measurement "
                    "inline, because a build agent reading this today gets nothing",
                )
            )
            continue
        if lineno:
            try:
                n = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                n = 0
            if int(lineno) > n:
                out.append(
                    Finding(
                        "citations",
                        True,
                        f"{rel} line {lineno}",
                        f"cited line {lineno} but the file has {n} lines",
                        "the file changed after the citation was written — re-read it and "
                        "re-cite, or the number no longer points at its evidence",
                    )
                )
                continue
        n_ok += 1
    return out, {"citations": len(seen), "citations_live": n_ok, "forward_refs": n_fwd}


def _check_evidence(folder: Path, plan: str) -> tuple[list[Finding], dict]:
    """A confidence tag is a self-assessment. It is worth the artifact under it."""
    out = []
    tickets = _TICKET.findall(plan)
    tagged = [(tid, title, tag or "") for tid, title, tag in tickets]
    # Split the plan into per-ticket bodies so a claim is checked against ITS OWN section
    # rather than against the document, which would let one URL anywhere vouch for
    # everything.
    bounds = [m.start() for m in _TICKET.finditer(plan)] + [len(plan)]
    bodies = [plan[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]

    try:
        scene_doc = json.loads((folder / "scene_checks.json").read_text(encoding="utf-8"))
        scene_by_id = {
            str(row.get("id")): row
            for row in scene_doc.get("contracts", [])
            if isinstance(row, dict) and row.get("id")
        }
    except (OSError, ValueError, TypeError):
        scene_by_id = {}
    def read_contract_spike(rel: str, subject: str) -> dict | None:
        if not rel.endswith(".json"):
            return None
        target = _resolve(folder, rel)
        if target is None:
            return None
        try:
            relative = target.resolve().relative_to(folder.resolve()).as_posix()
        except ValueError:
            relative = ""
        if not relative.startswith("plans/evidence/spikes/"):
            out.append(Finding(
                "evidence",
                True,
                subject,
                f"contract-bound spike record is outside plans/evidence/spikes ({rel})",
                "publish spike authority inside the immutable candidate bundle",
            ))
            return None
        try:
            record = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            out.append(Finding(
                "evidence", True, subject, f"spike record is unreadable ({rel}: {exc})",
                "re-run the spike and cite its generated typed JSON record",
            ))
            return None
        if record.get("schema") != "vfx-harness.plan-spike/v1":
            out.append(Finding(
                "evidence", True, subject, f"spike record has an unsupported schema ({rel})",
                "re-run it with the contract-bound spike tool",
            ))
            return None
        for key in ("script", "output"):
            member = record.get(key)
            member_path = member.get("path") if isinstance(member, dict) else None
            member_hash = member.get("sha256") if isinstance(member, dict) else None
            evidence_member = target.parent / str(member_path or "")
            if (
                not member_path
                or Path(str(member_path)).name != str(member_path)
                or not evidence_member.is_file()
                or hashlib.sha256(evidence_member.read_bytes()).hexdigest() != member_hash
            ):
                out.append(Finding(
                    "evidence", True, subject,
                    f"spike record has missing or stale {key} bytes ({rel})",
                    "re-run the spike so its exact script and full output are frozen with the record",
                ))
                return None
        blender = record.get("blender")
        if not isinstance(blender, dict) or not all(
            isinstance(blender.get(key), str) and blender.get(key).strip()
            for key in ("executable", "version")
        ) or str(blender.get("version")).strip().lower() == "unknown":
            out.append(Finding(
                "evidence", True, subject,
                f"spike record omits Blender executable/version identity ({rel})",
                "re-run the spike in the supported runtime; mechanism evidence is version-bound",
            ))
            return None
        rows = record.get("contracts") or []
        results = {
            str(item.get("id")): item
            for item in record.get("results") or []
            if isinstance(item, dict)
        }
        stale = []
        failed = []
        covered = {}
        for row in rows:
            if not isinstance(row, dict):
                stale.append("<invalid-row>")
                continue
            contract_id = str(row.get("id") or "<missing>")
            if scene_by_id.get(contract_id) != row:
                stale.append(contract_id)
                continue
            result = results.get(contract_id, {})
            if result.get("pass") is not True:
                failed.append(contract_id)
                continue
            covered[contract_id] = row
        if stale:
            out.append(Finding(
                "evidence", True, subject,
                f"spike record is stale or narrower than current scene contracts ({', '.join(stale)})",
                "re-run the exact current contract rows; a prior threshold, frame, selector, or "
                "decision state cannot prove the published contract",
            ))
        if failed or record.get("passed") is not True:
            out.append(Finding(
                "evidence", True, subject,
                f"spike record contains failing or missing contract results ({', '.join(failed) or rel})",
                "fix the mechanism or decision state and re-run the contract-bound spike; "
                "do not relax the target to preserve a confidence tag",
            ))
        return {"record": record, "covered": covered, "path": relative}

    n_spiked = n_research = 0
    for (tid, _title, tag), body in zip(tagged, bodies, strict=False):
        low = tag.lower()
        if "spiked" in low:
            n_spiked += 1
            # The word "spike_04" is not evidence; the file it names is. Checking the
            # substring would have passed every barrel_roll ticket, whose lab was archived
            # and whose spike references resolve to nothing.
            cited = [m.group(1) for m in _CITE.finditer(body)]
            spikes = [
                f"spike_{n:0{pad}d}.{ext}"
                for raw in _SPIKE.findall(body)
                for n in [int(raw)]
                for pad in (2, 1)
                for ext in _SPIKE_EXT
            ]
            live = [c for c in cited + spikes if _resolve(folder, c)]
            if not live:
                named = ", ".join(dict.fromkeys(cited)) or "nothing"
                out.append(
                    Finding(
                        "evidence",
                        True,
                        tid,
                        f"claims ✓spiked but no cited artifact resolves (cites: {named})",
                        "a spike whose artifact cannot be re-read is a claim, not evidence — "
                        "re-point it at the logs/plan_lab*/spike_NN.out that proves it, or "
                        "re-run the spike",
                    )
                )
            typed = [
                record
                for rel in cited
                if (record := read_contract_spike(rel, tid)) is not None
            ]
            if not typed:
                out.append(Finding(
                    "evidence",
                    True,
                    tid,
                    "claims ✓spiked but cites no contract-bound spike JSON record",
                    "call spike with the exact scene-contract rows and cite the generated "
                    "plans/evidence/spikes/*.json record; a script, render, or stdout file "
                    "does not prove it ran the published frame/selectors/thresholds",
                ))
        if "unknown" in low or "researched" in low:
            n_research += 1
            if not _URL.search(body) and not _RECIPE.search(body):
                out.append(
                    Finding(
                        "evidence",
                        False,
                        tid,
                        "tagged as researched but carries no source link or recipe citation",
                        "step 6 of the planner contract requires source links in the ticket — "
                        "without one the version-rot check (Blender 5.x vs 2.8-4.x content) "
                        "cannot be repeated by anyone",
                    )
                )

    return out, {"tickets": len(tickets), "spiked": n_spiked, "researched": n_research}
