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
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.domain.work_units import read_document
from vfx_harness.observability import run_artifacts

from . import grounding as _grounding

# A path-looking token in the plan prose. Two shapes, because plans cite both ways and a
# checker blind to one of them is worse than no checker: it reports "citations resolve"
# having never looked at half of them.
#   with a separator   `logs/plan_lab/spike_01.out`, `../barrel_roll/refs/M4_end.jpg`
#   bare artifact      `spike_05.out` — resolved by searching the shot, since a bare name
#                      says nothing about where it lives
# The bare form is restricted to extensions a plan cites as EVIDENCE. Allowing bare `.py`
# or `.md` would make every `__init__.py` in ordinary prose a blocking finding, and a gate
# that cries wolf gets skimmed.
_CITE = re.compile(
    r"`([^`\s]*[/\\][^`\s]*\.(?:py|out|png|jpg|json|md|txt)|[\w.-]+\.(?:out|png|glb))`"
    r"(?:\s*line\s*(\d+))?"
)
# How a plan refers to a spike, in every notation it actually uses: `spike_04`,
# `spike_04.out`, and `spike #1` — the last being the form the spike TOOL prints in its own
# result ("spike #1 · exit 0 · kept: logs/plan_lab_draft/spike_01.py"), so a plan quoting
# its own evidence back is using the pipeline's own wording. Matching only `spike_NN` flagged
# a correctly-cited, genuinely-proven ticket as unevidenced, which is the expensive kind of
# false positive: the repair brief would have told a planner to re-run a spike that had
# already run and was already cited.
_SPIKE = re.compile(r"\bspike[ _#]{0,2}(\d+)\b", re.IGNORECASE)
# The number is written both padded and unpadded, so resolve both.
_SPIKE_EXT = ("out", "py", "png")


def _global_executable_checks_apply(layer: object) -> bool:
    """Whether global authority contains executable detail for this layer.

    This is the single phase boundary for gate sections that inspect units, claims,
    composition contexts, or concrete contracts. Structural checks still inspect every
    layer; executable checks inspect only ``ready`` rows and the later JIT gate validates
    the materialized replacement.
    """
    execution = (
        layer.get("execution", "ready")
        if isinstance(layer, dict)
        else getattr(layer, "execution", "ready")
    )
    return execution == "ready"


def _materialized_view(folder: Path) -> tuple[set[str], set[str]]:
    """(layer ids materialized by the verified JIT view, overlay files pinned to it).

    A hash-pinned materialization view is the DESIGNED post-materialization shape of a
    consumer view: its ready layers and their concrete contracts are materialized
    authority, not global-preproduction violations. Run 20260824T153427Z-91b7c1
    materialized layer 1 legitimately and the pre-materialization rules then blocked
    every unit plan the generation produced — the materialize→gate→publish path had
    never passed. Exemption is integrity-gated: a pointer that is absent, malformed,
    for another bundle, or whose pinned hash does not match the staged bytes grants
    NOTHING, so the strict pre-materialization reading always remains the fallback.
    """
    pointer_path = folder / "state" / "jit-layers" / "current.json"
    if not pointer_path.is_file():
        return set(), set()
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        if pointer.get("schema") != "vfx-harness.jit-layer-view/v1":
            return set(), set()
        marker_path = folder / ".plan-consumer-view.json"
        if marker_path.is_file():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if pointer.get("bundle_hash") != marker.get("content_hash"):
                return set(), set()
        pinned = set()
        for name, expected in (pointer.get("hashes") or {}).items():
            staged = folder / name
            if (
                isinstance(expected, str)
                and staged.is_file()
                and hashlib.sha256(staged.read_bytes()).hexdigest() == expected
            ):
                pinned.add(str(name))
        if "layers.json" not in pinned:
            return set(), set()
        return {str(layer_id) for layer_id in pointer.get("materialized_layers") or []}, pinned
    except (OSError, ValueError, json.JSONDecodeError):
        return set(), set()


