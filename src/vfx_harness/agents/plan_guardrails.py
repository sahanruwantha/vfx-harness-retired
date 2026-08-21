"""Warm-session validation for planner-authored machine artifacts.

The deterministic plan gate remains the cross-artifact authority. These hooks run the
strict, file-local portions immediately after a Write/Edit so malformed JSON, an invalid
work-unit DAG, or an unsupported check kind is returned while the drafting context is
still alive instead of becoming a paid cold repair round later.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_agent_sdk import HookMatcher

from vfx_harness.domain.brief import load_shot
from vfx_harness.domain.contracts import load_document
from vfx_harness.observability.log import log

_MACHINE_ARTIFACTS = {
    "layers.json",
    "acceptance.json",
    "critic_axes.json",
    "checks.json",
    "scene_checks.json",
}


def _acceptance_errors(folder: Path) -> list[str]:
    path = folder / "acceptance.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"acceptance.json is unreadable: {exc}"]
    if not isinstance(rows, list) or not rows:
        return ["acceptance.json must be a non-empty list"]
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        where = f"acceptance.json[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{where} must be an object")
            continue
        rid = str(row.get("id") or "")
        if not rid:
            errors.append(f"{where}.id is required")
        elif rid in seen:
            errors.append(f"{where}.id duplicates {rid!r}")
        seen.add(rid)
        frame = row.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 1:
            errors.append(f"{where}.frame must be a positive integer")
        ref = row.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            errors.append(f"{where}.ref must be a non-empty relative path")
        elif not (folder / ref).is_file():
            errors.append(f"{where}.ref does not exist: {ref}")
        if not str(row.get("reads") or "").strip():
            errors.append(f"{where}.reads is required")
        fingerprint = row.get("fingerprint")
        if not isinstance(fingerprint, (str, dict)) or not fingerprint:
            errors.append(f"{where}.fingerprint must be structured metrics or legacy prose")
        elif isinstance(fingerprint, dict) and isinstance(ref, str) and (folder / ref).is_file():
            from vfx_harness.evaluation.grounding import check_structured_fingerprint

            claims, fingerprint_errors = check_structured_fingerprint(fingerprint, folder / ref)
            errors.extend(f"{where}.fingerprint: {error}" for error in fingerprint_errors)
            errors.extend(
                f"{where}.fingerprint metric {claim['key']} is {claim['verdict']}"
                for claim in claims
                if claim["verdict"] != "ok"
            )
        elif isinstance(fingerprint, str) and isinstance(ref, str) and (folder / ref).is_file():
            from vfx_harness.evaluation.grounding import check_fingerprint, measure

            claims, _ = check_fingerprint(fingerprint, measure(folder / ref))
            errors.extend(
                f"{where}.fingerprint metric {claim['key']} is {claim['verdict']}"
                for claim in claims
                if claim["verdict"] != "ok"
            )
    return errors


def validate_planner_artifact(folder: str | Path, name: str) -> list[str]:
    """Return every file-local validation error for one planner artifact."""
    root = Path(folder)
    path = root / name
    try:
        if name == "layers.json":
            from vfx_harness.evaluation.plan_gate import _check_unit_dependencies
            from vfx_harness.orchestration.ledger import load_layers

            dependency_errors = [
                f"{finding.where}: {finding.what}" for finding in _check_unit_dependencies(root)
            ]
            if dependency_errors:
                return dependency_errors
            load_layers(load_shot(root))
            return []
        if name == "scene_checks.json":
            from vfx_harness.evidence.scene_checks import validate_row

            rows = load_document(path, "contracts")
            errors = []
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    errors.append(f"scene_checks.json[{index}] must be an object")
                    continue
                error = validate_row(row)
                if error:
                    errors.append(f"scene_checks.json[{index}] {row.get('id', '<missing>')}: {error}")
            return errors
        if name == "checks.json":
            from vfx_harness.evidence.checks import load

            load(path)
            return []
        if name == "acceptance.json":
            return _acceptance_errors(root)
        if name == "critic_axes.json":
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list) or not rows:
                return ["critic_axes.json must be a non-empty list"]
            errors, seen = [], set()
            for index, row in enumerate(rows):
                if (
                    not isinstance(row, dict)
                    or not str(row.get("key") or "").strip()
                    or not str(row.get("desc") or "").strip()
                ):
                    errors.append(f"critic_axes.json[{index}] requires key and desc")
                    continue
                if row["key"] in seen:
                    errors.append(f"critic_axes.json[{index}] duplicates key {row['key']!r}")
                seen.add(row["key"])
            return errors
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return [f"{name}: {exc}"]
    return []


def planner_artifact_feedback(shot_folder: str | Path) -> HookMatcher:
    root = Path(shot_folder).resolve()

    async def _after(inp, tool_use_id, ctx):
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        if tool not in {"Write", "Edit"}:
            return {}
        args = (inp.get("tool_input") if isinstance(inp, dict) else getattr(inp, "tool_input", {})) or {}
        target = Path(str(args.get("file_path") or ""))
        if not target.is_absolute():
            target = root / target
        try:
            rel = target.resolve().relative_to(root)
        except ValueError:
            return {}
        if len(rel.parts) != 1 or rel.name not in _MACHINE_ARTIFACTS or not target.is_file():
            return {}
        errors = validate_planner_artifact(root, rel.name)
        if not errors:
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"WRITE-TIME VALIDATION PASSED for {rel.name}.",
            }}
        detail = "\n".join(f"- {item}" for item in errors[:20])
        log(f"! write-time validation: {rel.name} has {len(errors)} error(s)", 1)
        return {"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"WRITE-TIME VALIDATION FAILED for {rel.name}. Fix it now while this "
                f"context is warm; do not wait for the terminal gate.\n{detail}"
            ),
        }}

    return HookMatcher(matcher=None, hooks=[_after])


def planner_completion_gate(shot_folder: str | Path) -> HookMatcher:
    root = Path(shot_folder)

    async def _check(inp, tool_use_id, ctx):
        errors: list[str] = []
        global_plan = root / "plans" / "global.md"
        if not global_plan.is_file() or global_plan.stat().st_size < 200:
            errors.append("plans/global.md is missing or too small")
        for name in sorted(_MACHINE_ARTIFACTS):
            if not (root / name).is_file():
                errors.append(f"{name} is missing")
            else:
                errors.extend(validate_planner_artifact(root, name))
        if not errors:
            return {}
        detail = "\n".join(f"- {item}" for item in errors[:25])
        return {
            "decision": "block",
            "reason": (
                "Planner machine artifacts are not locally valid. Repair these before "
                f"finishing; the cross-artifact plan gate runs after the session.\n{detail}"
            ),
        }

    return HookMatcher(matcher=None, hooks=[_check])


def planner_hooks(shot_folder: str | Path) -> dict:
    return {
        "PostToolUse": [planner_artifact_feedback(shot_folder)],
        "Stop": [planner_completion_gate(shot_folder)],
    }
