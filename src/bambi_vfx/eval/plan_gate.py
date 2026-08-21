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
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..work_units import read_document
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

    def signature(self) -> str:
        """Stable identity of WHAT is wrong, for detecting a loop that stopped converging.
        Two rounds with the same signature means the repair pass changed nothing that
        matters, and continuing just pays for the same answer again."""
        return "|".join(sorted(f"{f.check}:{f.where}:{f.what[:60]}" for f in self.findings))


def _builder_render(folder: Path, c) -> Path | None:
    """The render a builder check was authored against: <layer>_best.png, else any judged
    frame for that layer."""
    rd = folder / "renders"
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
    from ..checks import load, verify

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
        from ..contracts import load_document

        try:
            raw_checks = load_document(spec, "checks")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return [Finding("done-checks", True, "checks.json", f"unreadable: {str(exc)[:120]}")], {}
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
    """Every layer needs at least one check it can RUN AT ITS OWN STAGE.

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
        from ..contracts import load_document

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
    out = []
    for lay in layers:
        lid = str(lay.get("id"))
        if runnable.get(lid, 0) == 0:
            n = sum(1 for c in specs if str(c.get("activates_at") or "") == lid)
            out.append(
                Finding(
                    "coverage",
                    True,
                    f"layer {lid}",
                    f"has {n} check(s) and NONE runnable at its own stage",
                    "a layer whose only checks need the final grade is judged on prose until "
                    "layer 7 exists. State what changes WHEN THIS LAYER RUNS: a pre_grade "
                    "target, or a check against its own render and the state before it "
                    "(propose_checks does this at build time)",
                )
            )
    return out, {"layers_uncovered": len(out)}


def _check_grounded(folder: Path) -> tuple[list[Finding], dict]:
    rec = _grounding.audit(folder)
    if "error" in rec:
        return [Finding("grounded", True, "acceptance.json", rec["error"])], {}
    out = []
    for mo in rec["moments"]:
        if "error" in mo:
            out.append(Finding("grounded", True, f"{mo['id']}", mo["error"]))
            continue
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
    return out, {"claims": rec["n_claims"], "grounded": rec["n_ok"]}


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
    from ..scene_checks import validate_row as validate_scene_check

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
        layers = read_document(folder / "layers.json")
        axes = json.loads((folder / "critic_axes.json").read_text())
        accept = json.loads((folder / "acceptance.json").read_text())
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return [Finding("contracts", True, "plan artifacts", f"unreadable: {e}")], {}

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
            from ..contracts import load_document

            scene_rows = load_document(scene_path, "contracts")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            out.append(Finding("contracts", True, "scene_checks.json", f"unreadable: {exc}"))
            scene_rows = []
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
    closure_claims = 0
    try:
        from ..claim_evidence import validate_claim_closure
        from ..ledger import load_layers

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


def _check_hierarchical_plans(folder: Path) -> tuple[list[Finding], dict]:
    """Require plans for dependency-ready units and validate the feedback ledger."""
    from ..brief import load_shot
    from ..layer_plans import load_amendments, work_unit_plan_path
    from ..ledger import load_layers
    from ..unit_state import load as load_unit_state
    from ..unit_state import validate_current
    from ..work_units import ready_units

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
                "plan.md is not supported; run `bambi plan <shot>` to migrate",
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
                if isinstance(row, dict) and row.get("status") == "passed"
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
    if next_layer is not None:
        try:
            state = load_unit_state(folder, str(next_layer.id))
            validate_current(state, str(next_layer.id), next_layer.stages)
        except ValueError as exc:
            out.append(
                Finding(
                    "hierarchy",
                    True,
                    f"logs/work_units/layer_{next_layer.id}.json",
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
                out.append(
                    Finding(
                        "hierarchy",
                        True,
                        str(path.relative_to(folder)),
                        f"ready unit {next_layer.id}.{unit.id} has no just-in-time plan",
                        f"run `bambi plan {folder} --layer {next_layer.id} --unit {unit.id}`",
                    )
                )
            else:
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
            f"   {s.get('grounded', 0)}/{s.get('claims', 0)} fingerprint claims reproduce · "
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