# `[known ✓spiked — spike_04/spike_05, verified]` and friends.
_TICKET = re.compile(r"^\*\*(G\d+\S*·\S*|[A-Z]\d+\S*)\s*·\s*(.+?)\*\*\s*(\[[^\]]*\])?", re.M)
_URL = re.compile(r"https?://[^\s)`\]]+")
_RECIPE = re.compile(r"find_recipe\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _resolve(folder: Path, rel: str) -> Path | None:
    """A citation resolves if the file is where it says, or — for a partial path — anywhere
    under the shot. A path fragment carries no location, so treating one as dead because it
    is not at the working directory would be the checker's error, not the plan's:
    `isolated/view_0.png` is a real plate, written by the asset stage two directories down.
    """
    p = (folder / rel).resolve()
    if p.is_file():
        return p
    tail = rel.replace("\\", "/").lstrip("./")
    return next(iter(sorted(folder.rglob(tail))), None)


def _planned_outputs(folder: Path) -> set[str]:
    """Paths the plan DECLARES IT WILL CREATE, which are not citations and must never be
    reported as dead ones.

    A plan names `build/01_layout.py` because it is commissioning that script, not because
    it is offering it as evidence. On a freshly planned shot none of them exist yet — that
    is the normal, correct state — and the first version of this check flagged all seven
    and would have sent a repair round out to "fix" the plan's own deliverables. The repair
    brief says "re-point it at a live path or restate the measurement inline", so the
    likeliest outcome was a worse plan, paid for.

    Read from layers.json rather than pattern-matched on `build/`, so this stays a claim
    the plan itself made rather than a convention this file assumes.
    """
    try:
        layers = read_document(folder / "layers.json")
    except (OSError, ValueError):
        return set()
    out = set()
    for lay in layers:
        s = lay.get("script")
        if s:
            out.add(str(s).replace("\\", "/").lstrip("./"))
        # Work-unit plans are scheduled deliverables. They are deliberately absent until
        # their dependency frontier becomes ready, so citing the declared path as future
        # work is not a dead evidence citation.
        for unit in lay.get("stages") or []:
            if isinstance(unit, dict) and unit.get("plan"):
                out.add(str(unit["plan"]).replace("\\", "/").lstrip("./"))
    return out


@dataclass
class Finding:
    check: str
    blocking: bool
    where: str
    what: str
    fix: str = ""

    def __str__(self) -> str:
        head = "✗" if self.blocking else "·"
        line = f"{head} [{self.check}] {self.where}: {self.what}"
        return line + (f"\n        → {self.fix}" if self.fix else "")


@dataclass
class GateResult:
    shot: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def clean(self) -> bool:
        return not self.blocking

    @property
    def publishable_outcome(self) -> str:
        """Typed terminal state for a gate-clean candidate."""
        if not self.clean:
            return "dirty"
        if self.stats.get("open_assumptions", 0):
            return "clean_with_assumptions"
        if self.stats.get("open_obligations", 0):
            return "clean_with_deferred"
        return "clean"

    def signature(self) -> str:
        """Stable identity of WHAT is wrong, for detecting a loop that stopped converging.
        Two rounds with the same signature means the repair pass changed nothing that
        matters, and continuing just pays for the same answer again."""
        return "|".join(sorted(f"{f.check}:{f.where}:{f.what[:60]}" for f in self.findings))

    def to_dict(self, *, outcome: str | None = None) -> dict:
        """The reusable authority record; terminal readers need not rerun the gate."""
        return {
            "schema": "vfx-harness.plan-gate/v1",
            "policy": "structural-authority/runtime-falsification-v1",
            "validation_scope": "structural_authority",
            "runtime_contracts_confirmed": False,
            "shot": self.shot,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "clean": self.clean,
            "outcome": outcome or ("clean" if self.clean else "dirty"),
            "blocking_count": len(self.blocking),
            "warning_count": len(self.findings) - len(self.blocking),
            "stats": self.stats,
            "signature": self.signature(),
            "findings": [
                {
                    "check": finding.check,
                    "severity": "blocking" if finding.blocking else "warning",
                    "where": finding.where,
                    "what": finding.what,
                    **({"fix": finding.fix} if finding.fix else {}),
                }
                for finding in self.findings
            ],
        }


def write_report(folder: Path, result: GateResult, *, outcome: str) -> Path:
    """Persist final gate authority in the active structured run."""
    layout = run_artifacts.active(folder)
    if layout is None:
        raise RuntimeError("plan-gate report requires an active structured run")
    return layout.write_report("plan_gate", result.to_dict(outcome=outcome))


def _builder_render(folder: Path, c) -> Path | None:
    """The render a builder check was authored against: <layer>_best.png, else any judged
    frame for that layer."""
    rd = run_artifacts.readable_renders_dir(folder)
    if not rd.is_dir():
        return None
    # FRAME FIRST. `<layer>_best.png` is the layer's best render of its PRIMARY judge frame;
    # measuring an f440 check against it reported three sound builder checks as "does not
    # hold" (0.354 authored, 1.532 measured) purely because the gate looked at the wrong
    # picture. Same class as the earlier proof/spec mismatch: right number, wrong artifact.
    if c.frame is not None:
        # EXCLUDE motion strips. `*_motion.png` is a horizontal montage of several frames,
        # so measuring a region against one reads the seams: three sound checks came back
        # at ~1.0 on every ratio because the glob matched the strip, not the frame.
        # The trailing underscore is load-bearing: `f1*` also matches `f100` and `f191`,
        # and alphabetical order puts f100 first — so a frame-1 check was measured against
        # the f100 render and reported 2.947 against a target of <= 0.42.
        hits = [q for q in sorted(rd.glob(f"{c.layer}@f{c.frame}_*.png")) if "_motion" not in q.name]
        if hits:
            return hits[0]
    best = rd / f"{c.layer}_best.png"
    return best if best.is_file() else None


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
    from vfx_harness.evidence.checks import load, verify

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
        from vfx_harness.domain.contracts import load_document

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
        from vfx_harness.domain.contracts import load_document

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


def _check_contracts(folder: Path, *, require_scene_checks: bool = False) -> tuple[list[Finding], dict]:
    from vfx_harness.evidence.scene_checks import validate_row as validate_scene_check

    out = []
    missing = [n for n in ("layers.json", "critic_axes.json", "acceptance.json") if not (folder / n).is_file()]
    if missing:
        legacy = (
            " (this shot has gates.json — it predates the layer contract)" if (folder / "gates.json").is_file() else ""
        )
        return [
            Finding(
                "contracts",
                True,
                ", ".join(missing),
                f"the plan produced no {' / '.join(missing)}{legacy}",
                "the build stage reads these, so nothing can be built from this plan as it "
                "stands — re-plan, or port the legacy artifact forward",
            )
        ], {}
    try:
        layers_document = json.loads((folder / "layers.json").read_text())
        layers = read_document(folder / "layers.json")
        axes = json.loads((folder / "critic_axes.json").read_text())
        accept = json.loads((folder / "acceptance.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return [Finding("contracts", True, "plan artifacts", f"unreadable: {e}")], {}

    unit_first = isinstance(layers_document, dict) and layers_document.get("schema") == 5
    materialized_ids, pinned_overlays = _materialized_view(folder) if unit_first else (set(), set())
    if unit_first:
        ready = [
            str(layer.get("id") or "?")
            for layer in layers
            if layer.get("execution") != "jit_deferred"
            and str(layer.get("id") or "?") not in materialized_ids
        ]
        if ready:
            out.append(Finding(
                "global-preproduction",
                True,
                "layers.json",
                "schema-5 global authority contains ready layers: " + ", ".join(ready),
                "publish every layer as ownership-only jit_deferred authority; materialize "
                "the dependency-ready root after publication",
            ))
        if accept:
            out.append(Finding(
                "global-preproduction",
                True,
                "acceptance.json",
                "schema-5 global authority contains acceptance fingerprints",
                "leave global acceptance empty and measure a reference only when its owning "
                "layer materializes",
            ))

    axis_keys = {a["key"] for a in axes}
    layer_ids = {str(lay.get("id")) for lay in layers}
    layer_axes = {str(lay.get("id")): set(lay.get("owns") or []) for lay in layers}
    layer_frames = {
        str(lay.get("id")): {
            int(item.get("frame"))
            for item in ([lay.get("judge")] if isinstance(lay.get("judge"), dict) else lay.get("judge") or [])
            if item.get("frame") is not None
        }
        for lay in layers
    }
    published_frames = {
        frame
        for lay in layers
        if _global_executable_checks_apply(lay)
        for frame in layer_frames.get(str(lay.get("id")), set())
    }
    owned: set[str] = set()
    for lay in layers:
        lid = lay.get("id", "?")
        if not lay.get("owns"):
            out.append(
                Finding(
                    "contracts",
                    True,
                    f"layer {lid}",
                    "owns no critic axis; legacy unscoped judging is not supported",
                    "assign at least one visible, independently answerable axis",
                )
            )
        for ax in lay.get("owns", []):
            owned.add(ax)
            if ax not in axis_keys:
                out.append(
                    Finding(
                        "contracts",
                        True,
                        f"layer {lid}",
                        f"owns axis '{ax}', which critic_axes.json does not define",
                        "the critic can never score this axis, so the layer cannot be judged "
                        "on the thing it is responsible for — add the axis or fix the name",
                    )
                )
        judges = lay.get("judge")
        for j in [judges] if isinstance(judges, dict) else judges or []:
            ref = j.get("ref")
            if ref and not (folder / ref).is_file():
                out.append(
                    Finding(
                        "contracts",
                        True,
                        f"layer {lid} judge f{j.get('frame')}",
                        f"reference '{ref}' does not exist",
                        "the layer would be judged against a missing plate",
                    )
                )
    for ax in sorted(axis_keys - owned):
        out.append(
            Finding(
                "contracts",
                False,
                "critic_axes.json",
                f"axis '{ax}' is defined but no layer owns it",
                "either a layer should own it or it is dead weight in every rubric it "
                "appears in — an unowned axis has nobody to route a failure to",
            )
        )
    for m in accept:
        if m.get("frame") not in published_frames:
            out.append(Finding(
                "deferred-overplanning",
                True,
                f"acceptance {m.get('id')}",
                f"fingerprints frame {m.get('frame')} before its producing layer materializes",
                "global acceptance.json may contain only judge frames of ready published units; "
                "add later moments through the materialized acceptance overlay",
            ))
        ref = m.get("ref")
        if ref and not (folder / ref).is_file():
            out.append(
                Finding(
                    "contracts",
                    True,
                    f"acceptance {m.get('id')}",
                    f"reference '{ref}' does not exist",
                    "this moment cannot be judged at all",
                )
            )

    scene_path = folder / "scene_checks.json"
    scene_rows = []
    if not scene_path.is_file():
        out.append(
            Finding(
                "contracts",
                require_scene_checks,
                "scene_checks.json",
                "no live-scene contracts supplied",
                "exact dimensions, placements, counts and mesh-state facts will be left to a "
                "vision judge; add scene_checks.json for numeric scene clauses",
            )
        )
    else:
        try:
            from vfx_harness.domain.contracts import load_document

            scene_rows = load_document(scene_path, "contracts")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            out.append(Finding("contracts", True, "scene_checks.json", f"unreadable: {exc}"))
            scene_rows = []
        from vfx_harness.evidence.scene_checks import validate_row_set

        # Advisory here, refused at authoring: guardrails and validate_materialization
        # raise on this shape for NEW row sets, but a published view may carry a
        # grandfathered instance that a permissive node class (e.g. Math, whose inputs
        # are literally named 'Value') satisfies honestly — blocking retroactively
        # would poison sealed authority that measurably works.
        for cross_row_finding in validate_row_set([r for r in scene_rows if isinstance(r, dict)]):
            out.append(Finding(
                "contracts",
                False,
                "scene_checks.json",
                cross_row_finding,
                "declare the response row's socket at this layer's next materialization",
            ))
        # Advisory mirror of validate_materialization's hard rule (new materializations
        # cannot publish without it): a judge frame with no occlusion-true visibility
        # row judges subjects nobody proved are on screen. Run 20260825: layer 2's
        # every judged surface sat behind a solid proxy disc at both judge frames,
        # invisible to projection-only bbox rows, and no rule fired because
        # composition-coverage is scoped to camera-owning layers.
        for lid in sorted(materialized_ids):
            for frame in sorted(layer_frames.get(lid, set())):
                if not any(
                    r.get("kind") == "visible_fraction" and r.get("frame") == frame
                    for r in scene_rows
                    if isinstance(r, dict)
                ):
                    out.append(Finding(
                        "composition-coverage",
                        False,
                        f"layer {lid} judge f{frame}",
                        "no occlusion-true visibility contract at this judge frame",
                        "add a visible_fraction row for the judged roles at this "
                        "layer's next materialization",
                    ))
        seen: set[str] = set()
        for row in scene_rows:
            rid = str(row.get("id") or "<missing>") if isinstance(row, dict) else "<invalid>"
            if not isinstance(row, dict):
                out.append(Finding("contracts", True, "scene_checks.json", "every record must be an object"))
                continue
            error = validate_scene_check(row)
            if error:
                out.append(Finding("contracts", True, rid, error, "fix the scene contract schema before building"))
            if rid in seen:
                out.append(Finding("contracts", True, rid, "duplicate scene-check id"))
            seen.add(rid)
            lid = str(row.get("fault_owner", ""))
            owner = str(row.get("owner_layer", ""))
            active = str(row.get("activates_at", ""))
            axis = str(row.get("axis", ""))
            if owner not in layer_ids:
                out.append(Finding("contracts", True, rid, f"owner_layer names nonexistent layer {owner!r}"))
            if active not in layer_ids:
                out.append(Finding("contracts", True, rid, f"activates_at names nonexistent layer {active!r}"))
            if lid not in layer_ids:
                out.append(Finding("contracts", True, rid, f"fault_owner names nonexistent layer {lid!r}"))
            elif axis not in layer_axes.get(lid, set()):
                out.append(
                    Finding(
                        "contracts",
                        True,
                        rid,
                        f"axis {axis!r} is not owned by layer {lid}",
                        "route the contract to the layer answerable for that property",
                    )
                )
            if row.get("frame") is not None and active in layer_frames:
                try:
                    frame = int(row["frame"])
                except (TypeError, ValueError):
                    out.append(Finding("contracts", True, rid, "frame is not an integer"))
                else:
                    if frame not in layer_frames[active]:
                        out.append(
                            Finding(
                                "contracts",
                                True,
                                rid,
                                f"frame {frame} is not judged by activation layer {active}",
                                "add the frame to the layer judge list or move the contract",
                            )
                        )
            for temporal_frame in row.get("frames") or []:
                if active in layer_frames and temporal_frame not in layer_frames[active]:
                    out.append(
                        Finding(
                            "contracts",
                            True,
                            rid,
                            f"temporal frame {temporal_frame} is not judged by activation layer {active}",
                            "every temporal endpoint is a real judge frame; add it to the layer and unit judge lists",
                        )
                    )
    if unit_first and scene_rows:
        # contracts owned by a verifiably materialized layer ARE the layer boundary
        # materialization the remedy asks for; only the rest violate preproduction
        unmaterialized_contracts = [
            row
            for row in scene_rows
            if not (
                "scene_checks.json" in pinned_overlays
                and str(row.get("owner_layer") or "") in materialized_ids
            )
        ]
        if unmaterialized_contracts:
            out.append(Finding(
                "global-preproduction",
                True,
                "scene_checks.json",
                "schema-5 global authority contains concrete scene contracts",
                "materialize scene contracts at the owning layer boundary",
            ))

    if unit_first:
        try:
            image_rows = read_document(folder / "checks.json")
        except (OSError, json.JSONDecodeError, ValueError):
            image_rows = []
        if image_rows:
            out.append(Finding(
                "global-preproduction",
                True,
                "checks.json",
                "schema-5 global authority contains candidate-sensitive image checks",
                "propose image checks only after a producing unit has created a real candidate",
            ))
    closure_claims = 0
    dependency_findings = _check_unit_dependencies(folder)
    if dependency_findings:
        # Hierarchy already reports every invalid edge with a targeted repair. Claim
        # closure cannot be evaluated until those edges are fixed, and repeating the
        # typed loader's first exception here only adds a duplicate partial finding.
        return out, {
            "layers": len(layers),
            "axes": len(axis_keys),
            "moments": len(accept),
            "scene_checks": len(scene_rows),
            "claims": 0,
        }
    try:
        from vfx_harness.evidence.claim_evidence import validate_claim_closure
        from vfx_harness.orchestration.ledger import load_layers

        parsed_layers = tuple(load_layers(type("ShotRoot", (), {"folder": folder})()).values())
        closure_claims = sum(
            len(unit.evaluation.claims)
            for layer in parsed_layers
            for unit in layer.stages
        )
        closure = validate_claim_closure(folder, parsed_layers)
        for finding in closure.findings:
            out.append(
                Finding(
                    "claim-closure",
                    True,
                    finding.where,
                    finding.what,
                    "bind the atomic claim to exact typed evidence, or declare qualified "
                    "qualitative/human authority; aggregate 'all checks pass' claims are invalid",
                )
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        out.append(Finding("claim-closure", True, "layers.json", f"unreadable claim graph: {exc}"))
    return out, {
        "layers": len(layers),
        "axes": len(axis_keys),
        "moments": len(accept),
        "scene_checks": len(scene_rows),
        "claims": closure_claims,
    }


def _check_unit_dependencies(folder: Path) -> list[Finding]:
    """Report every layer-local dependency that names no unit in its own layer.

    This intentionally runs before the typed loader. It does not replace schema or cycle
    validation; it makes one common structural failure exhaustive instead of allowing the
    loader's first exception to hide identical failures in later layers.
    """
    try:
        layers = read_document(folder / "layers.json")
    except (OSError, ValueError):
        return []

    findings: list[Finding] = []
    for layer_index, layer in enumerate(layers):
        stages = layer.get("stages")
        if not isinstance(stages, list):
            continue
        known = {
            str(stage.get("id"))
            for stage in stages
            if isinstance(stage, dict) and str(stage.get("id") or "").strip()
        }
        for stage_index, stage in enumerate(stages):
            if not isinstance(stage, dict) or not isinstance(stage.get("depends_on", []), list):
                continue
            unit_id = str(stage.get("id") or f"stages[{stage_index}]")
            missing = sorted(
                {
                    str(dep)
                    for dep in stage.get("depends_on", [])
                    if isinstance(dep, str) and dep not in known
                }
            )
            if not missing:
                continue
            findings.append(
                Finding(
                    "hierarchy",
                    True,
                    f"layers.json.layers[{layer_index}].stages.{unit_id}",
                    f"depends on unknown units: {', '.join(missing)}",
                    "depends_on is layer-local; remove cross-layer ids because accepted "
                    "layer order and protected interfaces already carry that dependency",
                )
            )
    return findings


def _check_evidence_coherence(folder: Path) -> tuple[list[Finding], dict]:
    """Check temporal, composition, and mutation ownership coverage across contracts."""
    from vfx_harness.domain.contracts import load_document
    from vfx_harness.evidence.scene_checks import TEMPORAL_KINDS

    try:
        layers = read_document(folder / "layers.json")
    except (OSError, ValueError):
        return [], {}
    try:
        scene_rows = load_document(folder / "scene_checks.json", "contracts")
    except (OSError, ValueError, json.JSONDecodeError):
        scene_rows = []
    try:
        image_rows = load_document(folder / "checks.json", "checks")
    except (OSError, ValueError, json.JSONDecodeError):
        image_rows = []

    out: list[Finding] = []
    temporal_ids = {
        str(row.get("id"))
        for row in scene_rows
        if isinstance(row, dict) and row.get("kind") in TEMPORAL_KINDS | {"frame_delta"}
    }
    bbox_kinds = {
        "bbox_width",
        "bbox_height",
        "bbox_center_x",
        "bbox_center_y",
        "bbox_top_y",
        "bbox_bottom_y",
    }
    composition_tokens = ("camera", "composition", "framing", "staging")
    scene_by_id = {
        str(row.get("id")): row for row in scene_rows if isinstance(row, dict) and row.get("id")
    }
    image_by_id = {
        str(row.get("id")): row for row in image_rows if isinstance(row, dict) and row.get("id")
    }
    # every role namespace the PLAN declares anywhere: unit mutation authority plus
    # deferred reservations — the universe a two-sided contract's measurement side may
    # observe (its own repair authority still closes on the primary selectors)
    plan_declared_roles: set[str] = set()
    for layer_row in layers:
        if not isinstance(layer_row, dict):
            continue
        for pattern in (layer_row.get("jit") or {}).get("reserved_roles") or []:
            plan_declared_roles.add(str(pattern))
        for stage_row in layer_row.get("stages") or []:
            if isinstance(stage_row, dict):
                for pattern in (stage_row.get("mutates") or {}).get("roles") or []:
                    plan_declared_roles.add(str(pattern))

    def _selector_declared(selector: str, declarations: set[str]) -> bool:
        return any(
            selector == declared
            or selector.startswith(f"{declared}.")
            or fnmatch.fnmatchcase(selector, declared)
            or fnmatch.fnmatchcase(declared, selector)
            for declared in declarations
        )

    motion_units = 0
    earlier_camera_available = False
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        lid = str(layer.get("id") or "")
        owns = [str(axis).lower() for axis in layer.get("owns") or []]
        judges = [
            int(row["frame"])
            for row in layer.get("judge") or []
            if isinstance(row, dict) and isinstance(row.get("frame"), int)
        ]
        domains = layer.get("evidence_domains")
        if not _global_executable_checks_apply(layer):
            # Deferred layers reserve authority but deliberately have no executable units,
            # claims, composition contexts, or controls until their JIT materialization gate.
            # Their structural shape is validated by the typed layer loader and meta gate.
            continue
        owns_composition = (
            "projected_composition" in domains
            if isinstance(domains, list)
            else any(token in axis for axis in owns for token in composition_tokens)
        )
        stages = {
            str(unit.get("id")): unit
            for unit in layer.get("stages") or []
            if isinstance(unit, dict) and unit.get("id")
        }
        # Typed authority first: a unit DECLARES what it provides to dependents. The
        # substring scan below is the legacy path for units that declare nothing, and it
        # is why `cam_rig` — the harness's own default camera-rig role, and the role the
        # approved camera decision keys against — was invisible to this rule while the
        # unit owning it had already passed.
        declared = {
            uid
            for uid, unit in stages.items()
            if "camera" in (unit.get("provides") or [])
        }
        camera_units = declared or {
            uid
            for uid, unit in stages.items()
            if any("camera" in str(role).lower() for role in (unit.get("mutates") or {}).get("roles") or [])
        }

        def _camera_available_to(
            uid: str,
            earlier: bool = earlier_camera_available,
            available: frozenset[str] = frozenset(camera_units),
            layer_stages: dict[str, dict] = stages,
        ) -> bool:
            if earlier:
                return True
            seen: set[str] = set()
            frontier = [uid]
            while frontier:
                current = frontier.pop()
                if current in seen:
                    continue
                seen.add(current)
                if current in available:
                    return True
                frontier.extend(
                    str(dep) for dep in layer_stages.get(current, {}).get("depends_on") or []
                )
            return False

        if owns_composition:
            for frame in judges:
                covered = False
                for unit in stages.values():
                    evaluation = unit.get("evaluation") or {}
                    # A required claim bound straight to a bbox contract at this judge
                    # frame IS executable projected context — the remediation text has
                    # always said a direct binding is valid, so honor it rather than
                    # demanding the composition_context restatement of the same fact.
                    if any(
                        isinstance(claim, dict)
                        and claim.get("required")
                        and frame in (claim.get("moments") or [])
                        and any(
                            isinstance(binding, dict)
                            and binding.get("kind") == "scene_contract"
                            and scene_by_id.get(str(binding.get("id")), {}).get("kind") in bbox_kinds
                            and scene_by_id.get(str(binding.get("id")), {}).get("frame") == frame
                            for binding in claim.get("evidence") or []
                        )
                        for claim in evaluation.get("claims") or []
                    ):
                        covered = True
                        break
                    context = evaluation.get("composition_context") or {}
                    if frame not in (context.get("frames") or []):
                        continue
                    contract_ids = {str(value) for value in context.get("contract_ids") or []}
                    if (
                        contract_ids
                        and all(
                            cid in scene_by_id and scene_by_id[cid].get("kind") in bbox_kinds
                            for cid in contract_ids
                        )
                        and any(scene_by_id[cid].get("frame") == frame for cid in contract_ids)
                    ):
                        covered = True
                        break
                    source_id = str(context.get("source_unit") or "")
                    source = stages.get(source_id)
                    if source and source_id in {str(value) for value in unit.get("depends_on") or []}:
                        source_contracts = {
                            str(binding.get("id"))
                            for claim in (source.get("evaluation") or {}).get("claims") or []
                            if isinstance(claim, dict)
                            for binding in claim.get("evidence") or []
                            if isinstance(binding, dict) and binding.get("kind") == "scene_contract"
                        }
                        source_frames = {
                            row.get("frame")
                            for row in (source.get("evaluation") or {}).get("judge") or []
                            if isinstance(row, dict)
                        }
                        if frame in source_frames and any(
                            str(row.get("id")) in source_contracts
                            and
                            row.get("kind") in bbox_kinds
                            and row.get("frame") == frame
                            and str(row.get("activates_at") or "") == lid
                            for row in scene_rows
                        ):
                            covered = True
                            break
                if not covered:
                    out.append(
                        Finding(
                            "composition-coverage",
                            True,
                            f"layer {lid} judge f{frame}",
                            "camera/composition owner has no executable projected context",
                            "bind a bbox contract directly or depend on a blockout unit that "
                            "is bbox-checked at this judge frame before sealing the camera",
                        )
                    )
        for uid, unit in stages.items():
            evaluation = unit.get("evaluation") or {}
            bound_ids = {
                str(binding.get("id"))
                for claim in evaluation.get("claims") or []
                if isinstance(claim, dict)
                for binding in claim.get("evidence") or []
                if isinstance(binding, dict) and binding.get("kind") == "scene_contract"
            }
            context = evaluation.get("composition_context") or {}
            bound_ids.update(str(value) for value in context.get("contract_ids") or [])
            bbox_ids = sorted(
                cid
                for cid in bound_ids
                if cid in scene_by_id and scene_by_id[cid].get("kind") in bbox_kinds
            )
            if bbox_ids and not _camera_available_to(uid):
                out.append(
                    Finding(
                        "composition-bootstrap",
                        True,
                        f"layer {lid} unit {uid}",
                        "bbox evidence is due before any declared camera is available",
                        "make the first camera and its measurable blockout one atomic scoped unit, "
                        "or depend on a unit that declares `provides: [\"camera\"]`; a blockout "
                        "cannot be projected through a camera owned only by its dependent",
                    )
                )
        for unit in layer.get("stages") or []:
            if not isinstance(unit, dict):
                continue
            uid = str(unit.get("id") or "<missing>")
            evaluation = unit.get("evaluation") or {}
            mutates = unit.get("mutates") or {}
            # dressed selectors carry appearance authority (ADR-0007), so contracts and
            # claims about the dressed surfaces close through them like mutation roles
            mutable_roles = {str(value) for value in mutates.get("roles") or []} | {
                str(value) for value in mutates.get("dresses") or []
            }
            mutable_controls = {str(value) for value in mutates.get("controls") or []}
            declared_dressable_elsewhere = {
                str(selector)
                for other in layers
                if isinstance(other, dict) and str(other.get("id")) != lid
                for selector in other.get("dressable") or []
            }
            undeclared_dresses = sorted(
                str(value)
                for value in mutates.get("dresses") or []
                if str(value) not in declared_dressable_elsewhere
            )
            if undeclared_dresses:
                out.append(
                    Finding(
                        "dressing-closure",
                        True,
                        f"layer {lid} unit {uid}",
                        "dresses selectors no other layer declares dressable: "
                        + ", ".join(undeclared_dresses),
                        "the owning layer's row must list these under `dressable`; "
                        "dressing is granted by the owner, never taken",
                    )
                )
            for claim in evaluation.get("claims") or []:
                if not isinstance(claim, dict) or not claim.get("required"):
                    continue
                for binding in claim.get("evidence") or []:
                    if not isinstance(binding, dict) or not binding.get("id"):
                        continue
                    evidence_id = str(binding["id"])
                    if binding.get("kind") == "image_contract":
                        check = image_by_id.get(evidence_id)
                        if check and check.get("stage") == "post_grade" and lid != str(layers[-1].get("id")):
                            out.append(
                                Finding(
                                    "unit-evidence-due",
                                    True,
                                    f"layer {lid} unit {uid} claim {claim.get('id', '?')}",
                                    f"required image contract {evidence_id} is post_grade and "
                                    "cannot execute at this unit boundary",
                                    "either author and prove an any/pre_grade contract for this "
                                    "unit, or make the final-plate requirement a typed obligation "
                                    "due after the grade-owning dependency; required unit evidence "
                                    "may not be deferred implicitly",
                                )
                            )
                    if binding.get("kind") != "scene_contract":
                        continue
                    contract = scene_by_id.get(evidence_id) or {}
                    # Two-sided measurement kinds observe the OTHER side of a relation:
                    # clearance obstacles and parallax far-groups are inherently other
                    # layers' roles (a persistent clearance contract exists precisely to
                    # measure against geometry its owner will never mutate). The primary
                    # selectors remain strictly inside mutation authority — repair
                    # authority closes there; the compare side is a measurement subject
                    # that must still name a role namespace the PLAN declares somewhere,
                    # so a control id smuggled into compare_roles stays a violation.
                    two_sided = str(contract.get("kind") or "") in {
                        "path_clearance_min",
                        "parallax_displacement_profile",
                        "onset_order",
                    }
                    # visible_fraction is observation-only: it mutates nothing, and it
                    # exists precisely so the CAMERA answers for geometry it will never
                    # own — run 20260825's spine escaped because visibility routed
                    # through the geometry unit, which satisfied its own bbox rows by
                    # convenient placement. Its roles are measurement subjects held to
                    # plan-declared namespaces (control smuggling stays a violation).
                    observation_only = str(contract.get("kind") or "") == "visible_fraction"
                    if observation_only:
                        primary_keys: tuple[str, ...] = ()
                    elif two_sided:
                        primary_keys = ("roles",)
                    else:
                        primary_keys = ("roles", "compare_roles")
                    selected_roles = {
                        str(value)
                        for key in primary_keys
                        for value in contract.get(key) or []
                    }
                    undeclared_roles = sorted(
                        selector
                        for selector in selected_roles
                        if not _selector_declared(selector, mutable_roles)
                    )
                    if two_sided or observation_only:
                        measurement_key = "roles" if observation_only else "compare_roles"
                        undeclared_roles.extend(sorted(
                            selector
                            for selector in {
                                str(value) for value in contract.get(measurement_key) or []
                            }
                            if not _selector_declared(selector, plan_declared_roles)
                        ))
                    if undeclared_roles:
                        out.append(
                            Finding(
                                "role-selector-closure",
                                True,
                                f"layer {lid} unit {uid} contract {evidence_id}",
                                "selects roles outside mutation authority: "
                                + ", ".join(undeclared_roles),
                                "use role selectors inside mutates.roles, or use typed "
                                "control_roles/compare_control_roles for bvfx_control ids; "
                                "required evidence and repair authority must close together",
                            )
                        )
                    selected_controls = {
                        str(value)
                        for key in ("control_roles", "compare_control_roles")
                        for value in contract.get(key) or []
                    }
                    undeclared = sorted(
                        selector
                        for selector in selected_controls
                        if not _selector_declared(selector, mutable_controls)
                    )
                    if undeclared:
                        out.append(
                            Finding(
                                "control-selector-closure",
                                True,
                                f"layer {lid} unit {uid} contract {evidence_id}",
                                "selects undeclared semantic controls: " + ", ".join(undeclared),
                                "declare each selected control in mutates.controls and map it "
                                "through control_roles; contract selectors and mutation authority "
                                "must share the same typed ids",
                            )
                        )
            if evaluation.get("temporal_evidence") == "motion":
                motion_units += 1
                bound = {
                    str(binding.get("id"))
                    for claim in evaluation.get("claims") or []
                    if isinstance(claim, dict)
                    for binding in claim.get("evidence") or []
                    if isinstance(binding, dict) and binding.get("kind") == "scene_contract"
                }
                if not bound & temporal_ids:
                    out.append(
                        Finding(
                            "temporal-coverage",
                            True,
                            f"layer {lid} unit {uid}",
                            "declares temporal_evidence='motion' but binds no temporal executable contract",
                            "bind onset_order, radial_distance_trend, transform_return_delta, or frame_delta evidence",
                        )
                    )
            controls = {str(value) for value in mutates.get("controls") or []}
            mapping = mutates.get("control_roles")
            if controls and not mapping:
                out.append(
                    Finding(
                        "ownership",
                        False,
                        f"layer {lid} unit {uid}",
                        f"declares {len(controls)} mutable control(s) without control_roles mapping",
                        "map each control to the semantic roles it governs so scope coherence is checkable",
                    )
                )
        earlier_camera_available = earlier_camera_available or bool(camera_units)

    fault_owners = [
        str(row.get("fault_owner") or "") for row in image_rows if isinstance(row, dict)
    ]
    owner_layers = {
        str(row.get("owner_layer") or "") for row in image_rows if isinstance(row, dict)
    }
    if len(fault_owners) >= 4:
        counts = {owner: fault_owners.count(owner) for owner in set(fault_owners)}
        concentrated, count = max(counts.items(), key=lambda item: item[1])
        final_layer = str(layers[-1].get("id") or "") if layers and isinstance(layers[-1], dict) else ""
        if (
            concentrated
            and count / len(fault_owners) >= 0.9
            and (len(owner_layers) >= 2 or concentrated == final_layer)
        ):
            out.append(
                Finding(
                    "ownership",
                    False,
                    "checks.json fault_owner distribution",
                    f"{count}/{len(fault_owners)} image checks route failures to layer {concentrated}",
                    "separate activation from fault ownership and route each failure to the earliest answerable layer",
                )
            )
    return out, {"motion_units": motion_units, "temporal_contracts": len(temporal_ids)}


def _check_meta_records(folder: Path) -> tuple[list[Finding], dict]:
    """Close every registered brief requirement through typed authority."""
    from vfx_harness.domain.contracts import load_document
    from vfx_harness.domain.plan_records import (
        brief_clause_spans,
        load_assumptions,
        load_obligations,
        load_requirements,
    )
    from vfx_harness.evidence.scene_checks import TEMPORAL_KINDS

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
    # pointer to an unavailable prior plan is not an adoption mechanism.
    decision_path = folder / "state" / "plan-resolutions.jsonl"
    structured_decisions: dict[str, tuple[int, dict]] = {}
    if decision_path.is_file():
        from vfx_harness.domain.plan_records import resolution_decision_strength

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
            if row.get("status") != "satisfied" or not isinstance(row.get("values"), dict):
                continue
            decision_id = str(row.get("id") or "").strip()
            contract = row["values"].get("contract")
            if not decision_id or not isinstance(contract, dict) or not contract:
                findings.append(Finding(
                    "decision-adoption", True, f"state/plan-resolutions.jsonl:{line_no}",
                    "structured resolution must provide values.contract",
                    "embed the complete executable contract fields in the approved resolution",
                ))
                continue
            structured_decisions[decision_id] = (line_no, contract)

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
        from vfx_harness.domain.brief import load_shot

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
    allowed_domains = {"scene", "image", "temporal", "projected_composition", "human"}
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        domains = layer.get("evidence_domains")
        if not isinstance(domains, list) or not domains:
            findings.append(Finding(
                "requirement-closure", True, f"layer {layer.get('id', '?')}",
                "evidence_domains is missing",
                "declare the typed evidence domains this layer requires; composition "
                "coverage must not be inferred from axis-name keywords",
            ))
        elif unknown_domains := sorted(set(map(str, domains)) - allowed_domains):
            findings.append(Finding(
                "requirement-closure", True, f"layer {layer.get('id', '?')}",
                "unknown evidence domains: " + ", ".join(unknown_domains),
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


def _check_hierarchical_plans(folder: Path) -> tuple[list[Finding], dict]:
    """Require plans for dependency-ready units and validate the feedback ledger."""
    from vfx_harness.domain.brief import load_shot
    from vfx_harness.domain.work_units import ready_units
    from vfx_harness.orchestration.layer_plans import (
        load_amendments,
        validate_work_unit_plan_authority,
        work_unit_plan_path,
    )
    from vfx_harness.orchestration.ledger import load_layers
    from vfx_harness.orchestration.unit_state import load as load_unit_state
    from vfx_harness.orchestration.unit_state import validate_current

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
                )
            )
            return out, {"unit_plans_required": 0, "layers_passed": len(passed)}
        unit_passed = {
            uid for uid, row in (state.get("units") or {}).items() if row.get("status") == "passed"
        }
        ready = ready_units(next_layer.stages, unit_passed)
        required = len(ready)
        if not ready:
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    f"layer {next_layer.id} work-unit DAG",
                    "no work unit is ready although the layer has not passed",
                    "resolve a blocked dependency or apply a transactional replan",
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
