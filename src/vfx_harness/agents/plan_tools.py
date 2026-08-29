"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab.

`build_plan_tools(shot_folder, blender=...)` returns an in-process MCP server and the
qualified tool names for `ClaudeAgentOptions.allowed_tools`:

  - probe_video     ffprobe a refs video (fps / frames / duration / size)
  - contact_sheet   tiled overview of a frame range, source frame numbers burned in
  - extract_frames  up to 4 exact frames at detail, with exposure/structure metrics
  - measure_ref     objective fingerprint of a ref still (plan targets, measured)
  - ask_supervisor  a question only the client can settle; planning continues on your
                    stated assumption and a human answers before the build starts
  (video tools intentionally absent — the shot folder contains stills + brief only)
  - spike           one-shot headless Blender run to VERIFY a researched technique

Scene truth comes from these tools, not from memory: choreography is read off the
source video, fingerprints are measured off the stills, and a researched rig is
proven in the lab before it may enter a ticket.

LOGGING — same doctrine as the build harness (harness narrative via log() + full
transcript via log_message in the runner). Additionally, everything the lab produces
is PERSISTED under the active run's scratch/plan-lab/ for post-mortem forensics:
  - every image the agent saw (downscaled JPEGs, numbered in call order)
  - every spike: NN.py (full script — the transcript clips tool inputs at 400 chars),
    NN.out (full blender stdout+stderr), NN.png (render, when requested)
