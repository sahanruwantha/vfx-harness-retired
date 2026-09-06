"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.domain.unit_outcomes import HypothesisFalsification
from vfx_harness.infrastructure.config import (
    DEFAULT_CRITIC_MODEL,
    DEFAULT_EXECUTION_MODEL,
    PROJECT_ROOT,
)

MODEL = DEFAULT_EXECUTION_MODEL
# The critic scores renders and runs 3-4x per layer to the builder's one session, so it
# dominates layer cost. It was fable-5 on that reasoning; it is opus-5 now because the
# verdict is the pipeline's only measure of quality and a cheaper judge is a false economy
# when every downstream decision rests on it.
#
# NOTE the calibration below: _JUDGE_SD = 0.603 and the adjudication band derived from it
# were MEASURED ON FABLE-5. They are the wrong constants for this judge until re-measured
# (`python -m vfx_harness.evaluation.cli variance <shot>`). Until then the panel is being convened on
# a noise estimate that belongs to a different model.
CRITIC_MODEL = DEFAULT_CRITIC_MODEL
AXES_SYSTEM = """\
You define the CRITIC RUBRIC for one VFX shot. Read brief.md and the reference images,
then output the 5-7 look axes a VFX supervisor would score a render on against THESE
references — the dimensions this specific shot's look lives or dies by (e.g. composition,
atmosphere, the hero subject's detail, environment, palette, finish/grade). Make them
specific to this shot's content and style, not generic. Return ONLY a JSON array of
{"key": "snake_case", "desc": "one concrete line"} and nothing else.
"""

DISTILL_SYSTEM = """\
You harvest REUSABLE Blender recipes from a build that just passed its critic. Read the
build script. Identify 0-2 GENERAL techniques worth reusing on other shots (volumetrics,
materials, compositor/grade, instancing) — NOT shot-specific values or trivia. For each,
Write vfx_harness/knowledge/recipes/<slug>.md with frontmatter (name, tags, blender: "5.2+", when,
verified: false), a short GOTCHAS note, and a parameterized code snippet. If a similar
recipe already exists, improve it instead of duplicating. If nothing is general enough,
write nothing and say so.

ALWAYS write `verified: false`. You are not able to verify anything — you are reading a
script, not running one. `verified: true` is set ONLY by vfx_harness.knowledge.verify_recipes, and only
after the snippet has been EXECUTED in a headless Blender and the top-level callables it
defines actually INVOKED, with what changed and a sha256 of the exact code that ran
recorded as evidence in vfx_harness/knowledge/recipes/_verified.json. `--audit` fails on any recipe
claiming verification without a matching spike entry, and the test suite asserts it — so
declaring it yourself does not merely lie to every future build, it breaks the build.

Verification is earned, by running:

    python -m vfx_harness.knowledge.verify_recipes --name <slug> --sync

which promotes the flag only if the spike actually passes.

Write your snippet so it CAN be verified: put the technique in a top-level function with
plain, defaulted arguments. Code that only runs inside a larger shot-specific block cannot
be proved to work — one recipe's shader function was never called, so verification proved
only that an unrelated loop beneath it ran, and a Blender-4 call inside that function
would have gone undetected.

If the snippet needs scaffolding before it can run — a named material or object it assumes
a real shot provides, a placeholder constant, or arguments the verifier cannot guess —
also Write vfx_harness/knowledge/recipes/_spikes/<slug>.py supplying them. Read
vfx_harness/knowledge/recipes/_spikes/README.md first; it documents the SPIKE_ARGS convention. Keep
that scaffolding OUT of the .md: find_recipe hands the recipe body to a builder verbatim,
and test scaffolding in there gets pasted straight into a shot.
"""

# Layer: the render passes when every axis clears PASS_MIN and the mean clears
# PASS_MEAN (both on the critic's 0–5 scale). Calibrated from data: across 4 builds the
# best/canonical band is 3.1-3.3, and coupled global axes REDISTRIBUTE score under
# revision — 3.3 sat inside that band and was missed by ≤0.16 four times straight.
#
# The "~±0.15 critic noise" this once claimed was WRONG, and wrong in the dangerous
# direction. Measured directly: the same render against the same reference on one axis
# scored 4.0, 3.0, 3.0, 2.0 across four repeats — a 2-point spread that flipped the
# verdict. On a one- or two-axis layer the mean IS that single number, so a lone verdict
# near the line is close to a coin flip. Hence _judge() below, which buys a second and
# third opinion exactly where the decision is uncertain.
PASS_MIN = 2
PASS_MEAN = 3.1

