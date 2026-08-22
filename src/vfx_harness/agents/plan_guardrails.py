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
    "requirements.json",
    "obligations.json",
    "assumptions.json",
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
        if name == "requirements.json":
            from vfx_harness.domain.plan_records import load_requirements

            load_requirements(root)
            return []
        if name == "obligations.json":
            from vfx_harness.domain.plan_records import load_obligations

            load_obligations(root)
            return []
        if name == "assumptions.json":
            from vfx_harness.domain.plan_records import load_assumptions

            load_assumptions(root)
            return []
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


def planner_path_scope(
    shot_folder: str | Path,
    *,
    readable_files: tuple[str | Path, ...] = (),
    readable_roots: tuple[str | Path, ...] = (),
    writable_files: tuple[str | Path, ...] | None = None,
    strict_reads: bool = False,
) -> HookMatcher:
    """Confine discovery to staging, with exact read-only evidence exceptions.

    A repair may read the immutable snapshot it was assigned, but the snapshot's absolute
    path must not become a route back to the shot root or neighbouring runs. Mutation never
    crosses the workspace boundary.
    """
    root = Path(shot_folder).resolve()
    read_exceptions = {Path(path).expanduser().resolve() for path in readable_files}
    read_root_exceptions = {Path(path).expanduser().resolve() for path in readable_roots}
    write_exceptions = (
        None
        if writable_files is None
        else {Path(path).expanduser().resolve() for path in writable_files}
    )

    async def _check(inp, tool_use_id, ctx):
        tool = inp.get("tool_name", "") if isinstance(inp, dict) else getattr(inp, "tool_name", "")
        args = (inp.get("tool_input") if isinstance(inp, dict) else getattr(inp, "tool_input", {})) or {}
        path_key = {
            "Read": "file_path",
            "Write": "file_path",
            "Edit": "file_path",
            "NotebookEdit": "notebook_path",
            "Grep": "path",
            "LSP": "path",
            "Glob": "path",
        }.get(tool)
        if path_key is None:
            return {}
        raw = args.get(path_key)
        if not raw:
            target = root
        else:
            target = Path(str(raw)).expanduser()
            target = (target if target.is_absolute() else root / target).resolve()

        # Glob carries its effective path in ``pattern`` when ``path`` is omitted. Reject
        # absolute/traversing patterns rather than relying on the tool's undocumented cwd
        # handling to preserve the boundary.
        if tool == "Glob":
            pattern = str(args.get("pattern") or "")
            pattern_path = Path(pattern).expanduser()
            if pattern_path.is_absolute() or ".." in pattern_path.parts:
                return _path_denial(tool, pattern, root)

        inside = target == root or root in target.parents
        read_only = tool in {"Read", "Grep", "LSP", "Glob"}
        authored = (
            target
            in {
                root / "brief.md",
                root / ".plan-workspace.json",
                root / "plan_amendments.jsonl",
                root / "state/plan-resolutions.jsonl",
            }
            or target == root / "refs"
            or root / "refs" in target.parents
        )
        write_tool = tool in {"Write", "Edit", "NotebookEdit"}
        bundle_member = write_tool and any(
            target.is_relative_to(parent)
            for parent in root.glob("runs/*/checkpoints/plans/bundles/*")
        )
        exact_write_denied = (
            write_tool and write_exceptions is not None and target not in write_exceptions
        )
        if strict_reads and read_only:
            declared_read = (
                authored
                or target in read_exceptions
                or (write_exceptions is not None and target in write_exceptions)
                or any(target == parent or parent in target.parents for parent in read_root_exceptions)
            )
            if declared_read:
                return {}
            return _path_denial(tool, str(target), root)
        if inside and not (write_tool and (authored or bundle_member or exact_write_denied)):
            return {}
        if read_only and target in read_exceptions:
            return {}
        return _path_denial(tool, str(target), root)

    return HookMatcher(matcher=None, hooks=[_check])


def _path_denial(tool: str, target: str, root: Path) -> dict:
    log(f"! planner path denied: {tool} on {target}", 1)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"Fresh planning is confined to this run's workspace: {root}. The requested "
            f"{tool} path ({target}) could expose or mutate shot-root state, prior plans, "
            "or another run. Use the staged brief, refs, and current candidate only. A "
            "repair may Read only the exact immutable snapshot named in its assignment."
        ),
    }}


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


def planner_hooks(
    shot_folder: str | Path,
    *,
    readable_files: tuple[str | Path, ...] = (),
    readable_roots: tuple[str | Path, ...] = (),
    writable_files: tuple[str | Path, ...] | None = None,
    strict_reads: bool = False,
    completion_gate: bool = True,
) -> dict:
    hooks = {
        "PreToolUse": [planner_path_scope(
            shot_folder,
            readable_files=readable_files,
            readable_roots=readable_roots,
            writable_files=writable_files,
            strict_reads=strict_reads,
        )],
        "PostToolUse": [planner_artifact_feedback(shot_folder)],
    }
    if completion_gate:
        hooks["Stop"] = [planner_completion_gate(shot_folder)]
    return hooks
