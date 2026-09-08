"""Confined spike construction followed by independent fresh-process measurement."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from vfx_harness.blender import artifact_execution, filesystem_confinement, resolution
from vfx_harness.evidence import scene_checks
from vfx_harness.observability.run_artifacts import RunLayout
from vfx_harness.orchestration.plan_bundle_integrity import digest, read_real_file

MAX_OUTPUT_BYTES = 8 * 1024 * 1024


def _bootstrap() -> str:
    root = filesystem_confinement.HARNESS_RUNTIME_ROOT.parent
    return f'''import sys, resource
sys.path.insert(0, {str(root)!r})
sys.path.insert(0, {str(root / "vfx_harness" / "blender")!r})
from vfx_harness.blender.process_confinement import install_worker_seccomp
resource.setrlimit(resource.RLIMIT_FSIZE, ({MAX_OUTPUT_BYTES}, {MAX_OUTPUT_BYTES}))
install_worker_seccomp()
import bpy
'''



def _one_shot_program(source: str) -> str:
    # Blender's audio teardown tries denied socket syscalls even with -noaudio.
    # Finish this one-shot process explicitly after the trusted program closes its
    # artifacts, preserving a nonzero exit for every exception.
    return f"""import os, sys, traceback
exit_process = os._exit
try:
    exec({source!r}, {{}})
except BaseException as error:
    for frame in traceback.extract_tb(error.__traceback__):
        print(f"{{frame.filename}}:{{frame.lineno}}: {{frame.name}}", file=sys.stderr)
    print(f"{{type(error).__name__}}: {{error}}", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    exit_process(1)
sys.stdout.flush()
sys.stderr.flush()
exit_process(0)
"""


def execute_spike(
    *, layout: RunLayout, directory: Path, script: str, contracts: list[dict],
    render_frame: int | None, timeout: int, blender: str, check_current: Callable[[], None],
) -> dict:
    """No script-generated stdout is interpreted as measurement or acceptance."""
    check_current()
    artifact_execution.validate_artifact_source(script)
    inputs, outputs = directory / "inputs", directory / "outputs"
    inputs.mkdir(parents=True, exist_ok=False)
    outputs.mkdir()
    source = inputs / "candidate.py"
    source.write_text(script, encoding="utf-8")
    blend = outputs / "candidate.blend"
    render = outputs / "render.png"
    readings = outputs / "readings.json"
    build = inputs / "construct.py"
    build.write_text(_bootstrap() + f'''
from vfx_harness.blender.artifact_execution import artifact_builtins, validate_artifact_source
source = open({str(source)!r}, encoding="utf-8").read()
validate_artifact_source(source)
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
exec(source, {{"__builtins__": artifact_builtins()}})
bpy.ops.wm.save_as_mainfile(filepath={str(blend)!r}, check_existing=False)
''', encoding="utf-8")
    probes = []
    for row in contracts:
        frame = row.get("frame")
        if frame is None:
            frame = (row.get("frames") or [sample["frame"] for sample in row.get("samples", [])] or [1])[0]
        probes.append(scene_checks._blender_probe([row], int(frame)))
    evaluator = inputs / "evaluate.py"
    evaluator.write_text(_bootstrap() + f'''
import json
readings = []
for source in {probes!r}:
    namespace = {{}}
    exec(source, namespace)
    readings.append(namespace["RESULT"][0])
with open({str(readings)!r}, "w", encoding="utf-8") as output:
    json.dump(readings, output, sort_keys=True, allow_nan=False)
''' + (f'''
scene = bpy.context.scene
scene.frame_set({render_frame!r})
scene.render.resolution_x, scene.render.resolution_y = 960, 540
scene.render.resolution_percentage = 100
engines = {{item.identifier for item in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}}
scene.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_EEVEE"
scene.render.filepath = {str(render)!r}
scene.render.image_settings.file_format = "PNG"
bpy.ops.render.render(write_still=True)
''' if render_frame is not None else ""), encoding="utf-8")
    for program in (build, evaluator):
        program.write_text(_one_shot_program(program.read_text(encoding="utf-8")), encoding="utf-8")
    executable = resolution.resolve_blender(blender, shot_bound=True)
    started = time.monotonic()
    result = {"executed": True, "status": "execution_failed", "results": [], "passed": False}

    def outcome(**fields):
        logs = {path.name: read_real_file(layout.shot, path, "spike diagnostic log").decode(
            "utf-8", errors="replace")[-2000:] for path in sorted(outputs.glob("*.log"))}
        return {**result, **fields, "diagnostics": logs}

    for phase, program, scene in (("construction", build, None), ("evaluation", evaluator, blend)):
        check_current()
        argv = [executable, "--background", "--factory-startup", "-noaudio", "--disable-autoexec"]
        if scene is not None:
            argv.append(str(scene))
        argv.extend(["--python-exit-code", "1", "--python", str(program)])
        log = outputs / f"{phase}.log"
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return outcome(status="timeout", phase=phase)
        with filesystem_confinement.prepared_worker_command(
            argv, writable_roots=(outputs,), readable_roots=(inputs,),
            authority_root=layout.shot, current_run_root=layout.root,
        ) as command, log.open("wb") as output:
            try:
                completed = subprocess.run(command.argv, pass_fds=command.pass_fds,
                                           stdout=output, stderr=subprocess.STDOUT, timeout=remaining, check=False)
            except subprocess.TimeoutExpired:
                return outcome(status="timeout", phase=phase)
        check_current()
        if completed.returncode != 0:
            return outcome(phase=phase, exit_code=completed.returncode)
    observations = json.loads(read_real_file(layout.shot, readings, "independent spike readings"))
    if not isinstance(observations, list) or len(observations) != len(contracts):
        raise ValueError("independent spike readings do not match the complete requested contract set")
    results = []
    for row, observation in zip(contracts, observations, strict=True):
        error = observation.get("error")
        value = observation.get("value")
        results.append({"id": row["id"], "value": value, "error": error,
                        "pass": not error and scene_checks._holds(row, value)})
    return outcome(status="measured", results=results,
                   passed=bool(results) and all(row["pass"] for row in results),
                   script_sha256=digest(script.encode()), blender_executable=executable,
                   wall_seconds=time.monotonic() - started)
