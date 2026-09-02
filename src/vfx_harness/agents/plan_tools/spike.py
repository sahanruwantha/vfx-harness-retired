"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import textwrap
import time
from pathlib import Path

from vfx_harness.agents.plan_tools.constants import (
    _SPIKE_CONTRACT_MARKER,
    _SPIKE_TIMEOUT,
)
from vfx_harness.agents.plan_tools.media import _SPIKE_HEADER, _SPIKE_RENDER, _contract_probe
from vfx_harness.domain.image_debts import normalize_evidence_id, normalize_evidence_ids
from vfx_harness.domain.plan_records import (
    load_active_structured_decisions,
    load_assumptions,
    read_selected_bundle_hash,
)
from vfx_harness.evidence.scene_checks import _holds, validate_row
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.layer_plans import stamp_work_unit_plan
from vfx_harness.orchestration.plan_authority import selected_artifact_path


def _publish_unit_plan_content(
    shot_folder: str | Path,
    target_path: str | Path,
    content: str,
    *,
    selected_authority=None,
) -> tuple[Path, int]:
    """Atomically publish content to the one harness-selected unit-plan target.

    The bundle-pinned integrity sidecar is stamped in the same publication so the
    consumer view admits the draft: a session's own gate_preview reported every fresh
    unit plan as absent while only the post-session stamp made it visible (run
    20260902T165518Z-004470). Gate attestation still comes only from the terminal gate.
    """
    root = Path(shot_folder).resolve()
    target = Path(target_path).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("target is not under the active shot") from exc
    if relative.parts[:1] != ("plans",):
        raise ValueError("target is not under the shot plans directory")
    if len(content.strip()) < 200:
        raise ValueError("unit plan must contain at least 200 non-whitespace characters")
    lines = content.count("\n") + 1
    if lines > 160:
        raise ValueError(
            f"unit plan has {lines} lines; maximum is 160 — keep evidence in "
            "machine contracts and publish only the execution index"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, content.rstrip() + "\n")
    stamp_work_unit_plan(root, target, selected_authority=selected_authority)
    return target, lines