# Circuit breaker. Turns are a poor proxy for what we actually care about — a layer
# needing 200 cheap turns is fine, one burning $40 in 40 turns is not — so cap SPEND
# and leave turns as loose headroom. Observed: BR C $10.69 passing, BR G $15.95 while
# truncated at 120 turns. Both default to unlimited in the SDK.
MAX_BUDGET_USD = 25.0
# Advisory token countdown shown to the model. None disables it; sized from measured
# layer usage rather than one global guess, since an undersized budget causes
# premature partial completion.
TASK_BUDGET_TOKENS: int | None = None

MAX_TURNS = 400
MAX_CONTINUES = 3  # turn-cap nudges before we call the build truncated
# How many times a canonical failure may be handed back before we stop paying for it.
MAX_CANON_REPAIRS = 2

_REPO = PROJECT_ROOT

_RESET = (
    "import bpy\n"
    "_vfx_handlers = bpy.app.handlers\n"
    "for _vfx_handler_name in dir(_vfx_handlers):\n"
    "    _vfx_handler_list = getattr(_vfx_handlers, _vfx_handler_name)\n"
    "    if isinstance(_vfx_handler_list, list):\n"
    "        _vfx_handler_list.clear()\n"
    "bpy.ops.wm.read_factory_settings(use_empty=True)\n"
    "del _vfx_handlers, _vfx_handler_name, _vfx_handler_list\n"
)

# Terminations that mean "the builder never finished", as opposed to "it finished badly".
_TRUNCATED = {"error_max_turns", "error_max_budget_usd"}


class BuildTruncated(RuntimeError):
    """The builder ran out of budget mid-build — no verdict is meaningful."""

    def __init__(self, message: str, *, terminal_cause: str = "build_truncated"):
        self.terminal_cause = terminal_cause
        super().__init__(message)


def _budget_terminal_cause(subtype: str) -> str:
    return (
        "max_turns_exhausted"
        if subtype == "error_max_turns"
        else "model_budget_exhausted"
    )


class UnclaimableUnit(RuntimeError):
    """The next dependency-ready unit is in a state no builder may claim.

    HIR-0214 gave `hypothesis_falsified` a typed stop naming its reviewed transaction.
    Every other unclaimable state kept the old behaviour: the claim refused with a
    traceback from a boundary holding the status, the legal states and the closed
    lifecycle, and the run terminalized as an unclassified `harness_defect` routed to
    engineering.  An operator interrupt -- including one taken to honour a budget
    ceiling -- leaves `building`, which `vfx units retry` clears in seconds.
    """


class BuildUnpassed(RuntimeError):
    """A direct build completed without accepting every unit in its requested layer."""


class BuildAuthorityDefect(RuntimeError):
    """Executable evidence proved that the current plan authority must change.

    The layer runtime carries the already-published strict finding to the public
    CLI boundary.  It deliberately carries no replan choice: the boundary may
    authorize publication of amended authority, but a revision-checked replan is
    selectable only after that replacement authority exists.
    """

    def __init__(
        self,
        finding: dict[str, Any],
        *,
        stage: Literal["builder", "composition"],
        exit_code: int,
        legacy_detail: str,
    ) -> None:
        payload = deepcopy(finding)
        self.finding = HypothesisFalsification.parse(
            payload,
            "builder terminal hypothesis falsification",
        )
        self.finding_payload = payload
        if stage not in {"builder", "composition"}:
            raise ValueError("builder authority defect stage must be builder or composition")
        self.stage = stage
        self.exit_code = int(exit_code)
        if self.exit_code not in {7, 9}:
            raise ValueError("builder authority defect exit code must preserve legacy code 7 or 9")
        self.legacy_detail = str(legacy_detail).strip()
        if not self.legacy_detail:
            raise ValueError("builder authority defect requires legacy terminal detail")
        super().__init__(self.legacy_detail)


class LayerVerdictFailed(RuntimeError):
    """Units passed but the composed layer ledger verdict did not.

    ``finalization`` is the underlying refusal when a terminal receipt exists and did not
    pass, so the public boundary can compile a stop from the receipt instead of from this
    exception's prose (HIR-0248). It is ``None`` for every other publication conflict.
    """

    def __init__(self, message: str, *, finalization: Exception | None = None) -> None:
        self.finalization = finalization
        super().__init__(message)


class UnpassedPrior(RuntimeError):
    """A layer below this one was never accepted — building on it would compound it."""


def builder_model() -> str:
    """Configured live-builder/axes model; resolved after the CLI loads its environment."""
    return builder_package().Settings.from_environment(load_dotenv_file=False).builder_model


def script_model() -> str:
    """Configured finalizer and canonical-repair model."""
    return builder_package().Settings.from_environment(load_dotenv_file=False).script_model


def critic_model() -> str:
    """Configured authoritative visual judge model."""
    return builder_package().Settings.from_environment(load_dotenv_file=False).critic_model


def distiller_model() -> str:
    return builder_package().Settings.from_environment(load_dotenv_file=False).distiller_model
