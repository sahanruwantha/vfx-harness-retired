"""What a plan was derived from, and whether it still matches.

Plan artifacts (`plans/global.md`, strict work-unit plans, and the machine contracts) carry no record
of the brief they came from. Two failures follow. Editing brief.md leaves a stale plan
with nothing saying it no longer matches its source — and this pipeline's whole contract
is that the plan encodes the brief, so a silent divergence there mis-specifies every
layer beneath it. And a crash mid-write can leave a truncated artifact that the next
stage parses as valid but empty.

The plan agent writes those files itself through the SDK's Write tool, so we cannot make
its writes atomic from here. What we can do is record what the inputs hashed to when the
plan was accepted, and refuse to build on a plan whose inputs have since moved.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from vfx_harness.domain.work_units import WorkUnit, read_document

STAMP = "plan.provenance.json"

# Everything the plan is a function of. A change to any of these invalidates it.
INPUTS = ("brief.md",)
CORE_ARTIFACTS = (
    "plans/global.md", "layers.json", "acceptance.json", "critic_axes.json",
    "checks.json", "scene_checks.json",
)
# ``runtime_checks.json`` is intentionally absent: it is append-only build evidence, not
# planner output.  Hashing it as a plan artifact made every valid builder addition report
# the plan as stale.


def _artifact_names(folder: Path) -> tuple[str, ...]:
    """Return the strict plan surface; legacy plan.md is intentionally ignored."""

    names = list(CORE_ARTIFACTS)
    try:
        layers = read_document(folder / "layers.json")
    except (OSError, ValueError):
        layers = []
    for layer_index, row in enumerate(layers):
        for unit_index, raw_unit in enumerate(row.get("stages") or []):
            try:
                unit = WorkUnit.parse(
                    raw_unit,
                    f"layers.json.layers[{layer_index}].stages[{unit_index}]",
                )
            except ValueError:
                continue
            if (folder / unit.plan).is_file():
                names.append(unit.plan)
            for claim in unit.evaluation.claims:
                artifact = (claim.qualification or {}).get("artifact")
                if artifact and (folder / artifact).is_file():
                    names.append(artifact)
    if (folder / "plan_amendments.jsonl").is_file():
        names.append("plan_amendments.jsonl")
    return tuple(dict.fromkeys(names))


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def atomic_write(path: Path, text: str) -> None:
    """Publish by rename — a reader sees the old file or the new one, never a half one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def stamp(folder: str | Path, *, model: str = "", note: str = "") -> Path:
    """Record what the plan was derived from. Called once the plan is accepted."""
    folder = Path(folder)
    rec = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "note": note,
        "inputs": {n: _digest(folder / n) for n in INPUTS},
        "artifacts": {n: _digest(folder / n) for n in _artifact_names(folder)},
    }
    out = folder / STAMP
    atomic_write(out, json.dumps(rec, indent=2) + "\n")
    return out


def check(folder: str | Path) -> list[str]:
    """Problems with the plan as it stands. Empty means the plan matches its inputs."""
    folder = Path(folder)
    stamp_path = folder / STAMP
    if not stamp_path.is_file():
        return [f"no {STAMP} — this plan predates provenance tracking, so nothing "
                f"records which brief it was derived from"]
    try:
        rec = json.loads(stamp_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"{STAMP} is unreadable ({e}) — cannot verify the plan matches the brief"]

    problems = []
    for name, was in (rec.get("inputs") or {}).items():
        now = _digest(folder / name)
        if now is None:
            problems.append(f"{name} is missing, but the plan was derived from it")
        elif was and now != was:
            problems.append(
                f"{name} has CHANGED since the plan was written ({was} → {now}) — the "
                f"plan no longer describes this brief")
    for name, was in (rec.get("artifacts") or {}).items():
        now = _digest(folder / name)
        if was and now is None:
            problems.append(f"{name} was part of the plan and is now missing")
        elif was and now != was:
            problems.append(f"{name} was edited after planning ({was} → {now})")
    return problems
