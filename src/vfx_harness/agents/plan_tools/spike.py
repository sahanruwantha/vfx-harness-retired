"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
import textwrap
import time
from pathlib import Path

from vfx_harness.agents.plan_tools.constants import (
    _SPIKE_CONTRACT_MARKER,
    _SPIKE_TIMEOUT,
)
from vfx_harness.agents.plan_tools.media import _SPIKE_HEADER, _SPIKE_RENDER, _contract_probe
from vfx_harness.agents.spike_policy import _LIGHT_APIS as _LIGHT_APIS
from vfx_harness.agents.spike_policy import _SPIKE_SELF_FULFILLING_KINDS as _SPIKE_SELF_FULFILLING_KINDS
from vfx_harness.agents.spike_policy import _decision_value_signals as _decision_value_signals
from vfx_harness.agents.spike_policy import _spike_ineligibility as _spike_ineligibility
from vfx_harness.agents.spike_policy import _SpikeBudget as _SpikeBudget
from vfx_harness.evidence.scene_checks import _holds, validate_row
from vfx_harness.observability.provenance import atomic_write
from vfx_harness.orchestration.plan_authority import selected_artifact_path


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
