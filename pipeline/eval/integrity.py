"""Does the record match the disk?

The ledger is the pipeline's memory. Everything downstream — chaining, the final mp4,
acceptance — trusts it to name things that exist. Three failures in the review's risk
list are all the same shape: "acceptance can silently judge a partial chain when scripts
are missing", "final rendering can execute orphan numeric scripts not in the accepted
ledger", "unpassed prior layers are chained anyway". Each was a claim in shot.json that
the filesystem did not support.

This is the cheapest check in the harness — no Blender, no model, milliseconds — and it
is the one most likely to catch a real problem, because the ledger and the build
directory drift apart every time a layer is re-run, renamed or interrupted.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..brief import Shot
from ..ledger import load_layers
from .determinism import Result


def _problems(shot: Shot) -> tuple[list[str], list[str], dict]:
    """-> (errors, warnings, data). Errors break a downstream stage; warnings are
    inconsistencies that currently happen to be harmless."""
    errors: list[str] = []
    warnings: list[str] = []
    data: dict = {}

    ledger_path = shot.folder / "shot.json"
    if not ledger_path.is_file():
        return [f"no shot.json in {shot.folder} — nothing has been recorded"], [], data
    try:
        record = json.loads(ledger_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"shot.json is unreadable ({e}) — every downstream stage that reads it "
                f"either crashes or, worse, treats the shot as having no layers"], [], data

    slots = record.get("milestones", {})
    try:
        layers = load_layers(shot)
    except Exception as e:      # noqa: BLE001 — a missing plan is itself the finding
        layers = {}
        errors.append(f"layers.json unusable ({str(e)[:120]}) — the accepted chain is "
                      f"defined by it, so nothing below can be verified against a plan")

    # 1. every layer the ledger says PASSED has a script on disk ------------------
    passed = [lid for lid, s in slots.items() if s.get("status") == "passed"]
    data["passed_layers"] = sorted(passed)
    for lid in sorted(passed):
        plan_layer = layers.get(lid)
        if plan_layer is None:
            errors.append(f"layer {lid} is recorded 'passed' but appears in no plan layer "
                          f"— _chain_scripts builds the deliverable from layers.json, so "
                          f"this layer's work is NOT in the mp4")
            continue
        script = shot.folder / plan_layer.script
        if not script.is_file():
            errors.append(f"layer {lid} is 'passed' but its plan script "
                          f"{plan_layer.script} does not exist — the chain cannot be "
                          f"replayed and acceptance would judge a partial build")
        # The ledger writes its OWN idea of the script name in Ledger.begin()
        # (build/<id>.py). Chaining ignores it and uses layers.json, so this is not
        # currently load-bearing — but it is the field a reader takes for "the accepted
        # script", and it points at a file that was never written.
        recorded = slots[lid].get("script")
        if recorded and recorded != plan_layer.script:
            exists = (shot.folder / recorded).is_file()
            warnings.append(
                f"layer {lid}: shot.json records script {recorded!r}"
                f"{'' if exists else ' (which does not exist)'} while layers.json says "
                f"{plan_layer.script!r} — the chain follows layers.json, so the ledger's "
                f"own field is misinformation")

    # 2. every judged render referenced in shot.json exists ----------------------
    missing_renders, checked = [], 0
    def _want(rel: str | None, where: str) -> None:
        nonlocal checked
        if not rel:
            return
        checked += 1
        if not (shot.folder / rel).is_file():
            missing_renders.append(f"{where} → {rel}")

    for lid, slot in slots.items():
        for r in slot.get("rounds", []):
            _want(r.get("render"), f"layer {lid} round {r.get('round')} ({r.get('kind')})")
        _want((slot.get("best") or {}).get("render"), f"layer {lid} best")
    for mid, r in ((record.get("acceptance") or {}).get("moments") or {}).items():
        _want(r.get("render"), f"acceptance {mid}")
    data["renders_referenced"] = checked
    data["renders_missing"] = missing_renders
    if missing_renders:
        errors.append(
            f"{len(missing_renders)} of {checked} judged render(s) referenced by "
            f"shot.json are gone — the evidence behind those verdicts cannot be "
            f"re-examined:\n        " + "\n        ".join(missing_renders[:10]))

    # 3. orphan scripts in build/ ------------------------------------------------
    build_dir = shot.folder / "build"
    named = {Path(L.script).name for L in layers.values()}
    orphans = ([p.name for p in sorted(build_dir.glob("[0-9]*.py")) if p.name not in named]
               if build_dir.is_dir() else [])
    data["orphan_scripts"] = orphans
    if orphans:
        # render_shot._chain_scripts already refuses to include these, and
        # _prior_layer_paths skips them with a warning. The risk is the human one: an
        # orphan looks exactly like an accepted layer in a directory listing.
        warnings.append(
            f"build/ holds {len(orphans)} script(s) matching no plan layer: "
            f"{', '.join(orphans)} — they are excluded from the chain, so they are dead "
            f"weight that reads as work")

    # 4. a 'passed' layer with no recorded verdict -------------------------------
    for lid in sorted(passed):
        if not slots[lid].get("rounds"):
            errors.append(f"layer {lid} is 'passed' with zero recorded critic rounds — "
                          f"nothing says what it was judged on")

    # 5. plan artifacts still match the brief they were derived from -------------
    try:
        from ..provenance import check as provenance_check
        prov = provenance_check(shot.folder)
    except Exception as e:      # noqa: BLE001
        prov = [f"provenance check unavailable: {str(e)[:100]}"]
    data["provenance"] = prov
    warnings.extend(prov)

    return errors, warnings, data


def artifact_integrity(shot: Shot) -> Result:
    errors, warnings, data = _problems(shot)
    data["errors"], data["warnings"] = errors, warnings
    if errors:
        detail = "\n      ".join(errors)
        if warnings:
            detail += "\n      " + "\n      ".join(f"(warn) {w}" for w in warnings)
        return Result("artifact integrity", ok=False, detail=detail, data=data)
    if warnings:
        # Warnings do not fail the check, but they are printed in full. Truncating them
        # is how "the ledger names a script that does not exist" stayed invisible.
        return Result("artifact integrity", ok=True,
                      detail=("no errors; " + str(len(warnings)) + " inconsistenc"
                              + ("y" if len(warnings) == 1 else "ies") + ":\n      "
                              + "\n      ".join(f"(warn) {w}" for w in warnings)),
                      data=data)
    return Result("artifact integrity", ok=True,
                  detail=f"{len(data.get('passed_layers', []))} passed layer(s), "
                         f"{data.get('renders_referenced', 0)} render reference(s), "
                         f"no orphan scripts", data=data)