class _CheckBatchBudget:
    """Two exploratory singles, then at most an initial batch plus one repair batch.

    Run 20260823T065933Z-844d62 spent eleven minutes of a 24-turn draft on Layer-1
    calibration — three batches, three more singles, eighteen rejected candidates —
    without writing a DAG, register, or candidate plan. The prompt said calibration
    stops after one retry; only the tool can mean it. When the second batch completes,
    calibration closes for the session and unresolved image checks are dropped in favor
    of executable scene contracts and build-time falsification.
    """

    def __init__(self, limit: int = 2, batch_limit: int = 2):
        self.limit = limit
        self.batch_limit = batch_limit
        self.singles = 0
        self.batches = 0

    @property
    def closed(self) -> bool:
        return self.batches >= self.batch_limit

    def take_single(self) -> bool:
        if self.closed or self.singles >= self.limit:
            return False
        self.singles += 1
        return True

    def take_batch(self) -> bool:
        if self.closed:
            return False
        self.batches += 1
        return True

    def reset_after_batch(self) -> None:
        self.singles = 0


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
        payload = json.dumps(identities, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def refusal(self, key: str) -> str | None:
        if self.total >= self.session_cap:
            return (
                f"spike budget exhausted ({self.session_cap} Blender runs this session). "
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


def _spike(
    blender: str,
    script: str,
    render_frame: int | None,
    timeout: int,
    py: Path,
    render_out: Path,
    contracts: list[dict] | None = None,
) -> dict:
    body = textwrap.dedent(script)
    contract_rows = list(contracts or [])
    src = (
        _SPIKE_HEADER
        + body
        + _contract_probe(contract_rows)
        + (_SPIKE_RENDER.format(frame=render_frame, out=str(render_out)) if render_frame is not None else "")
    )
    py.write_text(src, encoding="utf-8")
    t0 = time.monotonic()
    # Tests patch the package facade; resolve that seam at call time.
    import vfx_harness.agents.plan_tools as package  # noqa: PLC0415

    executable = package.resolve_blender(blender)
    rc, out, err = package._sh(
        [executable, "--background", "--factory-startup", "--python", str(py)],
        timeout=min(timeout, _SPIKE_TIMEOUT),
    )
    wall = time.monotonic() - t0
    full = (out + "\n--- stderr ---\n" + err).strip()
    py.with_suffix(".out").write_text(full, encoding="utf-8")
    errors = [l for l in full.splitlines() if any(k in l for k in ("Error", "Traceback", "error:", "Exception"))][:10]
    readings = []
    for line in out.splitlines():
        if not line.startswith(_SPIKE_CONTRACT_MARKER):
            continue
        try:
            readings.append(json.loads(line[len(_SPIKE_CONTRACT_MARKER):]))
        except json.JSONDecodeError:
            readings.append({"error": "contract result marker contained invalid JSON"})

    contract_results = []
    for index, row in enumerate(contract_rows):
        reading = readings[index] if index < len(readings) else {
            "error": "contract result missing from Blender output"
        }
        validation_error = validate_row(row)
        error = validation_error or str(reading.get("error") or "")
        contract_results.append({
            "id": str(row.get("id") or "<missing>"),
            "value": reading.get("value"),
            "error": error,
            "pass": not error and _holds(row, reading.get("value")),
        })
    return {
        "rc": rc,
        "wall": wall,
        "tail": full[-2500:],
        "errors": errors,
        "render": render_out if render_out.is_file() else None,
        "contracts": contract_rows,
        "contract_results": contract_results,
        "blender_executable": str(Path(executable).resolve()),
        "blender_version": next(
            (line.strip() for line in full.splitlines() if line.strip().startswith("Blender ")),
            "unknown",
        ),
    }


def _persist_spike_evidence(
    workspace: Path,
    *,
    phase: str,
    number: int,
    script_path: Path,
    render_path: Path,
    result: dict,
    render_frame: int | None = None,
) -> Path | None:
    """Deposit a citable spike record inside a global plan transaction.

    Raw lab files remain run scratch for forensics. This record is the immutable evidence
    surface: publication freezes it with the plan, so a verifier and later consumer can
    reproduce the exact script/output without reading historical run directories.
    """
    if not (workspace / ".plan-workspace.json").is_file():
        return None
    safe_phase = "".join(char if char.isalnum() or char in "-_" else "-" for char in phase).strip("-")
    safe_phase = safe_phase or "global"
    evidence_dir = workspace / "plans" / "evidence" / "spikes"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_phase}-spike-{number:02d}"
    script = script_path.read_text(encoding="utf-8")
    output_path = script_path.with_suffix(".out")
    output = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
    render_name = None
    if render_path.is_file():
        render_name = f"{stem}.png"
        shutil.copy2(render_path, evidence_dir / render_name)
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    script_sha256 = digest(script)
    output_sha256 = digest(output)
    script_name = f"{stem}.py"
    output_name = f"{stem}.out"
    atomic_write(evidence_dir / script_name, script)
    atomic_write(evidence_dir / output_name, output)
    lines = [
        f"# Blender plan spike evidence — {stem}",
        "",
        f"- exit_code: `{int(result['rc'])}`",
        f"- wall_seconds: `{float(result['wall']):.3f}`",
        f"- script_sha256: `{script_sha256}`",
        f"- output_sha256: `{output_sha256}`",
    ]
    if render_name:
        lines.append(f"- render: [{render_name}]({render_name})")
    lines.extend([
        "",
        "## Script",
        "",
        f"<pre>{html.escape(script)}</pre>",
        "",
        "## Full Blender output",
        "",
        f"<pre>{html.escape(output)}</pre>",
        "",
    ])
    target = evidence_dir / f"{stem}.md"
    atomic_write(target, "\n".join(lines))
    contracts = list(result.get("contracts") or [])
    if not contracts:
        return target.relative_to(workspace)
    record = {
        "schema": "vfx-harness.plan-spike/v1",
        "script_sha256": script_sha256,
        "output_sha256": output_sha256,
        "script": {"path": script_name, "sha256": script_sha256},
        "output": {"path": output_name, "sha256": output_sha256},
        "blender": {
            "executable": str(result.get("blender_executable") or ""),
            "version": str(result.get("blender_version") or ""),
        },
        "render_frame": render_frame,
        "markdown": target.relative_to(workspace).as_posix(),
        "contracts": contracts,
        "results": list(result.get("contract_results") or []),
        "passed": bool(result.get("contract_results")) and all(
            item.get("pass") is True for item in result.get("contract_results", [])
        ),
    }
    if render_name:
        record["render"] = {
            "path": render_name,
            "sha256": hashlib.sha256((evidence_dir / render_name).read_bytes()).hexdigest(),
            "frame": render_frame,
        }
    record_path = evidence_dir / f"{stem}.json"
    atomic_write(record_path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record_path.relative_to(workspace)


# --------------------------------------------------------------------------- #
# the MCP server                                                               #
# --------------------------------------------------------------------------- #
def _ready_measure_refs(shot_folder: Path) -> set[str] | None:
    """Return reference paths currently due for executable global authority.

    ``None`` means no sparse DAG exists yet; an empty set is a valid DAG with no due refs.
    """
    layers_path = shot_folder / "layers.json"
    if not layers_path.is_file():
        try:

            layers_path = selected_artifact_path(shot_folder, "layers.json")
        except (FileNotFoundError, ValueError):
            return None
    try:
        layer_rows = json.loads(layers_path.read_text(encoding="utf-8")).get("layers", [])
    except (OSError, ValueError, AttributeError):
        return None
    return {
        str(judge.get("ref") or "").removeprefix("./")
        for layer in layer_rows
        if isinstance(layer, dict) and layer.get("execution", "ready") == "ready"
        for judge in layer.get("judge") or []
        if isinstance(judge, dict)
    }
