"""Shared VFX spike eligibility and hypothesis budgets, independent of transport."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from vfx_harness.domain.image_debts import normalize_evidence_id, normalize_evidence_ids
from vfx_harness.domain.plan_records import (
    load_active_structured_decisions,
    load_assumptions,
    read_selected_bundle_hash,
)

# Facts a proxy scene makes true by constructing them: counting the 24 modules you just
# placed, or rendering the response of a rig you just lit, proves the SPIKE exists — not
# that the producing unit's cumulative scene will satisfy the same row. Existence,
# lighting, visibility, and rendered-response evidence belongs to the unit that owns it.
_SPIKE_SELF_FULFILLING_KINDS = {
    "object_count",
    "material_count",
    "material_user_count",
    "material_assignment_fraction",
    "node_count",
    "node_link_count",
    "animation_count",
    "compositor_enabled",
    "control_render_response",
    "frame_delta",
}
_LIGHT_APIS = re.compile(r"light_add|bpy\.data\.lights|lights\.new|type\s*=\s*['\"]LIGHT['\"]")


def _decision_value_signals(shot_folder: Path) -> tuple[set[str], list[tuple[str, str, set[str]]]]:
    """Falsification contract ids and adopted value shapes from recorded decisions."""

    falsification_ids: set[str] = set()
    adopted: list[tuple[str, str, set[str]]] = []
    try:

        for record in load_assumptions(shot_folder):
            falsification_ids.update(normalize_evidence_ids(record.falsification_contract_ids))
    except (OSError, ValueError):
        pass
    resolutions = Path(shot_folder) / "state" / "plan-resolutions.jsonl"
    if resolutions.is_file():
        for line in resolutions.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            falsification = row.get("falsification") or {}
            falsification_ids.update(
                normalize_evidence_ids(falsification.get("contract_ids") or [])
            )
        selected = read_selected_bundle_hash(shot_folder)
        if selected:
            for decision in load_active_structured_decisions(
                resolutions, bundle_hash=selected
            ).values():
                kind = decision.contract.get("kind")
                if kind:
                    adopted.append((
                        decision.id,
                        str(kind),
                        {str(role) for role in decision.contract.get("roles") or []},
                    ))
    return falsification_ids, adopted


def _spike_ineligibility(
    script: str,
    render_frame: object,
    contracts: list[dict],
    shot_folder: Path,
) -> str | None:
    """Refuse hypotheses that only the producing unit can prove.

    Run 20260823T050739Z-7781dd built a proxy iris scene to "prove" the approved
    24-light count and an unlit lighting adversary. The rule forbidding that lived only
    in prompt prose, so the tool accepted the request. Eligibility is now a boundary:
    adopted decision values, decision falsification paths, self-fulfilling construction
    facts, and proxy lighting/visibility reads are refused deterministically.
    """

    falsification_ids, adopted = _decision_value_signals(Path(shot_folder))
    for row in contracts:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "<missing>")
        if row.get("decision_id"):
            return (
                f"spike refused: contract {row_id} adopts decision "
                f"{row.get('decision_id')!r}. An approved value is not a spike hypothesis — "
                "its producing unit proves the adopted contract against the real scene, and "
                "failure routes through the decision's falsification path."
            )
        if normalize_evidence_id(row_id) in falsification_ids:
            return (
                f"spike refused: contract {row_id} is a recorded decision's runtime "
                "falsification path. Only its producing unit may generate that evidence, in "
                "the cumulative scene; a proxy result would recreate the false-evidence "
                "failure this harness exists to prevent."
            )
        kind = str(row.get("kind") or "")
        if kind in _SPIKE_SELF_FULFILLING_KINDS:
            return (
                f"spike refused: contract {row_id} ({kind}) is self-fulfilling in a proxy "
                "scene — the spike constructs the very fact it counts or renders. Existence, "
                "count, lighting, and rendered-response evidence belongs to the producing "
                "unit at build time; record the value as a start with a runtime "
                "falsification contract instead."
            )
        roles = {str(role) for role in row.get("roles") or []}
        for decision_id, adopted_kind, adopted_roles in adopted:
            if kind == adopted_kind and (not adopted_roles or roles & adopted_roles):
                return (
                    f"spike refused: contract {row_id} re-measures decision {decision_id}'s "
                    f"adopted {adopted_kind} values. Approved values are consumed verbatim, "
                    "proven by their producing unit, and revised only through transactional "
                    "replanning — never re-derived in a proxy scene."
                )
    if render_frame is not None and _LIGHT_APIS.search(script or ""):
        return (
            "spike refused: the script creates lights and requests a render — a proxy "
            "lighting/visibility read. Whether something reads lit, unlit, or visible is "
            "cumulative-scene evidence owned by the producing unit; Layer 1 proves it "
            "against the real build. Mechanism spikes stay light-free."
        )
    return None


class _SpikeBudget:
    """A session spike ceiling plus one retry per failed hypothesis.

    One draft spent ten of its twelve spikes discovering the onset_order row schema by
    trial and error — malformed shapes, syntax errors, zero-valued probes — because
    nothing bounded repeated attempts at the same idea. A hypothesis is the exact
    contract rows a spike names (or "exploratory" without any); after the initial
    attempt and one failed retry, further spikes for it are refused so the planner
    records a planner_start with a runtime falsification path instead of paying Blender
    to guess.
    """

    def __init__(self, session_cap: int = 4, attempts_per_hypothesis: int = 2):
        self.session_cap = session_cap
        self.attempts_per_hypothesis = attempts_per_hypothesis
        self.total = 0
        self.failures: dict[str, int] = {}

    @staticmethod
    def key(contracts: list[dict]) -> str:
        if not contracts:
            return "exploratory"
        # IDs are labels the planner controls and therefore cannot define budget identity:
        # the stopped run renamed the same onset-order probe repeatedly. Key the semantic
        # hypothesis instead so cosmetic renaming cannot buy another Blender attempt.
        semantic_fields = (
            "kind", "frame", "frames", "samples", "roles", "control_roles",
            "compare_roles", "compare_control_roles", "component", "property",
        )
        identities = [
            {field: row[field] for field in semantic_fields if field in row}
            for row in contracts
        ]
        payload = json.dumps(sorted(identities, key=lambda row: json.dumps(row, sort_keys=True)),
                             sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def refusal(self, key: str) -> str | None:
        if self.total >= self.session_cap:
            return (
                f"spike budget exhausted ({self.session_cap} spike attempts this session). "
                "Stop probing: record the mechanism as a planner_start with a runtime "
                "falsification contract and let the producing unit prove it in the real scene."
            )
        if self.failures.get(key, 0) >= self.attempts_per_hypothesis:
            return (
                f"this hypothesis already failed {self.attempts_per_hypothesis} spike "
                "attempts. A third blind probe is not evidence — change the approach "
                "(find_recipe, or a different contract kind), or record a planner_start "
                "with a runtime falsification path and move on."
            )
        return None

    def record(self, key: str, *, ran_blender: bool, failed: bool) -> None:
        if ran_blender:
            self.total += 1
        if failed:
            self.failures[key] = self.failures.get(key, 0) + 1

