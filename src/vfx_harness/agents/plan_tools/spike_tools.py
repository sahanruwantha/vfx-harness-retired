"""Agent SDK tools for the PLAN harness — scene forensics + the spike lab."""

from __future__ import annotations

import subprocess

import anyio
from claude_agent_sdk import tool

from vfx_harness.agents.plan_tools.media import _text
from vfx_harness.agents.plan_tools.spike import (
    _persist_spike_evidence,
    _spike,
    _spike_ineligibility,
    _SpikeBudget,
)
from vfx_harness.blender.tools import _b64, _load, _stats
from vfx_harness.evidence.scene_checks import KIND_DEFINITIONS, validate_row
from vfx_harness.observability.log import log


def register_spike_tools(**closed):

    shot_folder = closed["shot_folder"]
    lab = closed["lab"]
    lab_rel = closed["lab_rel"]
    spikes = closed["spikes"]
    _resolve = closed["_resolve"]
    _keep = closed["_keep"]
    spike_budget = closed["spike_budget"]
    blender = closed["blender"]
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

    return spike
