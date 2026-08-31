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
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.domain.work_units import read_document
from vfx_harness.observability import run_artifacts
from vfx_harness.orchestration.jit_materialization.view_pointer import (
    JitViewPointerError,
    canonical_view_hash,
    parse_jit_view_pointer,
    require_materialized_layers_match,
)
from vfx_harness.orchestration.plan_authority import CONSUMER_VIEW_SCHEMA, resolve_current

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
        selected = parse_jit_view_pointer(
            json.loads(pointer_path.read_text(encoding="utf-8"))
        )
        marker_path = folder / ".plan-consumer-view.json"
        if marker_path.is_file():
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if selected.bundle_hash != marker.get("content_hash"):
                return set(), set()
        documents: dict[str, object] = {}
        for name, expected in selected.hashes.items():
            staged = folder / name
            if not staged.is_file() or hashlib.sha256(staged.read_bytes()).hexdigest() != expected:
                return set(), set()
            documents[name] = json.loads(staged.read_text(encoding="utf-8"))
        require_materialized_layers_match(selected, documents["layers.json"])
        if canonical_view_hash(documents) != selected.view_hash:
            return set(), set()
        return set(selected.materialized_layers), set(selected.hashes)
    except (
        JitViewPointerError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return set(), set()


def _global_authority_layers(folder: Path, consumer_layers: list[dict]) -> list[dict]:
    """Return the immutable sparse layer rows behind a run-scoped consumer view.

    Materialization deliberately replaces a deferred row with a ready execution row,
    and ready rows cannot carry ``jit``. Global capability ownership, dependency
    closure, and reserved namespaces nevertheless remain authority in the selected
    bundle. Reading those predicates from the overlaid execution row forgets exactly
    the interfaces that made the materialization legal.

    The marker is not trusted by path alone: resolve the shot's selected bundle and
    require the marker's root and digest to match it. Any malformed or stale marker
    falls back to the consumer rows, which preserves the existing fail-closed result.
    """
    marker_path = folder / ".plan-consumer-view.json"
    if not marker_path.is_file():
        return consumer_layers
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))

        if marker.get("schema") != CONSUMER_VIEW_SCHEMA:
            return consumer_layers
        bundle = resolve_current(Path(str(marker["shot"])).resolve())
        if (
            bundle.root != Path(str(marker["bundle"])).resolve()
            or bundle.content_hash != marker.get("content_hash")
        ):
            return consumer_layers
        document = json.loads((bundle.root / "layers.json").read_text(encoding="utf-8"))
        rows = document.get("layers") if document.get("schema") == 5 else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return consumer_layers
        if {str(row.get("id") or "") for row in rows} != {
            str(row.get("id") or "") for row in consumer_layers
        }:
            return consumer_layers
        return rows
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return consumer_layers


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
    # The layer whose STATE this finding is about, when it is about one layer's
    # progress rather than the plan itself. Unit-plan generation for layer X must not
    # be blocked by layer Y's stuck state (run bwng97m5n: layer 1's amendment died on
    # 'layer 2 has no ready unit' — a finding the layer-2 rematerialization owns).
    layer: str | None = None

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

    def clean_for(self, layer_id: str) -> bool:
        """Clean for generating THIS layer's unit plan: plan-wide findings and this
        layer's own state findings block; another layer's state-progress findings do
        not — they belong to that layer's own transaction."""
        return not [
            f
            for f in self.blocking
            if f.layer is None or str(f.layer) == str(layer_id)
        ]

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