"""

from __future__ import annotations

import hashlib
import html
import itertools
import json
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

import anyio
from claude_agent_sdk import create_sdk_mcp_server, tool

from vfx_harness.blender.session import resolve_blender
from vfx_harness.blender.tools import _b64, _load, _metrics_line, _stats
from vfx_harness.domain.image_debts import payable_image_property_kinds
from vfx_harness.domain.work_units import work_unit_authoring_schema
from vfx_harness.evidence.checks import METRICS
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.observability.provenance import atomic_write

SERVER_NAME = "plan"
_MAX_TILES = 25  # 5 columns × up to 5 rows per contact sheet
_MAX_FRAMES = 4  # full-detail frames per extract_frames call
_SPIKE_TIMEOUT = 180  # s, hard cap for one headless blender run
_JPEG_Q = 85
_SPIKE_CONTRACT_MARKER = "@@VFX_PLAN_CONTRACT@@"


_CALIBRATION_CLOSED = (
    "CALIBRATION CLOSED: this session already spent its initial batch and one repair "
    "batch. Do not keep tuning image checks — they are optional evidence. Keep the "
    "candidates that passed, DROP the unresolved ones, and write the DAG, ownership "
    "register, and Layer 1 unit now. Anything an image check could not calibrate belongs "
    "to an executable scene contract or the producing unit's build-time falsification."
)


def _publish_unit_plan_content(
    shot_folder: str | Path,
    target_path: str | Path,
    content: str,
) -> tuple[Path, int]:
    """Atomically publish content to the one harness-selected unit-plan target."""
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
    from vfx_harness.domain.image_debts import normalize_evidence_ids
    from vfx_harness.domain.plan_records import (
        load_active_structured_decisions,
        read_selected_bundle_hash,
    )

    falsification_ids: set[str] = set()
    adopted: list[tuple[str, str, set[str]]] = []
    try:
        from vfx_harness.domain.plan_records import load_assumptions

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
    from vfx_harness.domain.image_debts import normalize_evidence_id

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


def _metric_list() -> str:
    """The metric vocabulary, GENERATED from the registry.

    It was hardcoded into the tool descriptions, so `region_lit_variance` existed in
    METRICS and was advertised nowhere — the same shape as contact_sheet being defined and
    never registered. A capability nothing names is indistinguishable from one that does
    not exist.
    """
    from vfx_harness.evidence.checks import METRICS

    return " · ".join(sorted(METRICS))


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if is_error else {})}


def _sh(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# --------------------------------------------------------------------------- #
# video forensics (sync workers, called via anyio.to_thread)                   #
# --------------------------------------------------------------------------- #
def _probe(video: Path) -> dict:
    rc, out, err = _sh(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,nb_frames,duration",
            "-of",
            "json",
            str(video),
        ]
    )
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {err.strip()[:300]}")
    st = json.loads(out)["streams"][0]
    num, den = (st.get("r_frame_rate") or "25/1").split("/")
    fps = float(num) / float(den or 1)
    frames = int(st.get("nb_frames") or 0) or int(float(st.get("duration") or 0) * fps)
    return {
        "width": st.get("width"),
        "height": st.get("height"),
        "fps": fps,
        "frames": frames,
        "duration": float(st.get("duration") or 0),
    }


def _sheet(video: Path, start: int, end: int, step: int, out_dir: Path) -> tuple[Path, list[int]]:
    shown = list(range(start, end + 1, step))[:_MAX_TILES]
    end = shown[-1]
    rows = -(-len(shown) // 5)  # ceil
    vf = (
        f"drawtext=text='%{{frame_num}}':x=10:y=10:fontsize=48:fontcolor=yellow:"
        f"borderw=3:bordercolor=black,"
        f"select='between(n\\,{start}\\,{end})*not(mod(n-{start}\\,{step}))',"
        f"scale=380:-2,tile=5x{rows}"
    )
    out = out_dir / f"sheet_{start}_{end}_{step}.png"
    rc, _, err = _sh(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-vf", vf, "-vsync", "0", "-frames:v", "1", str(out)],
        timeout=180,
    )
    if rc != 0 or not out.is_file():
        raise RuntimeError(f"ffmpeg sheet failed: {err.strip()[:300]}")
    return out, shown


def _frame(video: Path, n: int, fps: float, out_dir: Path) -> Path:
    out = out_dir / f"f{n:05d}.png"
    rc, _, err = _sh(
        ["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{n / fps:.4f}", "-i", str(video), "-frames:v", "1", str(out)],
        timeout=60,
    )
    if rc != 0 or not out.is_file():
        raise RuntimeError(f"ffmpeg frame {n} failed: {err.strip()[:300]}")
    return out


# --------------------------------------------------------------------------- #
# spike lab                                                                    #
# --------------------------------------------------------------------------- #
_SPIKE_HEADER = """\
import bpy
sc = bpy.context.scene
_engines = {i.identifier for i in
            bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
sc.render.engine = ('BLENDER_EEVEE_NEXT' if 'BLENDER_EEVEE_NEXT' in _engines
                    else 'BLENDER_EEVEE')
for _o in list(bpy.data.objects):
    bpy.data.objects.remove(_o, do_unlink=True)
# ---- spike body ----
"""

_SPIKE_RENDER = """
# ---- spike render ----
import bpy
sc = bpy.context.scene
sc.frame_set({frame})
sc.render.resolution_x, sc.render.resolution_y = 960, 540
sc.render.filepath = r"{out}"
sc.render.image_settings.file_format = 'PNG'
bpy.ops.render.render(write_still=True)
print("SPIKE_RENDER_OK", sc.render.filepath)
"""


def _contract_probe(contracts: list[dict]) -> str:
    """Append authoritative scene-contract readings to an exploratory Blender spike."""
    from vfx_harness.evidence.scene_checks import _blender_probe

    snippets = []
    for row in contracts:
        samples = row.get("samples") or []
        frames = row.get("frames") or []
        frame = row.get("frame")
        if frame is None and samples:
            frame = samples[0].get("frame")
        if frame is None and frames:
            frame = frames[0]
        snippets.append(_blender_probe([row], int(frame or 1)))
        snippets.append(
            f"\nprint({_SPIKE_CONTRACT_MARKER!r} + json.dumps(RESULT[0], sort_keys=True))\n"
        )
    return "\n".join(snippets)


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
    executable = resolve_blender(blender)
    rc, out, err = _sh(
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
    from vfx_harness.evidence.scene_checks import _holds, validate_row

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
            from vfx_harness.orchestration.plan_authority import selected_artifact_path

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


def build_plan_tools(
    shot_folder: Path,
    *,
    blender: str = "blender",
    lab_dir: Path | None = None,
    include_gate: bool = False,
    run_layout: run_artifacts.RunLayout | None = None,
    measure_ref_paths: tuple[str, ...] | None = None,
    enabled_tools: frozenset[str] | None = None,
    candidate_materialization: str | Path | None = None,
    overlay_root: str | Path | None = None,
    unit_plan_target: str | Path | None = None,
):
    shot_folder = Path(shot_folder)
    work = Path(tempfile.mkdtemp(prefix="planlab-"))  # raw ffmpeg output
    layout = run_layout or run_artifacts.ensure(shot_folder, command="plan-lab")
    lab = (Path(lab_dir) if lab_dir else
           layout.scratch / "plan-lab" / "global")
    lab.mkdir(parents=True, exist_ok=True)
    try:
        lab_rel = lab.relative_to(shot_folder).as_posix()
    except ValueError:
        # Tests and external callers may deliberately supply an isolated temporary lab.
        lab_rel = str(lab)
    seq = itertools.count(1)
    spikes = itertools.count(1)

    def _resolve(p: str) -> Path:
        path = Path(p).expanduser()
        resolved = (path if path.is_absolute() else shot_folder / path).resolve()
        root = shot_folder.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"plan tool path escapes the active planning workspace: {p!r}"
            ) from exc
        return resolved

    def _keep(im, stem: str) -> Path:
        """Persist exactly what the agent saw (downscaled JPEG) for post-mortem."""
        out = lab / f"{next(seq):03d}_{stem}.jpg"
        im.save(out, format="JPEG", quality=_JPEG_Q)
        return out

    # Exploration is useful; turning fifty independent checks into fifty narrated tool
    # turns is not. This counter is scoped to one plan-agent session. Two single probes let
    # the planner learn a metric/region; after that the batch tool is the only path until a
    # batch has run, at which point two more targeted follow-ups are available.
    check_budget = _CheckBatchBudget()
    spike_budget = _SpikeBudget()
    # Draft and verify share one run, so deterministic reference fingerprints are
    # computed once per image content and reused across both sessions.
    measure_cache_path = (
        lab.parent if lab.parent.name == "plan-lab" else lab
    ) / "measure_ref_cache.json"
    gate_calls = 0
    prior_gate_signature: str | None = None
    materialization_write_lock = anyio.Lock()
    materialization_revision_token: str | None = None
    materialization_axis_ids: tuple[str, ...] | None = None
    if candidate_materialization is not None:
        candidate_path = Path(candidate_materialization)
        if candidate_path.is_file():
            from vfx_harness.orchestration.jit_materialization import (
                materialization_candidate_revision,
            )

            materialization_revision_token = materialization_candidate_revision(candidate_path)
            try:
                candidate_payload = json.loads(candidate_path.read_text(encoding="utf-8"))
                materialization_axis_ids = tuple(
                    str(value)
                    for value in ((candidate_payload.get("layer") or {}).get("owns") or [])
                    if str(value)
                )
            except (OSError, ValueError, AttributeError, json.JSONDecodeError):
                # The staging transaction reports malformed candidate authority. Keep
                # an empty enum here so the tool cannot accept guessed claim axes first.
                materialization_axis_ids = ()

    @tool(
        "publish_unit_plan",
        "Publish the complete bounded work-unit plan to the exact target selected by "
        "the harness. This tool intentionally accepts no path: output location is "
        "authority, not a model decision. The content must be at least 200 characters "
        "and no more than 160 lines.",
        {
            "type": "object",
            "properties": {"content": {"type": "string", "minLength": 200}},
            "required": ["content"],
            "additionalProperties": False,
        },
    )
    async def publish_unit_plan(args):
        if unit_plan_target is None:
            return _text(
                "publish_unit_plan is only available during JIT unit planning",
                is_error=True,
            )
        try:
            content = str(args.get("content") or "")
            target, lines = _publish_unit_plan_content(
                shot_folder, unit_plan_target, content
            )
        except (OSError, ValueError) as exc:
            return _text(f"unit plan publication refused: {exc}", is_error=True)
        return _text(
            f"UNIT PLAN PUBLISHED to {target.relative_to(shot_folder).as_posix()} "
            f"({lines} lines)"
        )

    @tool(
        "probe_video",
        "ffprobe a reference video (path relative to the shot folder, e.g. "
        "'refs/source_25fps.mp4'): fps, frame count, duration, resolution. Call this "
        "FIRST so contact_sheet/extract_frames use real frame numbers.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    async def probe_video(args):
        t0 = time.monotonic()
        try:
            info = await anyio.to_thread.run_sync(_probe, _resolve(args["path"]))
        except Exception as e:
            log(f"plan-lab ✗ probe {args['path']}: {str(e)[:120]}", 1)
            return _text(f"probe failed: {e}", is_error=True)
        log(
            f"plan-lab probe {args['path']} → {info['frames']}f @ {info['fps']:g}fps "
            f"{info['width']}×{info['height']} ({time.monotonic() - t0:.1f}s)",
            1,
        )
        return _text(json.dumps(info))

    @tool(
        "contact_sheet",
        "Tiled overview of a video frame range with SOURCE frame numbers burned into "
        "each tile (yellow, top-left). Args: path, start, end, step — at most 25 tiles "
        "per call (5 columns). Sweep the whole video in a few calls, then re-call with "
        "a small step to zoom into transitions. This is how you do the scene read.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
                "step": {"type": "integer"},
            },
            "required": ["path", "start", "end", "step"],
        },
    )
    async def contact_sheet(args):
        step = max(1, int(args["step"]))
        t0 = time.monotonic()
        try:
            path, shown = await anyio.to_thread.run_sync(
                _sheet, _resolve(args["path"]), int(args["start"]), int(args["end"]), step, work
            )
        except Exception as e:
            log(f"plan-lab ✗ sheet {args.get('start')}–{args.get('end')} step {step}: {str(e)[:120]}", 1)
            return _text(f"contact_sheet failed: {e}", is_error=True)
        im = _load(str(path))
        kept = _keep(im, f"sheet_{shown[0]}-{shown[-1]}_s{step}")
        log(
            f"plan-lab sheet {shown[0]}–{shown[-1]} step {step} "
            f"({len(shown)} tiles, {time.monotonic() - t0:.1f}s) → {kept.name}",
            1,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"frames {shown[0]}–{shown[-1]} step {step} "
                    f"({len(shown)} tiles, read left→right, top→bottom)",
                },
                {"type": "image", "data": _b64(im), "mimeType": "image/jpeg"},
            ]
        }

    @tool(
        "extract_frames",
        "Up to 4 exact video frames at full detail, each with exposure + structure "
        "metrics. Use on the frames that matter (state changes, milestones, rotation "
        "checkpoints) after contact_sheet has located them.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "frames": {"type": "array", "items": {"type": "integer"}}},
            "required": ["path", "frames"],
        },
    )
    async def extract_frames(args):
        video = _resolve(args["path"])
        frames = [int(f) for f in args["frames"]][:_MAX_FRAMES]
        try:
            fps = (await anyio.to_thread.run_sync(_probe, video))["fps"]
        except Exception as e:
            log(f"plan-lab ✗ extract probe {args['path']}: {str(e)[:120]}", 1)
            return _text(f"probe failed: {e}", is_error=True)
        blocks = []
        for n in frames:
            try:
                p = await anyio.to_thread.run_sync(_frame, video, n, fps, work)
            except Exception as e:
                log(f"plan-lab ✗ extract frame {n}: {str(e)[:120]}", 1)
                return _text(f"extract failed at frame {n}: {e}", is_error=True)
            im = _load(str(p))
            _keep(im, f"v{n:05d}")
            blocks.append({"type": "text", "text": f"[v:{n}]\n{_stats(im)}\n{_metrics_line(im)}"})
            blocks.append({"type": "image", "data": _b64(im), "mimeType": "image/jpeg"})
        log(f"plan-lab extract {frames} → {len(frames)} frames kept", 1)
        return {"content": blocks}

    @tool(
        "measure_ref",
        "A reference STILL and its objective fingerprint together — the IMAGE plus "
        "exposure mean/clipped/black, per-band structure σ and halation. Use these "
        "MEASURED numbers as the plan's look targets — never invent fingerprint values. "
        "Read the picture for everything the numbers cannot carry: camera height and "
        "angle, which faces are lit and which fall into shadow, what the silhouette "
        "does, how the light behaves in the air.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    async def measure_ref(args):
        # Measurement scope follows published execution scope. Requiring the sparse DAG
        # first prevents a cold global session from measuring the whole shot before it has
        # decided what is actually due; later JIT sessions resolve their materialized view.
        due_refs = (
            {path.removeprefix("./") for path in measure_ref_paths}
            if measure_ref_paths is not None
            else _ready_measure_refs(shot_folder)
        )
        if due_refs is None:
            return _text(
                "measure_ref is unavailable until layers.json declares the sparse DAG; "
                "write ownership and the first ready unit before measuring its references",
                is_error=True,
            )
        requested = str(args["path"]).removeprefix("./")
        if requested not in due_refs:
            return _text(
                f"measure_ref refused {requested}: it is not judged by a ready global unit; "
                "measure it when its owner layer materializes",
                is_error=True,
            )
        try:
            source = _resolve(args["path"])
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        except Exception as e:
            log(f"plan-lab ✗ measure {args['path']}: {str(e)[:120]}", 1)
            return _text(f"measure failed: {e}", is_error=True)
        try:
            cache = json.loads(measure_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        if (hit := cache.get(digest)) is not None:
            # Draft already measured this exact image; re-tokenizing the picture for a
            # verify pass buys nothing deterministic. The numbers are the same numbers.
            log(f"plan-lab measure {args['path']}: cached fingerprint reused", 1)
            return _text(
                f"{args['path']}\ncanonical fingerprint: "
                f"{json.dumps(hit['fingerprint'], sort_keys=True)}\n"
                "(reused from this run's earlier measurement — the image was already "
                "shown then; Read the file only if you need to view it again)"
            )
        try:
            im = _load(str(source))
        except Exception as e:
            log(f"plan-lab ✗ measure {args['path']}: {str(e)[:120]}", 1)
            return _text(f"measure failed: {e}", is_error=True)
        from vfx_harness.evidence.metrics import canonical_fingerprint

        fingerprint = canonical_fingerprint(im)
        cache[digest] = {"path": str(args["path"]), "fingerprint": fingerprint}
        measure_cache_path.parent.mkdir(parents=True, exist_ok=True)
        measure_cache_path.write_text(
            json.dumps(cache, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        line = f"{args['path']}\ncanonical fingerprint: {json.dumps(fingerprint, sort_keys=True)}"
        log(f"plan-lab measure {args['path']}: {_stats(im).removeprefix('exposure: ')}", 1)
        # The image travels WITH its numbers. This tool used to return text only, so
        # "measured it" and "looked at it" were separable — and the one plan written that
        # way scored 26 mentions of halation (which measure_ref reports) against ZERO for
        # camera angle, shadow side or solid form (which only the picture carries). Those
        # are exactly the properties no exposure statistic can express, and the resulting
        # render read as a flat card. A planner can still decline to look; it can no
        # longer measure without being shown.
        return {
            "content": [
                {"type": "text", "text": line},
                {"type": "image", "data": _b64(im), "mimeType": "image/jpeg"},
            ]
        }

    @tool(
        "measure_check",
        "RUN a candidate done-check before you commit it to a ticket. Returns its value on "
        "the reference, its value on the adversary you name, this metric's own resampling "
        "noise, and a verdict. A check may not enter the plan until this returns OK.\n"
        f"metric: {_metric_list()}.\n"
        "regions: normalised [x0,y0,x1,y1] in 0..1, origin TOP-LEFT (x right, y down) — "
        "key 'r' for the single-region metrics, "
        "region_ratio accepts exactly two semantic names in numerator/denominator order "
        "(or explicit numerator/denominator). op: '>=' | '<=' | 'band' with lo/hi.\n"
        "rejects: paths to renders this check EXISTS TO REJECT — name the artifact showing "
        "the defect you are guarding against. Without it the check is graded against "
        "whatever bad renders happen to exist, and a check that only rejects an easy "
        "unrelated failure looks discriminating while being blind to its real target.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "metric": {"type": "string"},
                "op": {"type": "string"},
                "lo": {"type": "number"},
                "hi": {"type": "number"},
                "ref": {"type": "string"},
                "regions": {"type": "object"},
                "rejects": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["metric", "op", "ref"],
        },
    )
    async def measure_check(args):
        if check_budget.closed:
            log("plan-lab x measure_check: calibration closed", 1)
            return _text(_CALIBRATION_CLOSED, is_error=True)
        if not check_budget.take_single():
            log("plan-lab x measure_check: single-call exploration cap reached", 1)
            return _text(
                "SINGLE-CHECK EXPLORATION CAP REACHED. Draft the remaining candidates as "
                "one CHECK MANIFEST and call measure_checks(checks=[...]). The cap resets "
                "after a batch so you can probe up to two rejected cases precisely.",
                is_error=True,
            )
        from vfx_harness.evidence.checks import Check, verify

        try:
            c = Check.from_dict({**args, "lo": args.get("lo", float("-inf")), "hi": args.get("hi", float("inf"))})
            ref = _resolve(args["ref"])
            if not ref.is_file():
                return _text(f"reference {args['ref']} does not exist", is_error=True)
            corpus = sorted(
                q
                for sib in shot_folder.parent.glob("*/renders")
                if sib.parent.name != shot_folder.name and not sib.parent.name.startswith("_")
                for q in sib.glob("*_best.png")
            )
            v = await anyio.to_thread.run_sync(lambda: verify(c, ref, corpus, root=Path.cwd()))
        except Exception as e:
            log(f"plan-lab x measure_check {args.get('id')}: {str(e)[:120]}", 1)
            return _text(f"measure_check failed: {e}", is_error=True)
        head = "OK - this check is fit to commit" if v.ok else "REJECTED - do NOT commit this"
        body = [
            f"{head}",
            f"  target      {c.target()} on {c.metric}",
            f"  reference   {args['ref']} reads {v.ref_value:.4g}"
            f"  -> {'satisfies' if v.ref_value is not None and c.holds(v.ref_value) else 'FAILS'}",
        ]
        if v.bad_values:
            caught = not all(c.holds(x) for x in v.bad_values)
            body.append(
                f"  adversary   reads {min(v.bad_values):.4g}..{max(v.bad_values):.4g}"
                f"  -> {'REJECTED by the check (good)' if caught else 'PASSES the check (BAD)'}"
            )
        if v.floor:
            body.append(f"  noise floor {v.floor:.4g} (this metric's own movement under resampling)")
        body += [f"  ! {r}" for r in v.reasons]
        if v.ok and v.ref_value is not None:
            adv = f"[{v.bad_values[0]:.4g}]" if v.bad_values else "[]"
            body += [
                "",
                "  COPY THIS into the check's `proof` field, unedited:",
                f'    "proof": {{"ref": {v.ref_value:.4g}, "adversary": {adv}}}',
                "  The gate re-runs the spec you ship and compares it to these numbers. If you",
                "  change the regions or thresholds afterwards, RUN IT AGAIN — a proof that does",
                "  not reproduce means the spec you tested is not the spec you shipped.",
            ]
        log(f"plan-lab measure_check {args.get('id', c.metric)}: {'OK' if v.ok else 'REJECTED'}", 1)
        return _text("\n".join(body))

    @tool(
        "measure_checks",
        "Run MANY candidate done-checks in ONE call. Same rules and same verdicts as "
        "measure_check, but authoring 50 checks one at a time cost 91 round-trips, 112 "
        "turns and 133k output tokens in a single repair round — the model was narrating "
        "between independent measurements that have no bearing on each other. Batch them.\n"
        "Pass `checks`: a list of the same objects measure_check takes (metric, op, lo/hi, "
        "ref, regions, rejects). Returns one compact line per check plus a paste-ready "
        "`proof` block for the ones that pass. Up to 40 per call. AT MOST TWO batches per "
        "session — the initial manifest and ONE repair of its rejects. After that "
        "calibration closes: drop unresolved candidates (image checks are optional) and "
        "proceed on executable scene contracts and build-time falsification.",
        {
            "type": "object",
            "properties": {"checks": {"type": "array", "items": {"type": "object"}}},
            "required": ["checks"],
        },
    )
    async def measure_checks(args):
        from vfx_harness.evidence.checks import Check, verify

        specs = list(args.get("checks") or [])[:40]
        if not specs:
            return _text("no checks supplied", is_error=True)
        if not check_budget.take_batch():
            log("plan-lab x measure_checks: calibration closed", 1)
            return _text(_CALIBRATION_CLOSED, is_error=True)
        corpus = sorted(
            q
            for sib in shot_folder.parent.glob("*/renders")
            if sib.parent.name != shot_folder.name and not sib.parent.name.startswith("_")
            for q in sib.glob("*_best.png")
        )

        def _run() -> tuple[list[str], dict]:
            lines, proofs, n_ok = [], {}, 0
            for d in specs:
                cid = str(d.get("id", "?"))
                try:
                    c = Check.from_dict({**d, "lo": d.get("lo", float("-inf")), "hi": d.get("hi", float("inf"))})
                    ref = _resolve(d.get("ref", ""))
                    if not ref.is_file():
                        lines.append(f"  REJECTED {cid:10} ref {d.get('ref')} does not exist")
                        continue
                    v = verify(c, ref, corpus, root=Path.cwd())
                except Exception as e:
                    lines.append(f"  REJECTED {cid:10} {str(e)[:90]}")
                    continue
                if v.ok:
                    n_ok += 1
                    proofs[cid] = {"ref": round(v.ref_value, 4), "adversary": [round(x, 4) for x in v.bad_values[:1]]}
                    lines.append(
                        f"  OK       {cid:10} {c.metric} {c.target()} "
                        f"· ref {v.ref_value:.4g}" + (f" · adv {v.bad_values[0]:.4g}" if v.bad_values else "")
                    )
                else:
                    why = v.reasons[0].split("—")[0].strip() if v.reasons else "failed"
                    detail = v.reasons[0].split("—", 1)[1].strip()[:150] if v.reasons and "—" in v.reasons[0] else ""
                    lines.append(f"  REJECTED {cid:10} {why}: {detail}")
            return lines, proofs, n_ok

        lines, proofs, n_ok = await anyio.to_thread.run_sync(_run)
        check_budget.reset_after_batch()
        head = (
            f"{n_ok}/{len(specs)} fit to commit. REJECTED ones must be fixed or dropped — the gate re-runs every rule."
        )
        tail = (
            (
                "\n\nPaste these `proof` values into the matching records, unedited. If you "
                "then change a region or threshold, RUN IT AGAIN:\n" + json.dumps(proofs, indent=1)
            )
            if proofs
            else ""
        )
        log(f"plan-lab measure_checks: {n_ok}/{len(specs)} ok", 1)
        return _text(head + "\n" + "\n".join(lines) + tail)

    @tool(
        "spike",
        "VERIFY a researched technique in a one-shot headless Blender before it enters "
        "a ticket. Your script runs from an EMPTY scene (engine preset to EEVEE); build "
        "the minimal rig that proves the mechanism (seconds, not a look test). Pass "
        "render_frame to get a 960×540 render back; always print() the values you need "
        "to check. When the spike is evidence for a ticket, pass the exact final "
        "scene-contract rows in contracts and tag the spike objects with their semantic "
        "bvfx_role/bvfx_control values. The tool runs those contracts inside the same "
        "scene and emits a typed citable record; an exploratory spike without contracts "
        "cannot justify a ✓spiked ticket. ELIGIBILITY is enforced: approved/adopted "
        "decision values, decision falsification paths, existence/count/rendered-response "
        "facts, and lighting/visibility reads are refused — those are the producing "
        "unit's evidence against the cumulative scene, not a proxy hypothesis. No bvfx "
        "helpers here — raw bpy, exactly like the internet snippet you are testing.",
        {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "render_frame": {"type": "integer"},
                "timeout": {"type": "integer"},
                "contracts": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Exact scene_checks.json rows this spike claims to prove. Each row "
                        "is evaluated in the spike scene at its declared frame."
                    ),
                },
            },
            "required": ["script"],
        },
    )
    async def spike(args):
        contracts = list(args.get("contracts") or [])
        veto = _spike_ineligibility(
            str(args.get("script") or ""), args.get("render_frame"), contracts, shot_folder
        )
        if veto is not None:
            log("plan-lab ✗ spike refused: ineligible hypothesis", 1)
            return _text(veto, is_error=True)
        hypothesis = _SpikeBudget.key(contracts)
        if (refusal := spike_budget.refusal(hypothesis)) is not None:
            log(f"plan-lab ✗ spike refused ({hypothesis[:60]}): budget", 1)
            return _text(refusal, is_error=True)
        n = next(spikes)
        py = lab / f"spike_{n:02d}.py"
        render_out = lab / f"spike_{n:02d}.png"
        if contracts:
            from vfx_harness.evidence.scene_checks import KIND_DEFINITIONS, validate_row

            invalid = [
                f"{row.get('id', '<missing>')}: {error}"
                for row in contracts
                if (error := validate_row(row))
            ]
            if invalid:
                # A malformed row costs a failed attempt: blind schema discovery through
                # repeated probes is exactly what the per-hypothesis budget bounds.
                spike_budget.record(hypothesis, ran_blender=False, failed=True)
                kinds = sorted({
                    str(row.get("kind"))
                    for row in contracts
                    if row.get("kind") in KIND_DEFINITIONS
                })
                hint = "\n".join(
                    f"  {kind}: {KIND_DEFINITIONS[kind]}" for kind in kinds
                )
                return _text(
                    "spike contracts are invalid — fix the exact rows before running Blender:\n"
                    + "\n".join(f"- {item}" for item in invalid)
                    + (f"\nkind reference:\n{hint}" if hint else ""),
                    is_error=True,
                )
        try:
            res = await anyio.to_thread.run_sync(
                _spike,
                blender,
                args["script"],
                args.get("render_frame"),
                int(args.get("timeout", 120)),
                py,
                render_out,
                contracts,
            )
        except subprocess.TimeoutExpired:
            spike_budget.record(hypothesis, ran_blender=True, failed=True)
            log(f"plan-lab ✗ spike #{n} TIMEOUT → {py.name}", 1)
            return _text(
                f"spike timed out — simplify the rig or raise timeout "
                f"(script kept: {lab_rel}/{py.name})",
                is_error=True,
            )
        except Exception as e:
            spike_budget.record(hypothesis, ran_blender=True, failed=True)
            log(f"plan-lab ✗ spike #{n} launch failed: {str(e)[:120]}", 1)
            return _text(f"spike failed to launch: {e}", is_error=True)
        contract_failures = [
            item for item in res.get("contract_results", []) if item.get("pass") is not True
        ]
        spike_budget.record(
            hypothesis,
            ran_blender=True,
            failed=bool(res["rc"] != 0 or res["errors"] or contract_failures),
        )
        status = (
            "ok" if res["rc"] == 0 and not res["errors"] and not contract_failures
            else "CONTRACT FAIL" if contract_failures and res["rc"] == 0 and not res["errors"]
            else "ERRORS"
        )
        evidence_rel = _persist_spike_evidence(
            shot_folder,
            phase=lab.name,
            number=n,
            script_path=py,
            render_path=render_out,
            result=res,
            render_frame=args.get("render_frame"),
        )
        log(
            f"plan-lab spike #{n} rc={res['rc']} {res['wall']:.1f}s [{status}] "
            f"→ {py.name}" + (f" + {render_out.name}" if res["render"] else ""),
            1,
        )
        for e in res["errors"][:3]:
            log(f"· {e[:160]}", 2)
        head = f"spike #{n} · exit {res['rc']} in {res['wall']:.1f}s · kept: {lab_rel}/{py.name}" + (
            f" · ERRORS: {' | '.join(res['errors'])}" if res["errors"] else ""
        )
        if evidence_rel is not None:
            head += f" · citable evidence: {evidence_rel.as_posix()}"
        if contract_failures:
            head += " · CONTRACT FAILURES: " + " | ".join(
                f"{item.get('id')}: {item.get('error') or item.get('value')}"
                for item in contract_failures
            )
        blocks = [{"type": "text", "text": f"{head}\n--- output tail ---\n{res['tail']}"}]
        if res["render"] is not None:
            im = _load(str(res["render"]))
            blocks.append({"type": "text", "text": _stats(im)})
            blocks.append({"type": "image", "data": _b64(im), "mimeType": "image/jpeg"})
        return {
            "content": blocks,
            **({"is_error": True} if res["rc"] != 0 or contract_failures else {}),
        }

    # probe_video / contact_sheet / extract_frames are NOT registered: a real brief
    # arrives as reference images plus prose, never the finished shot. They were dead
    # tools whose doctrine ("SCENE READ — probe_video first") the planner still tried to
    # follow, silently degrading to five stills for a 480-frame shot. Every unregistered
    # tool is also schema text removed from every request.
    @tool(
        "ask_supervisor",
        "Raise a question ONLY the client can settle — an ambiguity in the brief, a "
        "contradiction between the brief and the stills, or a taste call that is theirs. "
        "Does not block: state the assumption you will plan on and continue. A human "
        "answers before an AFFECTED layer starts. Name the affected layer ids and/or "
        "owned axes; use global_decision only when every layer truly depends on it. "
        "Do NOT use it for anything measure_ref or a spike could answer.",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "assumption": {"type": "string"},
                "why_it_matters": {"type": "string"},
                "affected_layers": {"type": "array", "items": {"type": "string"}},
                "affected_axes": {"type": "array", "items": {"type": "string"}},
                "global_decision": {"type": "boolean"},
            },
            "required": ["question", "assumption", "affected_layers"],
        },
    )
    async def ask_supervisor(args):
        from vfx_harness.orchestration.escalate import ask as _ask

        qid = _ask(
            shot_folder,
            layer="PLAN",
            question=args["question"],
            assumption=args["assumption"],
            why_it_matters=args.get("why_it_matters", ""),
            affected_layers=args.get("affected_layers") or [],
            affected_axes=args.get("affected_axes") or [],
            global_decision=bool(args.get("global_decision")),
        )
        return _text(f"Recorded as Q{qid}. Continue planning on: {args['assumption']}")

    @tool(
        "escalate_vocabulary_gap",
        "Record that NO evidence kind can express a claim you must close. This is the "
        "honest alternative to padding: a typed durable record of the requirement, the "
        "kinds you attempted, and why each cannot certify the claim. Close the "
        "requirement with an explicit decision resolution that references the returned "
        "gap id — never with a trivially-satisfiable contract (those are rejected at "
        "validation). Gaps are visible to the operator and to future planning sessions.",
        {
            "type": "object",
            "properties": {
                "requirement_id": {"type": "string"},
                "claim": {"type": "string"},
                "attempted": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string"},
                            "why_it_cannot_certify": {"type": "string"},
                        },
                        "required": ["kind", "why_it_cannot_certify"],
                    },
                    "minItems": 1,
                },
                "note": {"type": "string"},
            },
            "required": ["requirement_id", "claim", "attempted"],
        },
    )
    async def escalate_vocabulary_gap(args):
        record_dir = shot_folder / "state" / "plan-escalations"
        record_dir.mkdir(parents=True, exist_ok=True)
        path = record_dir / "vocabulary-gaps.jsonl"
        existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        gap_id = f"VG-{len(existing) + 1:03d}"
        record = {
            "schema": "vfx-harness.vocabulary-gap/v1",
            "id": gap_id,
            "requirement_id": str(args["requirement_id"]),
            "claim": str(args["claim"]),
            "attempted": [
                {
                    "kind": str(item.get("kind") or ""),
                    "why_it_cannot_certify": str(item.get("why_it_cannot_certify") or ""),
                }
                for item in args["attempted"]
            ],
            "note": str(args.get("note") or ""),
            "run_id": layout.run_id,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        log(f"plan-lab vocabulary gap {gap_id}: {args['requirement_id']} — {str(args['claim'])[:70]}", 1)
        return _text(
            f"Recorded {gap_id} for {args['requirement_id']}. Close the requirement with an "
            f"explicit decision resolution referencing {gap_id} (statement + "
            f"decision_strength), not a contract. The gap is durable state: the harness "
            f"grows the vocabulary against it, and a later generation re-binds the "
            f"requirement to a real metric."
        )

    @tool(
        "run_gate",
        "Run the free deterministic plan gate against the current working artifacts. "
        "Use during draft, verify, or repair after a coherent artifact sweep so cross-file "
        "defects are fixed while context is warm. Read-only and capped at four calls per "
        "planning session.",
        {"type": "object", "properties": {}},
    )
    async def run_gate(args):
        nonlocal gate_calls, prior_gate_signature
        gate_calls += 1
        if gate_calls > 4:
            return _text(
                "run_gate call cap reached (4); finish the bounded planning sweep",
                is_error=True,
            )
        from vfx_harness.domain.brief import load_shot
        from vfx_harness.evaluation import plan_gate

        result = plan_gate.run(shot_folder, require_scene_checks=True)
        # The filesystem leaf is deliberately named ``plan-workspace``; reports and
        # feedback must retain the authored shot identity from brief.md instead.
        result.shot = load_shot(shot_folder).id
        body = plan_gate.report(result)
        signature = result.signature()
        if not result.clean and signature == prior_gate_signature:
            return _text(
                body
                + "\n\nGATE PLATEAU: findings are unchanged from the previous call. "
                "Stop editing and end this session; the outer deterministic loop owns "
                "any further repair.",
                is_error=True,
            )
        prior_gate_signature = signature
        repair = plan_gate.feedback(result)
        return _text(body + (f"\n\nREPAIR BRIEF\n{repair}" if repair else ""))

    @tool(
        "evidence_vocabulary",
        "The complete registry of scene-contract evidence kinds: definition, evidence "
        "domain, and the structural fields each kind requires. Call this BEFORE "
        "authoring contracts, and whenever a validator error mentions a kind, a "
        "property, or a vacuous target — run 20260824T153427Z-91b7c1 burned 8 write "
        "rounds guessing at a vocabulary this call returns in one turn. If no kind can "
        "express a claim, say so via ask_supervisor instead of padding with a "
        "trivially-satisfiable contract; padding shapes are rejected at validation.",
        {"type": "object", "properties": {}},
    )
    async def evidence_vocabulary(args):
        from vfx_harness.evidence.scene_checks import (
            FRAME_SCOPED_KINDS,
            KIND_DEFINITIONS,
            KIND_DOMAINS,
            SUPPORTED_KINDS,
            WINDOW_KINDS,
        )

        extra_fields = {
            "keyframe_schedule": [
                "samples: [{frame, values:{property: scalar|vector}}, …] (≥2, unique frames)"
            ],
            "object_property": [
                "frame",
                "property (Blender-evaluated path only — custom properties are "
                "self-certification and rejected; vector components use numeric paths "
                "such as location.2 or rotation_euler.1, not location.z)",
            ],
            "path_clearance_min": [
                "frames [a,b]",
                "compare_roles (obstacle roles, disjoint from roles)",
                "frame_step (optional)",
                "empty compare_roles match is not clearance (fail closed, not 1e9)",
            ],
            "parallax_displacement_profile": [
                "frames [a,b]",
                "compare_roles (far group, disjoint from roles)",
            ],
            "curve_derivative_max": ["frames [a,b]", "property (location|rotation_euler|scale)"],
            "onset_order": ["frames [a,b]", "compare_roles/compare_control_roles (disjoint)"],
            "transform_return_delta": ["frames [a,b]", "component (location|rotation|scale)"],
            "control_render_response": [
                "graph", "node_roles", "probe_values [lo,hi]", "region [x0,y0,x1,y1]",
                "frame (the render frame the sweep measures — the subject must be "
                "VISIBLE there; pair with a visible_fraction row)",
                "response_metric (mean_delta is luminance-only and reads ~0 for pure "
                "hue/tint shifts — palette semantics need mae)",
                "socket/socket_index (optional — otherwise resolution needs a socket "
                "literally named 'Value': the control tag belongs on a ShaderNodeValue, "
                "and the tagged control must stay FREE of drivers)",
            ],
            "frame_delta": ["frames [a,b]", "region (optional)"],
            "node_socket_value": [
                "graph (material|compositor|world)",
                "node_roles",
                "socket (name) or socket_index",
                "direction (input|output)",
                "component (optional, for vector sockets: channel index 0-3 or R/G/B/A)",
            ],
            "node_count": ["graph (material|compositor|world)", "node_roles"],
            "render_region_stat": [
                "stat (mean|stddev luminance, or mean_r/mean_g/mean_b channel means, "
                "all 0-255)",
                "region [x0,y0,x1,y1]",
                "op min/max/band with targets copied from measure_ref's reading of the "
                "judge reference — THE exposure anchor: every relative metric passes at "
                "any brightness, and luminance-only anchors pass a colorless frame "
                "(express 'amber' as mean_r above mean_b via two rows)",
            ],
            "visible_fraction": [
                "roles (the surfaces this judge frame is judged ON — occluders need no "
                "declaration, any closer surface counts)",
                "op min lo≈0.2–0.5 for must-be-seen; op max hi<1 for not-yet-revealed",
            ],
            "projected_origin_x": [
                "roles/control_roles selecting exactly one object (Empty/control is legal)",
                "op min/max/band in normalized camera coordinates; camera-alignment only, "
                "not visibility; repair_owner must provide camera",
            ],
            "projected_origin_y": [
                "roles/control_roles selecting exactly one object (Empty/control is legal)",
                "op min/max/band in normalized top-left camera coordinates; camera-alignment "
                "only; repair_owner must provide camera",
            ],
            "node_link_count": [
                "graph", "from_node_roles", "to_node_roles",
                "from_socket/to_socket (optional)",
            ],
        }
        entries = {}
        for kind in sorted(SUPPORTED_KINDS):
            fields = []
            if kind in WINDOW_KINDS and kind not in extra_fields:
                fields.append("frames [a,b]")
            if kind in FRAME_SCOPED_KINDS and kind != "object_property":
                fields.append("frame")
            fields.extend(extra_fields.get(kind, []))
            entries[kind] = {
                "definition": KIND_DEFINITIONS.get(kind, ""),
                "domain": KIND_DOMAINS.get(kind, "scene"),
                "fields": fields,
            }
        note = (
            "Projected bbox_* and projected_origin_* targets must lie inside the normalized frame; "
            "bbox/visible_fraction require rendered surfaces, while projected_origin_* is "
            "the camera-owner alignment instrument for Empty/control hosts. The control "
            "producer proves fixed world state with scene evidence and publishes a typed "
            "placement_control; the camera successor depends on it, declares the exact "
            "consume, and owns projection without mutating the observed selector. "
            "path_clearance_min fails closed on an empty obstacle selection — persistent "
            "lifecycle re-evaluates as geometry arrives, it does not make absence a PASS."
        )
        return _text(json.dumps({"kinds": entries, "note": note}, indent=1))

    gate_preview_calls = 0
    prior_preview_signature: str | None = None

    @tool(
        "gate_preview",
        "Run the deterministic plan gate against the CURRENT consumer view (selected "
        "bundle + materialized layers + staged unit plans) — the exact evaluation "
        "terminal publication will apply. Use it before finishing so findings become "
        "fixes in this session instead of a retracted artifact. Read-only; capped at "
        "three calls per session.",
        {"type": "object", "properties": {}},
    )
    async def gate_preview(args):
        nonlocal gate_preview_calls, prior_preview_signature
        gate_preview_calls += 1
        if gate_preview_calls > 3:
            return _text(
                "gate_preview call cap reached (3); finish the artifact and let the "
                "terminal gate decide",
                is_error=True,
            )
        from vfx_harness.domain.brief import load_shot
        from vfx_harness.evaluation import plan_gate
        from vfx_harness.orchestration.plan_authority import prepare_consumer_view

        try:
            view = await anyio.to_thread.run_sync(prepare_consumer_view, layout)
            candidate = Path(candidate_materialization) if candidate_materialization else None
            if candidate is not None and candidate.is_file():
                # stage the session's own unpublished payload so the gate previews the
                # POST-publication world — two generations published on a false CLEAN
                # because the preview saw the pre-publication view
                from vfx_harness.orchestration.jit_materialization import stage_candidate_view

                try:
                    await anyio.to_thread.run_sync(
                        lambda: stage_candidate_view(
                            shot_folder, candidate, view, overlay_root=overlay_root
                        )
                    )
                except (ValueError, OSError) as exc:
                    return _text(
                        f"candidate materialization does not validate, so the gate has "
                        f"nothing to preview: {exc}",
                        is_error=True,
                    )
            result = await anyio.to_thread.run_sync(
                lambda: plan_gate.run(view, require_scene_checks=False)
            )
        except Exception as exc:
            log(f"plan-lab ✗ gate_preview: {str(exc)[:120]}", 1)
            return _text(f"gate preview failed: {exc}", is_error=True)
        result.shot = load_shot(shot_folder).id
        body = plan_gate.report(result)
        log(f"plan-lab gate_preview → {'CLEAN' if result.clean else f'{len(result.blocking)} blocking'}", 1)
        signature = result.signature()
        if not result.clean and signature == prior_preview_signature:
            return _text(
                body
                + "\n\nGATE PLATEAU: findings are unchanged from the previous preview. "
                "Anything you cannot fix from inside this session (missing unit plan, "
                "another layer's authority) belongs to the outer flow — finish your "
                "artifact and report the residue.",
                is_error=True,
            )
        prior_preview_signature = signature
        repair = plan_gate.feedback(result)
        return _text(body + (f"\n\nREPAIR BRIEF\n{repair}" if repair else ""))

    @tool(
        "stage_materialization_unit",
        "Stage exactly one bounded work unit plus the scene contracts and requirement "
        "bindings it owns into the harness-seeded candidate. There is no path argument. "
        "Issue one staging call, wait for its result, then author the next unit. The unit "
        "parameter is a closed schema: use producer/interface_id/kind for consumes, one "
        "of none/keyframes/motion for temporal_evidence, and omit composition_context "
        "unless it has non-empty frames plus exactly one source_unit or contract_ids. "
        "Every claim.axis enumerates the active layer's exact owned axis ids. "
        "After all units, call finalize_materialization. This is unpublished scratch "
        "state; duplicate unit, contract, or requirement ids are refused.",
        {
            "type": "object",
            "properties": {
                "unit": work_unit_authoring_schema(
                    image_property_kinds=payable_image_property_kinds(METRICS),
                    axis_ids=materialization_axis_ids,
                ),
                "scene_contracts": {"type": "array", "items": {"type": "object"}},
                "requirement_bindings": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "layer_updates": {
                    "type": "object",
                    "properties": {
                        "dressable": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["unit", "scene_contracts", "requirement_bindings"],
            "additionalProperties": False,
        },
    )
    async def stage_materialization_unit_tool(args):
        nonlocal materialization_revision_token
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "stage_materialization_unit is only available during layer materialization",
                is_error=True,
            )
        from vfx_harness.orchestration.jit_materialization import (
            materialization_candidate_revision,
            stage_materialization_unit,
        )

        try:
            async with materialization_write_lock:
                await anyio.to_thread.run_sync(
                    lambda: stage_materialization_unit(
                        candidate,
                        unit=args.get("unit"),
                        scene_contracts=args.get("scene_contracts") or [],
                        requirement_bindings=args.get("requirement_bindings") or [],
                        layer_updates=args.get("layer_updates"),
                        expected_revision=materialization_revision_token,
                    )
                )
                materialization_revision_token = materialization_candidate_revision(candidate)
                payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _text(f"unit staging refused: {exc}", is_error=True)
        return _text(
            f"STAGED unit {args['unit'].get('id', '<missing>')}: "
            f"candidate now has {len(payload['layer']['stages'])} unit(s), "
            f"{len(payload['scene_contracts'])} contract(s), and "
            f"{len(payload['requirement_bindings'])} requirement binding(s). "
            "Stage the next independent unit, or call finalize_materialization."
        )

    @tool(
        "unstage_materialization_unit",
        "Remove exactly one previously staged unit from unpublished materialization "
        "scratch when later validation proves the decomposition wrong. The locked, "
        "revision-checked transaction also removes contracts no surviving unit binds "
        "and prunes requirement bindings that become empty. It refuses while a "
        "surviving unit depends on or consumes the target; unstage in reverse dependency "
        "order or patch those exact references first. This never changes selected "
        "authority or durable work-unit state.",
        {
            "type": "object",
            "properties": {"unit_id": {"type": "string", "minLength": 1}},
            "required": ["unit_id"],
            "additionalProperties": False,
        },
    )
    async def unstage_materialization_unit_tool(args):
        nonlocal materialization_revision_token
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "unstage_materialization_unit is only available during layer materialization",
                is_error=True,
            )
        from vfx_harness.orchestration.jit_materialization import (
            materialization_candidate_revision,
            unstage_materialization_unit,
        )

        try:
            async with materialization_write_lock:
                result = await anyio.to_thread.run_sync(
                    lambda: unstage_materialization_unit(
                        candidate,
                        unit_id=args.get("unit_id"),
                        expected_revision=materialization_revision_token,
                    )
                )
                materialization_revision_token = materialization_candidate_revision(candidate)
                payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _text(f"unit unstaging refused: {exc}", is_error=True)
        return _text(
            f"UNSTAGED unit {result.unit_id}: removed contract ids "
            f"{list(result.removed_contract_ids)} and empty requirement bindings "
            f"{list(result.removed_requirement_ids)}; candidate now has "
            f"{len(payload['layer']['stages'])} unit(s), "
            f"{len(payload['scene_contracts'])} contract(s), and "
            f"{len(payload['requirement_bindings'])} requirement binding(s)."
        )

    @tool(
        "materialization_status",
        "Return compact harness-owned progress for the seeded materialization candidate: "
        "staged unit ids and counts only. Use this instead of Read; raw candidate JSON is "
        "not a model context surface.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def materialization_status(args):
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "materialization_status is only available during layer materialization",
                is_error=True,
            )
        try:
            from vfx_harness.orchestration.jit_materialization import (
                materialization_candidate_revision,
            )

            payload = json.loads(candidate.read_text(encoding="utf-8"))
            revision = materialization_candidate_revision(candidate)
            units = [
                str(row.get("id"))
                for row in ((payload.get("layer") or {}).get("stages") or [])
                if isinstance(row, dict)
            ]
            contracts = payload.get("scene_contracts") or []
            requirements = payload.get("requirement_bindings") or []
        except (OSError, ValueError, AttributeError, json.JSONDecodeError) as exc:
            return _text(f"materialization status unavailable: {exc}", is_error=True)
        return _text(json.dumps({
            "staged_units": units,
            "unit_count": len(units),
            "scene_contract_count": len(contracts),
            "requirement_binding_count": len(requirements),
            "revision": revision,
            "session_revision_matches": revision == materialization_revision_token,
        }))

    @tool(
        "finalize_materialization",
        "Validate the complete incrementally staged candidate against global authority "
        "and the current consumer view. Call only after every unit and owned requirement "
        "has been staged. Returns VALIDATION PASSED or all remaining JSON-pointer "
        "findings; repair those with patch_materialization.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def finalize_materialization(args):
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "finalize_materialization is only available during layer materialization",
                is_error=True,
            )
        from vfx_harness.orchestration.jit_materialization import (
            attest_materialization_finalization,
            inspect_materialization,
            selected_view_artifact,
        )
        from vfx_harness.orchestration.plan_authority import artifact_path, resolve_current

        try:
            bundle = resolve_current(shot_folder)
            base_layers = selected_view_artifact(
                shot_folder, "layers.json", bundle.content_hash, overlay_root=overlay_root
            ) or artifact_path(shot_folder, "layers.json")
            base_scene_checks = selected_view_artifact(
                shot_folder,
                "scene_checks.json",
                bundle.content_hash,
                overlay_root=overlay_root,
            ) or artifact_path(shot_folder, "scene_checks.json")
            base_requirements = selected_view_artifact(
                shot_folder,
                "requirements.json",
                bundle.content_hash,
                overlay_root=overlay_root,
            ) or artifact_path(shot_folder, "requirements.json")
            findings, _materialized = await anyio.to_thread.run_sync(
                lambda: inspect_materialization(
                    bundle.root,
                    candidate,
                    expected_bundle_hash=bundle.content_hash,
                    base_layers_path=base_layers,
                    base_scene_checks_path=base_scene_checks,
                    resolutions_path=shot_folder / "state" / "plan-resolutions.jsonl",
                    base_requirements_path=base_requirements,
                )
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return _text(str(exc), is_error=True)
        if not findings:
            try:
                attestation = attest_materialization_finalization(
                    candidate, bundle_hash=bundle.content_hash
                )
            except OSError as exc:
                return _text(f"finalization attestation failed: {exc}", is_error=True)
            return _text(
                f"FINALIZATION ATTESTED for {candidate.name} at the current revision "
                f"({attestation.name}). The outer transaction may publish even if this "
                "call consumes the final model turn."
            )
        return _text(
            "VALIDATION FAILED. Remaining findings:\n"
            + "\n".join(f"- {item}" for item in findings),
            is_error=True,
        )

    @tool(
        "patch_materialization",
        "Atomically set one or several RFC 6901 JSON Pointers on the candidate "
        "materialization file, then re-validate once. Group independent findings in "
        "`patches`; use `pointer` + `value` for one repair. Every value is JSON-encoded. "
        "Cannot replace the document root. Returns VALIDATION PASSED or all remaining "
        "findings; if any pointer is invalid, no patch is written.",
        {
            "type": "object",
            "properties": {
                "pointer": {
                    "type": "string",
                    "description": "JSON Pointer such as /scene_contracts/2/owner_layer",
                },
                "value": {
                    "type": "string",
                    "description": "JSON-encoded replacement at that pointer",
                },
                "patches": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "pointer": {"type": "string"},
                            "value": {
                                "type": "string",
                                "description": "JSON-encoded replacement value",
                            },
                        },
                        "required": ["pointer", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "oneOf": [
                {"required": ["pointer", "value"]},
                {"required": ["patches"]},
            ],
            "additionalProperties": False,
        },
    )
    async def patch_materialization(args):
        nonlocal materialization_revision_token
        candidate = Path(candidate_materialization) if candidate_materialization else None
        if candidate is None:
            return _text(
                "patch_materialization is only available during layer materialization",
                is_error=True,
            )
        raw_patches = args.get("patches")
        if raw_patches is None:
            raw_patches = [{"pointer": args.get("pointer"), "value": args.get("value")}]
        patches: list[tuple[str, object]] = []
        try:
            for index, row in enumerate(raw_patches):
                pointer = str((row or {}).get("pointer") or "")
                if not pointer:
                    return _text(f"patches[{index}].pointer is required", is_error=True)
                patches.append((pointer, json.loads(str((row or {}).get("value")))))
        except (TypeError, json.JSONDecodeError) as exc:
            return _text(f"value must be JSON-encoded: {exc}", is_error=True)
        from vfx_harness.orchestration.jit_materialization import (
            apply_materialization_patches,
            materialization_candidate_revision,
            selected_view_artifact,
        )
        from vfx_harness.orchestration.plan_authority import artifact_path, resolve_current

        try:
            bundle = resolve_current(shot_folder)
            base_layers = selected_view_artifact(
                shot_folder, "layers.json", bundle.content_hash, overlay_root=overlay_root
            ) or artifact_path(shot_folder, "layers.json")
            base_scene_checks = selected_view_artifact(
                shot_folder,
                "scene_checks.json",
                bundle.content_hash,
                overlay_root=overlay_root,
            ) or artifact_path(shot_folder, "scene_checks.json")
            base_requirements = selected_view_artifact(
                shot_folder,
                "requirements.json",
                bundle.content_hash,
                overlay_root=overlay_root,
            ) or artifact_path(shot_folder, "requirements.json")
            async with materialization_write_lock:
                findings = await anyio.to_thread.run_sync(
                    lambda: apply_materialization_patches(
                        bundle.root,
                        candidate,
                        patches,
                        expected_bundle_hash=bundle.content_hash,
                        base_layers_path=base_layers,
                        base_scene_checks_path=base_scene_checks,
                        resolutions_path=shot_folder / "state" / "plan-resolutions.jsonl",
                        base_requirements_path=base_requirements,
                        expected_revision=materialization_revision_token,
                    )
                )
                materialization_revision_token = materialization_candidate_revision(candidate)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return _text(str(exc), is_error=True)
        if not findings:
            return _text(f"VALIDATION PASSED for {candidate.name}.")
        detail = "\n".join(f"- {item}" for item in findings)
        return _text(
            f"VALIDATION FAILED for {candidate.name}. Remaining findings:\n{detail}",
            is_error=True,
        )

    # probe_video / contact_sheet / extract_frames were DEFINED and never registered, so
    # they were unreachable on every shot — not just stills-only ones. contact_sheet's own
    # description reads "This is how you do the scene read", and it has never once been
    # callable. Nothing detected that, because an absent tool is indistinguishable from a
    # tool the model chose not to call.
    #
    # Registered conditionally on the shot actually having video: a stills-only shot should
    # not carry three tools whose every call can only fail, and a shot WITH video must not
    # silently lose its scene read. The exclusion is now a decision with a reason instead
    # of an omission.
    video = sorted((shot_folder / "refs").glob("*.mp4")) if (shot_folder / "refs").is_dir() else []
    tools = [measure_ref, measure_check, measure_checks, spike, ask_supervisor,
             evidence_vocabulary, gate_preview, escalate_vocabulary_gap]
    if unit_plan_target is not None:
        tools.append(publish_unit_plan)
    if candidate_materialization is not None:
        tools.extend([
            stage_materialization_unit_tool,
            unstage_materialization_unit_tool,
            materialization_status,
            finalize_materialization,
            patch_materialization,
        ])
    if include_gate:
        tools.append(run_gate)
    if video:
        tools = [probe_video, contact_sheet, extract_frames, *tools]
    if enabled_tools is not None:
        tools = [candidate for candidate in tools if candidate.name in enabled_tools]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=tools)
    names = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    log(
        f"plan tools: {', '.join(t.name for t in tools)}"
        + (f"  ({len(video)} video ref(s))" if video else "  (stills only — video tools not registered)"),
        1,
    )
    return server, names
