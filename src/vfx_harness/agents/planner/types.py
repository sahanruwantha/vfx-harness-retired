"""Stage 2 — the PLAN harness."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vfx_harness.domain.stop_envelopes import StopEnvelope

# Materialization authority is the selected bundle, the decision ledger, sealed
# outcomes, and the candidate file. Glob/Grep of historical bundles is not a repair
# instrument; Edit is JSON text-edit of the wrong document (HIR-0023). Task/Agent
# are not remat repair instruments: a spawned Explore burned remat4 reading denied
# paths, then the process died on a broken pipe (HIR-0026).
MATERIALIZATION_DENIED_TOOLS = [
    "Bash",
    "Edit",
    "Glob",
    "Grep",
    "Task",
    "Agent",
    "ListAgents",
    "ScheduleWakeup",
]


@dataclass(frozen=True, slots=True)
class PlanLoopResult:
    """Terminal authority from the deterministic until-clean loop."""

    path: Path
    outcome: str
    blocking_count: int
    plan_pointer: str | None = None
    plan_bundle: str | None = None
    plan_content_hash: str | None = None
    stop_envelope: StopEnvelope | None = None

    @property
    def clean(self) -> bool:
        return self.outcome in {
            "clean", "clean_with_assumptions", "clean_with_deferred"
        } and self.blocking_count == 0


class PlanGateFailure(SystemExit):
    """Exit 3 while preserving a useful run-status detail instead of a traceback."""

    def __init__(
        self,
        result: PlanLoopResult,
        *,
        stop_envelope: StopEnvelope | None = None,
    ):
        envelope = stop_envelope or result.stop_envelope
        if envelope is not None and not isinstance(envelope, StopEnvelope):
            raise ValueError("PlanGateFailure.stop_envelope must be a StopEnvelope")
        self.detail = (
            f"{envelope.stop_class}: {envelope.found} {envelope.next_action}"
            if envelope is not None
            else (
                f"plan loop {result.outcome} with {result.blocking_count} blocking "
                f"finding(s) remaining in {result.path}"
            )
        )
        if envelope is not None:
            self.stop_envelope = envelope
            self.terminal_cause = envelope.stop_class
        self.run_metadata = {
            "outcome": result.outcome,
            "blocking_count": result.blocking_count,
            "plan_gate_report": "reports/plan_gate.json",
            **({"plan_pointer": result.plan_pointer} if result.plan_pointer else {}),
            **({"plan_bundle": result.plan_bundle} if result.plan_bundle else {}),
            **({"plan_content_hash": result.plan_content_hash} if result.plan_content_hash else {}),
        }
        super().__init__(3)

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class PlanRoleCapabilities:
    """Testable workflow contract for one global planning role."""

    role: str
    verbs: frozenset[str]
    allowed_tools: frozenset[str]
    denied_tools: frozenset[str]
    include_gate: bool


def plan_role_capabilities(role: str) -> PlanRoleCapabilities:
    """Return the declared verbs and concrete affordances for a global plan role."""
    if role not in {"draft", "verify", "repair"}:
        raise ValueError(f"unknown global plan role: {role!r}")
    denied = {"Bash"}
    if role == "repair":
        denied.update({"Task", "Agent"})
    return PlanRoleCapabilities(
        role=role,
        verbs=frozenset({"author", "patch", "gate", "escalate"}),
        allowed_tools=frozenset({"Edit"}),
        denied_tools=frozenset(denied),
        include_gate=True,
    )


def _phase_tools(names: list[str], *short_names: str) -> list[str]:
    """Expose only tools that belong to the current authority boundary."""
    suffixes = tuple(f"__{name}" for name in short_names)
    return [name for name in names if name.endswith(suffixes)]


def _planner_tool_policy(repair: bool) -> tuple[list[str], list[str]]:
    """Compatibility adapter for callers that predate explicit role manifests."""
    capabilities = plan_role_capabilities("repair" if repair else "draft")
    return sorted(capabilities.allowed_tools), sorted(capabilities.denied_tools)
