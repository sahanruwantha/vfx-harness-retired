"""Agent SDK tools over a warm BlenderSession.

`build_blender_tools(session)` returns an in-process MCP server and the list of
qualified tool names to put in `ClaudeAgentOptions.allowed_tools`. Render tools
return image content blocks, so the agent literally sees the frames it makes.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.atomicity import LIVE_WRITE_FAMILY_RULE, script_write_family_evidence
from vfx_harness.domain.image_debts import debts_from_dicts, unpaid_image_contract_debts
from vfx_harness.evidence.checks import load_image_contract_payment_rows
from vfx_harness.observability import run_artifacts


def _run_bpy_write_family_error(source: str, unit_scope: Mapping | None) -> str:
    """Refuse a payload whose typed Blender calls exceed compiled unit authority."""
    if not unit_scope:
        return ""
    clusters = [
        row
        for row in (unit_scope.get("write_clusters") or [])
        if isinstance(row, Mapping) and row.get("instrument_family")
    ]
    if len(clusters) != 1:
        labels = [
            "/".join(str(row.get(key) or "") for key in ("role_namespace", "host_class", "instrument_family"))
            for row in clusters
        ]
        return (
            "BLOCKED: active unit does not have exactly one derived write-cluster; "
            f"found {labels or ['(none)']}. Rematerialize or split the unit before "
            f"mutating Blender. {LIVE_WRITE_FAMILY_RULE}"
        )
    try:
        evidence = script_write_family_evidence(source)
    except SyntaxError as exc:
        return f"BLOCKED: run_bpy payload is not valid Python at line {exc.lineno}: {exc.msg}"
    planned = str(clusters[0]["instrument_family"])
    allowed = {planned}
    if planned == "camera":
        allowed.add("keyframe")
    mutates = unit_scope.get("mutates") or {}
    if isinstance(mutates, Mapping) and mutates.get("dresses"):
        allowed.add("shading")
    illegal = [item for item in evidence if item.family not in allowed]
    if not illegal:
        return ""
    observed = ", ".join(item.label() for item in evidence)
    return (
        "BLOCKED: run_bpy payload exceeds the active unit's derived write family "
        f"{planned!r}; detected {observed}. Allowed families for this unit: "
        + ", ".join(sorted(allowed))
        + ". Split or rematerialize the work; semantic role tags cannot make mixed "
        f"mutation legal. {LIVE_WRITE_FAMILY_RULE}"
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parent_chain_hash(prior_paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in prior_paths:
        resolved = Path(path).resolve()
        digest.update(resolved.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resolved.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _capture_image_artifact(
    *,
    shot_dir: Path,
    source: str | Path,
    frame: int,
    mode: str,
    scale: float,
    resolution: list[int] | tuple[int, ...] | None,
    role: str,
    unit_id: str,
    parent_chain_hash: str,
) -> dict:
    """Copy one plate to immutable run evidence and return a non-path agent handle."""
    src = Path(source)
    sha = _sha256_file(src)
    layout = run_artifacts.ensure(shot_dir, command="build")
    safe_unit = re.sub(r"[^A-Za-z0-9_.-]+", "_", unit_id or "unit")[:60]
    safe_role = re.sub(r"[^A-Za-z0-9_.-]+", "_", role)[:40]
    dest = run_artifacts.renders_dir(shot_dir) / (f"{safe_unit}_{safe_role}_f{int(frame):04d}_{sha[:16]}.png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.is_file():
        shutil.copyfile(src, dest)
    handle = f"image:{safe_role}:f{int(frame)}:{sha[:16]}"
    return {
        "handle": handle,
        "path": dest.relative_to(shot_dir).as_posix(),
        "sha256": sha,
        "run_id": layout.run_id,
        "frame": int(frame),
        "mode": str(mode),
        "scale": float(scale),
        "resolution": list(resolution or []),
        "role": role,
        "unit_id": unit_id,
        "parent_chain_hash": parent_chain_hash,
    }


def _payment_eligible_candidate(rendered: dict) -> bool:
    """Whether a live render can share the fixed v2 adversary settings."""
    return (
        not bool(rendered.get("diagnostic_only"))
        and str(rendered.get("mode")) == "eevee"
        and float(rendered.get("scale", 0.0)) == 0.5
    )


def capture_image_adversaries(
    session: BlenderSession,
    shot_dir: str | Path,
    comparison_state: dict,
    prior_paths: list[Path],
) -> dict[int, dict]:
    """Render the pre-unit chain once; the model never chooses its own adversary."""
    debts = list(comparison_state.get("image_debts") or [])
    if not debts:
        comparison_state["image_adversaries"] = {}
        return {}
    root = Path(shot_dir)
    parent_hash = _parent_chain_hash(prior_paths)
    comparison_state["parent_chain_hash"] = parent_hash
    records: dict[int, dict] = {}
    registry = comparison_state.setdefault("image_artifacts", {})
    for frame in sorted({int(row["frame"]) for row in debts}):
        rendered = session.call("render", frame=frame, mode="eevee", scale=0.5)
        record = _capture_image_artifact(
            shot_dir=root,
            source=rendered["image_path"],
            frame=frame,
            mode="eevee",
            scale=0.5,
            resolution=rendered.get("resolution"),
            role="pre_unit_adversary",
            unit_id=str(comparison_state.get("unit_id") or "unit"),
            parent_chain_hash=parent_hash,
        )
        records[frame] = record
        registry[record["handle"]] = record
    comparison_state["image_adversaries"] = records
    return records


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], **({"is_error": True} if is_error else {})}


def _merge_worklist_items(state: dict, new_items: list[str]) -> dict:
    """Carry unresolved items across a rewritten checklist without stale completions."""
    old_done = set(state.get("done", []))
    unfinished = [item for item in state.get("items", []) if item not in old_done]
    state["items"] = list(dict.fromkeys([*unfinished, *new_items]))
    state["done"] = [item for item in state.get("done", []) if item in state["items"]]
    return state


def _refresh_unpaid_image_debts(comparison_state: dict, shot_dir: str | Path | None) -> list[dict]:
    """Recompute unpaid image-contract debts from disk after propose_checks / mutation."""

    cards = debts_from_dicts(comparison_state.get("image_debts"))
    if not cards or not shot_dir:
        comparison_state["unpaid_image_debts"] = []
        return []
    unpaid = [card.as_dict() for card in unpaid_image_contract_debts(cards, load_image_contract_payment_rows(shot_dir))]
    comparison_state["unpaid_image_debts"] = unpaid
    return unpaid
