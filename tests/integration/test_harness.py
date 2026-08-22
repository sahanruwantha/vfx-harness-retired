"""Deterministic self-test for the harness. No models, no cost.

Written because an external review pointed out the README claimed a test suite that did
not exist, and because three defects shipped today were caught only by ad-hoc checks —
including one in a fix whose own test covered the failure I imagined rather than the one
I had watched happen.

    .venv/bin/python -m tests.integration.test_harness
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import anyio

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "src"))
FAILS: list[str] = []


def check(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    from vfx_harness.agents.builder import (
        _ablation_frames,
        _apply_evidence_gate,
        _audit_panel_citations,
        _axes_need_motion,
        _builder_options,
        _builder_ticket_context,
        _critic_options,
        _critic_schema,
        _evidence_convergence_stop,
        _filter_critic_issues,
        _focus_references,
        _focus_requests,
        _image_reproduction,
        _layer_motion_frames,
        _needs_critic_panel,
        _owned_axes,
        _plan_layer_excerpt,
        _repair_action,
        _repair_change_summary,
        _required_focus_requests,
        _retry_warm_start,
        _round_rank,
        _script_options,
        _verdict,
        _worklist_evidence,
    )
    from vfx_harness.agents.guardrails import api_guardrails, web_allowlist
    from vfx_harness.agents.shot_context import clear_layer_context, write_layer_context
    from vfx_harness.application.run_shot import _can_advance
    from vfx_harness.blender.tools import (
        _check_args_error,
        _comparison_lock_error,
        _comparison_mode_scale,
        _layer_feedback_policy,
        _merge_worklist_items,
        _metrics_line,
        _pixel_contract_gate,
        _scene_completion_state,
        _stats,
        build_blender_tools,
    )
    from vfx_harness.domain.brief import load_shot
    from vfx_harness.evidence.checks import _metric_regions
    from vfx_harness.evidence.metrics import compare, look_pair, look_vector
    from vfx_harness.infrastructure.sandbox import _relocate, path_sandbox
    from vfx_harness.knowledge.recipes import _all, recipe_index, search_recipes
    from vfx_harness.observability import run_artifacts as _ra
    from vfx_harness.orchestration.escalate import answer, answers_block, ask, unanswered_for_layer
    from vfx_harness.orchestration.escalate import load as lq
    from vfx_harness.orchestration.ledger import (
        Ledger,
        Milestone,
        load_axes,
        load_layers,
        load_milestones,
        plan_strips,
    )
    from vfx_harness.orchestration.script_map import find_lines, outline

    shot = load_shot("shots/barrel_roll")
    layers, axes, moments = load_layers(shot), load_axes(shot), load_milestones(shot)

    print("\n[whole-shot dry run]")
    check("dry-run advances past an unchanged pending verdict", _can_advance("pending", dry_run=True))
    check("a real run still stops on a pending verdict", not _can_advance("pending", dry_run=False))
    check("a real run advances only after a pass", _can_advance("passed", dry_run=False))
    check(
        "a judge conflict blocks downstream layers without pretending to pass",
        not _can_advance("judge_conflict", dry_run=False),
    )

    print("\n[plan artifacts]")
    # Count derived, not hardcoded: inserting the lighting stage renumbered 5..8 into
    # 6..9 and these read as three failures of the PLAN rather than of the assertion.
    # What matters is that ids are dense and 1-based, which is what chaining relies on.
    check("layers load", len(layers) >= 8, f"{len(layers)}")
    check("ids are 1..N", [l.id for l in layers.values()] == [str(i) for i in range(1, len(layers) + 1)])
    check("script prefix matches id", all(Path(l.script).name.startswith(f"{int(l.id):02d}_") for l in layers.values()))
    check("every layer multi-frame aware", all(len(l.judges) >= 1 for l in layers.values()))
    check("every judge ref exists", all((shot.folder / r).is_file() for l in layers.values() for _f, r in l.judges))
    _strict_shot = load_shot("shots/beacon_wake")
    _strict_layers = load_layers(_strict_shot)
    check(
        "strict per-layer plans resolve for the migrated shot",
        all(len(_plan_layer_excerpt(_strict_shot, l)) > 200 for l in _strict_layers.values()),
    )
    check(
        "strict shot exposes no legacy monolithic plan to builder retrieval",
        not (_strict_shot.folder / "plan.md").exists(),
    )
    _ctx_path = write_layer_context(_strict_shot, _strict_layers["1"], load_axes(_strict_shot), {})
    _ctx = _ctx_path.read_text()
    check(
        "durable context names one authoritative layer plan and LIVE_BUILD mode",
        "MODE is `LIVE_BUILD`" in _ctx and "`plans/01_layout_set.md`" in _ctx and "never be read" in _ctx,
    )
    check(
        "durable live context never asks the builder to publish a script",
        "Write the delta script at the end" not in _ctx and "Do not call `Write` or `Edit`" in _ctx,
    )
    clear_layer_context(_strict_shot)
    from vfx_harness.evaluation.plan_gate import _check_hierarchical_plans

    _legacy_root = Path(tempfile.mkdtemp())
    (_legacy_root / "plan.md").write_text("legacy\n")
    _legacy_findings, _ = _check_hierarchical_plans(_legacy_root)
    check(
        "plan gate blocks a discoverable legacy plan instead of warning",
        any(f.blocking and f.where == "plan.md" for f in _legacy_findings),
    )
    _legacy_plan = _plan_layer_excerpt
    try:
        _legacy_plan(shot, layers["1"])
        _legacy_rejected = False
    except FileNotFoundError:
        _legacy_rejected = True
    check("a monolithic-plan-only shot is rejected rather than backfilled", _legacy_rejected)
    check("acceptance moments load", len(moments) == 10, f"{len(moments)}")
    check("layers inherit plan strips", len(layers["1"].as_milestone(plan_strips(shot)).strip) > 0)
    owned = {a for l in layers.values() for a in l.owns}
    check("every axis owned", {k for k, _ in axes} == owned)
    # Scope prose names other layers by NUMBER ("the finish grade (layer 8)") so the critic
    # knows what a later stage delivers. Inserting the lighting stage renumbered 5..8 into
    # 6..9 and left layer 3 telling the critic that the grade is layer 8 — which is now
    # rebirth. Nothing caught it; the strings just went stale and stayed plausible.
    import re as _re

    bad = []
    for l in layers.values():
        for m in _re.finditer(r"layers? (\d+)(?:\s*[/&,]\s*(\d+))?", l.reads):
            for g in m.groups():
                if g and g not in layers:
                    bad.append(f"layer {l.id} cites layer {g}, which does not exist")
    check("scope prose cites layers that exist", not bad, "; ".join(bad[:3]))
    # A layer must not describe ITSELF as the stage that delivers something later.
    self_ref = [l.id for l in layers.values() if _re.search(rf"layers? {l.id}\b", l.reads)]
    check("no layer cites itself as a later stage", not self_ref, str(self_ref))
    check("no mute layer", all(l.owns for l in layers.values()))

    print("\n[verdict + tolerance]")
    check("single-axis pass needs min>=3", _verdict({"scores": {"a": 3}})["pass"])
    check("single-axis 2 fails", not _verdict({"scores": {"a": 2}})["pass"])
    check(
        "a perfect single-axis score does not buy a redundant second opinion",
        not _needs_critic_panel(_verdict({"scores": {"a": 4}})),
    )
    check(
        "a single-axis score on the 2/3 boundary still gets adjudicated",
        _needs_critic_panel(_verdict({"scores": {"a": 3}})),
    )
    check("n/a excluded from mean", _verdict({"scores": {"a": 4, "b": "n/a"}})["mean"] == 4.0)
    _all_axes = [("camera", "frame"), ("sky", "cloud"), ("grade", "finish")]
    _camera_layer = type("Layer", (), {"owns": ("camera",)})()
    check(
        "layer ownership filters axes before prompting", _owned_axes(_all_axes, _camera_layer) == [("camera", "frame")]
    )
    _scoped_score = _critic_schema([("camera", "frame")], allow_na=False)["properties"]["scores"]["properties"][
        "camera"
    ]
    _full_score = _critic_schema([("camera", "frame")], allow_na=True)["properties"]["scores"]["properties"]["camera"]
    check(
        "scoped critic schema requires a numeric score",
        _scoped_score.get("type") == "integer" and "anyOf" not in _scoped_score,
    )
    check("full-rubric schema can still mark beat-specific axes n/a", "anyOf" in _full_score)
    _semantic_ratio = _metric_regions(
        "region_ratio",
        {"orb_high": (0.4, 0.2, 0.6, 0.4), "orb_low": (0.4, 0.6, 0.6, 0.8)},
    )
    check(
        "region ratios accept semantic numerator/denominator names",
        _semantic_ratio["a"] == _semantic_ratio["orb_high"] and _semantic_ratio["b"] == _semantic_ratio["orb_low"],
    )
    check(
        "critic schema requires typed observations",
        "observations" in _critic_schema([("camera", "frame")])["required"],
    )
    _focus_schema = _critic_schema([("camera", "frame")])
    check(
        "critic schema can request bounded aligned focus regions",
        "focus_requests" in _focus_schema["required"]
        and _focus_schema["properties"]["focus_requests"]["maxItems"] == 2,
    )
    _focus_item_schema = _focus_schema["properties"]["focus_requests"]["items"]
    check(
        "focus requests must declare frame and coordinate space",
        {"source", "source_frame"}.issubset(_focus_item_schema["required"]),
    )
    _valid_focus = _focus_requests(
        {
            "scores": {"camera": 2},
            "focus_requests": [
                {
                    "id": "left rib",
                    "axis": "camera",
                    "source": "candidate_frame",
                    "source_frame": 1,
                    "region": [0.1, 0.2, 0.3, 0.7],
                    "reason": "rib foot is below reliable full-frame detail",
                }
            ],
        },
        [("camera", "frame")],
        focus_references={1: "refs/f1.png"},
    )
    check(
        "valid focus requests preserve axis, reason and top-left crop",
        len(_valid_focus) == 1
        and _valid_focus[0]["id"] == "left_rib"
        and _valid_focus[0]["source_frame"] == 1
        and _valid_focus[0]["crop"] == [0.1, 0.2, 0.3, 0.7],
        str(_valid_focus),
    )
    _lighting_milestone = _strict_layers["4"].as_milestone(plan_strips(_strict_shot))
    _frame_local_refs = _focus_references(_strict_shot, _lighting_milestone, allowed_frames=[40])
    check(
        "canonical frame-local focus cannot inspect a different judged frame",
        set(_frame_local_refs) == {40},
        str(_frame_local_refs),
    )
    _strip_focus = _focus_requests(
        {
            "scores": {"camera": 2},
            "focus_requests": [
                {
                    "id": "f80 ring cross",
                    "axis": "camera",
                    "source": "motion_strip",
                    "source_frame": 80,
                    "region": [0.585, 0.12, 0.7, 0.82],
                    "reason": "the f80 strip panel is too small to resolve",
                }
            ],
        },
        [("camera", "frame")],
        focus_references={1: "refs/f1.png", 40: "refs/f40.png", 80: "refs/f80.png", 120: "refs/f120.png"},
        motion_frames=[1, 20, 40, 60, 80, 100, 120],
    )
    check(
        "strip-global f80 coordinates map to an f80-local optical crop",
        len(_strip_focus) == 1
        and _strip_focus[0]["source_frame"] == 80
        and _strip_focus[0]["crop"] == [0.095, 0.12, 0.9, 0.82],
        str(_strip_focus),
    )
    _cross_panel_focus = _focus_requests(
        {
            "scores": {"camera": 2},
            "focus_requests": [
                {
                    "id": "ambiguous",
                    "axis": "camera",
                    "source": "motion_strip",
                    "source_frame": 80,
                    "region": [0.56, 0.2, 0.59, 0.6],
                    "reason": "crosses the f60/f80 seam",
                }
            ],
        },
        [("camera", "frame")],
        focus_references={80: "refs/f80.png"},
        motion_frames=[1, 20, 40, 60, 80, 100, 120],
    )
    check("cross-panel strip crops are rejected as ambiguous", not _cross_panel_focus, str(_cross_panel_focus))
    _bad_focus = _focus_requests(
        {
            "scores": {"camera": 4},
            "focus_requests": [
                {
                    "id": "fishing",
                    "axis": "camera",
                    "source": "candidate_frame",
                    "source_frame": 1,
                    "region": [0, 0, 0.2, 0.2],
                    "reason": "axis already passes",
                },
                {
                    "id": "whole",
                    "axis": "camera",
                    "source": "candidate_frame",
                    "source_frame": 1,
                    "region": [0, 0, 0.9, 0.9],
                    "reason": "not a focus crop",
                },
            ],
        },
        [("camera", "frame")],
        focus_references={1: "refs/f1.png"},
    )
    check("passing axes and near-full-frame fishing cannot trigger focus renders", not _bad_focus, str(_bad_focus))
    _panel_audit = _audit_panel_citations(
        {"observations": [{"id": "rib", "panel_ids": ["shown", "invented"]}]}, [{"id": "shown"}]
    )
    check(
        "a critic cannot cite a focus panel it was never shown",
        _panel_audit["observations"][0]["panel_ids"] == ["shown"]
        and _panel_audit["invalid_panel_citations"][0]["panel_ids"] == ["invented"],
        str(_panel_audit),
    )

    print("\n[critic evidence overrides invented measurements]")
    def _observation(*, kind="measurable", claim_id="layout.ring_width", check_ids=None, action="enlarge it"):
        return {
            "id": "ring-width",
            "kind": kind,
            "axis": "layout",
            "property": "ring_width",
            "observation": "ring is only 0.11 W",
            "action": action,
            "moment": 1,
            "roles": ["hero.ring"],
            "claim_id": claim_id,
            "check_ids": list(check_ids or []),
            "panel_ids": [],
        }

    _bindings = {"layout.ring_width": frozenset({"bbox_ring"})}
    _pass_evidence = [{"id": "bbox_ring", "pass": True, "authoritative": True}]
    _claimed = {
        "pass": False,
        "observations": [_observation(check_ids=["bbox_ring"])],
    }
    _filtered = _filter_critic_issues(_claimed, _pass_evidence, claim_bindings=_bindings)
    check(
        "a measurable claim contradicting a PASS cannot trigger repair",
        not _filtered["issues"] and _filtered["judge_conflict"] and len(_filtered["contradicted_issues"]) == 1,
        str(_filtered),
    )
    _uncovered = _filter_critic_issues(
        {
            "pass": False,
            "observations": [_observation(claim_id=None, check_ids=[])],
        },
        _pass_evidence,
        claim_bindings=_bindings,
    )
    check(
        "an uncovered measurable defect becomes a contract gap rather than a fake contradiction",
        not _uncovered["issues"] and _uncovered["contract_gap"] and not _uncovered.get("judge_conflict"),
        str(_uncovered),
    )
    _failed = _filter_critic_issues(
        {
            "pass": False,
            "observations": [_observation(check_ids=["bbox_ring"])],
        },
        [{"id": "bbox_ring", "pass": False, "authoritative": True}],
        claim_bindings=_bindings,
    )
    check(
        "a measurable claim backed by a FAIL remains actionable",
        len(_failed["issues"]) == 1 and not _failed.get("judge_conflict"),
        str(_failed),
    )
    _visual = _filter_critic_issues(
        {
            "pass": False,
            "observations": [
                _observation(
                    kind="qualitative",
                    claim_id="layout.ring_width",
                    check_ids=[],
                    action="separate the silhouette",
                )
            ],
        },
        _pass_evidence,
        claim_bindings=_bindings,
        qualified_claims={"layout.ring_width"},
    )
    check("qualified qualitative criticism remains actionable", _visual["issues"])
    _full_rubric = _filter_critic_issues(
        {
            "pass": False,
            "observations": [_observation(claim_id=None, check_ids=[])],
        },
        None,
    )
    check(
        "full-rubric measurable defects without evidence remain explicit gaps",
        _full_rubric["contract_gap"] and not _full_rubric["issues"],
    )
    _gated = _apply_evidence_gate(
        {"pass": True, "issues": [], "scores": {"layout": 4}},
        [
            {
                "id": "bbox_ring",
                "metric": "bbox_width",
                "value": 0.11,
                "target": "0.16..0.18",
                "pass": False,
                "authoritative": True,
            }
        ],
    )
    check(
        "a failed authoritative contract overrules a flattering critic",
        not _gated["pass"] and _gated["decided_by"] == "checks" and _gated["issues"][0].startswith("[check:bbox_ring]"),
        str(_gated),
    )
    check("authoritative check failures skip redundant subjective panels", not _needs_critic_panel(_gated))
    _merged_worklist = _merge_worklist_items(
        {"items": ["old done", "unresolved defect"], "done": ["old done"], "notes": []},
        ["new ticket"],
    )
    check(
        "rewriting a worklist carries unresolved defects across attempts",
        _merged_worklist["items"] == ["unresolved defect", "new ticket"] and _merged_worklist["done"] == [],
        str(_merged_worklist),
    )
    with tempfile.TemporaryDirectory() as _workdir:
        _workroot = Path(_workdir)
        (_workroot / "state" / "worklists").mkdir(parents=True)
        (_workroot / "state" / "worklists" / "layer-2.json").write_text(
            json.dumps({"items": ["built", "repair dead control"], "done": ["built"], "notes": []})
        )
        _work_evidence = _worklist_evidence(_workroot, "2")
        _work_gated = _apply_evidence_gate({"pass": True, "issues": [], "scores": {"materials": 4}}, _work_evidence)
        check(
            "an unfinished durable worklist blocks critic handoff",
            len(_work_evidence) == 1
            and not _work_evidence[0]["pass"]
            and not _work_gated["pass"]
            and "repair dead control" in _work_gated["issues"][0],
            str(_work_gated),
        )
    check(
        "a passing round outranks an equal-mean contract failure",
        _round_rank({"pass": True, "mean": 4.0}) > _round_rank({"pass": False, "mean": 4.0}),
    )

    print("\n[canonical reproduction is pixels, not a second opinion]")
    from PIL import Image as _EvidenceImage

    with tempfile.TemporaryDirectory() as _evdir:
        _evroot = Path(_evdir)
        _live = _evroot / "live.png"
        _same = _evroot / "same.png"
        _near = _evroot / "near.png"
        _different = _evroot / "different.png"
        _EvidenceImage.new("RGB", (32, 18), (40, 40, 40)).save(_live)
        _EvidenceImage.new("RGB", (32, 18), (40, 40, 40)).save(_same)
        _near_im = _EvidenceImage.new("RGB", (32, 18), (40, 40, 40))
        _near_im.putpixel((2, 2), (42, 42, 42))
        _near_im.save(_near)
        _EvidenceImage.new("RGB", (32, 18), (80, 80, 80)).save(_different)
        check("identical canonical pixels reproduce", _image_reproduction(_live, _same)["match"])
        check("tiny antialias movement still reproduces", _image_reproduction(_live, _near)["match"])
        check("a materially different frame does not reproduce", not _image_reproduction(_live, _different)["match"])

        from vfx_harness.evidence.checks import layer_evidence

        (_evroot / "checks.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "checks": [
                        {
                            "id": "bright",
                            "owner_layer": "1",
                            "fault_owner": "1",
                            "activates_at": "1",
                            "lifecycle": "layer",
                            "axis": "layout",
                            "frame": 1,
                            "ref": "refs/r.png",
                            "metric": "region_mean",
                            "regions": {"r": [0, 0, 1, 1]},
                            "op": "band",
                            "lo": 35,
                            "hi": 45,
                            "stage": "pre_grade",
                            "rejects": ["bad.png"],
                            "proof": {"ref": 40, "adversary": [0]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        _evidence = layer_evidence(_evroot, "1", frame=1, ref="refs/r.png", render=_live)
        check(
            "canonical checks are evaluated on the exact judged render",
            len(_evidence) == 1 and _evidence[0]["pass"] and _evidence[0]["authoritative"],
            str(_evidence),
        )
        (_evroot / "runtime_checks.json").write_text(
            json.dumps(
                [
                    {
                        "id": "runtime-bright",
                        "layer": "1",
                        "axis": "layout",
                        "frame": 1,
                        "ref": "refs/r.png",
                        "metric": "frame_mean",
                        "op": ">=",
                        "lo": 35,
                        "origin": "builder",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _split_evidence = layer_evidence(_evroot, "1", frame=1, ref="refs/r.png", render=_live)
        check(
            "planner contracts and runtime evidence load from separate ledgers",
            {row["id"] for row in _split_evidence} == {"bright", "runtime-bright"}
            and sum(row["authoritative"] for row in _split_evidence) == 1,
            str(_split_evidence),
        )

        from vfx_harness.evidence.scene_checks import _blender_probe
        from vfx_harness.evidence.scene_checks import layer_evidence as _scene_evidence

        _scene_rows = [
            {
                "id": "ring-width",
                "owner_layer": "1",
                "fault_owner": "1",
                "activates_at": "1",
                "lifecycle": "persistent",
                "axis": "layout",
                "frame": 1,
                "kind": "bbox_width",
                "roles": ["hero.ring.outer"],
                "op": "band",
                "lo": 0.16,
                "hi": 0.18,
            },
            {
                "id": "ribs",
                "owner_layer": "1",
                "fault_owner": "1",
                "activates_at": "1",
                "lifecycle": "persistent",
                "axis": "layout",
                "frame": 1,
                "kind": "object_count",
                "roles": ["architecture.rib.*"],
                "op": "eq",
                "value": 3,
            },
        ]
        (_evroot / "scene_checks.json").write_text(
            json.dumps({"schema": 2, "contracts": _scene_rows}), encoding="utf-8"
        )

        class _FakeSceneSession:
            def run(self, code, *, journal=True):
                compile(code, "<scene-check-probe>", "exec")
                check("live-scene probes stay out of the canonical replay journal", journal is False)
                return {
                    "result": [
                        {
                            "id": "ring-width",
                            "value": 0.1634,
                            "objects": ["ring_outer"],
                            "roles": ["hero.ring.outer"],
                            "error": "",
                        },
                        {
                            "id": "ribs",
                            "value": 3,
                            "objects": ["rib_centre", "rib_left", "rib_right"],
                            "roles": ["architecture.rib.center", "architecture.rib.left", "architecture.rib.right"],
                            "error": "",
                        },
                    ]
                }

        _scene = _scene_evidence(_evroot, "1", frame=1, session=_FakeSceneSession())
        check(
            "live-scene facts are evaluated and attached to the evidence card",
            len(_scene) == 2 and all(item["pass"] for item in _scene),
            str(_scene),
        )
        check(
            "strict interface contracts are authoritative regardless of authoring history",
            all(row["authoritative"] for row in _scene),
            str(_scene),
        )
        check(
            "scene evidence preserves matched objects for existence/legibility judgement",
            _scene[1]["objects"] == ["rib_centre", "rib_left", "rib_right"],
            str(_scene),
        )
        check(
            "scene evidence explains metric semantics instead of exposing an opaque name",
            "normalized camera coordinates" in _scene[0].get("definition", ""),
            str(_scene),
        )
        from vfx_harness.evidence.scene_checks import KIND_DEFINITIONS as _scene_definitions

        check(
            "radial inward fraction publishes its exact formula",
            "normal dot" in _scene_definitions["radial_inward_fraction"]
            and "<= 0" in _scene_definitions["radial_inward_fraction"],
        )
        compile(_blender_probe(_scene_rows, 1), "<scene-check-probe>", "exec")
        check("generated Blender scene probe is syntactically valid", True)

        from vfx_harness.evidence.scene_checks import validate_row as _validate_scene_row

        check(
            "name-based scene selectors are rejected with no compatibility path",
            "removed"
            in (
                _validate_scene_row(
                    {
                        "id": "legacy",
                        "kind": "object_count",
                        "objects": ["rib_*"],
                        "op": "eq",
                        "value": 3,
                    }
                )
                or ""
            ),
        )
        _node_contract = {
            "id": "material-control",
            "owner_layer": "2",
            "fault_owner": "2",
            "activates_at": "2",
            "lifecycle": "persistent",
            "kind": "node_socket_value",
            "graph": "material",
            "material_roles": ["material.hero.*"],
            "node_roles": ["control.hero.gain"],
            "socket": "Value",
            "socket_index": 1,
            "op": "band",
            "lo": 0.5,
            "hi": 2.0,
        }
        check(
            "semantic material/node contracts are first-class scene checks",
            _validate_scene_row(_node_contract) is None,
            str(_validate_scene_row(_node_contract)),
        )
        _functional_contract = {
            **{key: value for key, value in _node_contract.items() if key not in {"socket", "op", "lo", "hi"}},
            "id": "value-output-responds",
            "kind": "control_render_response",
            "socket_index": 0,
            "socket_direction": "output",
            "probe_values": [5.5, 26],
            "frame": 1,
            "region": [0.4, 0.4, 0.6, 0.6],
            "response_metric": "mae",
            "op": "min",
            "lo": 3,
        }
        check(
            "functional controls may explicitly target a node output socket",
            _validate_scene_row(_functional_contract) is None,
            str(_validate_scene_row(_functional_contract)),
        )
        from vfx_harness.evidence.scene_checks import _control_script as _scene_control_script

        _output_probe = _scene_control_script(_functional_contract, 26)
        check(
            "generated functional probe falls back across input and output collections",
            "nodes[0].inputs),('output',nodes[0].outputs)" in _output_probe and "resolved_direction" in _output_probe,
        )

        from vfx_harness.domain.contracts import active_for as _active_contract

        check(
            "persistent interfaces remain active in downstream layers",
            _active_contract(_node_contract, "6", frame=1),
        )

        _locks = {}
        check(
            "first comparison settings establish the round lock",
            _comparison_lock_error(_locks, (1, 1, "full"), ("eevee", 0.5, 50)) is None,
        )
        check(
            "identical comparison settings remain legal",
            _comparison_lock_error(_locks, (1, 1, "full"), ("eevee", 0.5, 50)) is None,
        )
        check(
            "comparison settings cannot drift within a round",
            "LOCKED" in (_comparison_lock_error(_locks, (1, 1, "full"), ("workbench", 0.5, 50)) or ""),
        )
        check(
            "the round-wide base lock also covers other frames",
            _comparison_lock_error(_locks, (1, "base"), ("eevee", 0.5, None)) is None
            and "LOCKED" in (_comparison_lock_error(_locks, (1, "base"), ("solid", 0.5, None)) or ""),
        )
        check(
            "a new round may establish new settings",
            _comparison_lock_error(_locks, (2, 1, "full"), ("workbench", 0.5, 50)) is None,
        )
        check(
            "crop comparison inherits the round's base mode and scale",
            _comparison_mode_scale({"res_pct": 300}, ("eevee", 0.5, None)) == ("eevee", 0.5),
        )
        check(
            "an explicit crop scale remains explicit for lock validation",
            _comparison_mode_scale({"scale": 0.4}, ("eevee", 0.5, None)) == ("eevee", 0.4),
        )
        check(
            "framing mistakes fail before reaching the Blender worker",
            "object" in (_check_args_error("framing", {"frame": 1}) or ""),
        )
        check(
            "motion needs two frames before reaching the Blender worker",
            "at least 2" in (_check_args_error("motion", {"object": "Hero", "frames": [1]}) or ""),
        )
        check(
            "layout comparison suppresses look-action advice",
            not _layer_feedback_policy(_strict_shot.folder, "1")["look_actions"],
        )
        _metric_probe = _EvidenceImage.new("RGB", (32, 16), (255, 255, 255))
        check(
            "layout render text suppresses exposure and structure metrics",
            "exposure:" not in _stats(_metric_probe, feedback_groups=[])
            and "structure:" not in _metrics_line(_metric_probe, _metric_probe, feedback_groups=[]),
        )
        _gate_root = Path(tempfile.mkdtemp())
        _gate_ref = _gate_root / "ref.png"
        _gate_black = _gate_root / "black.png"
        _gate_lit = _gate_root / "lit.png"
        _EvidenceImage.new("RGB", (32, 16), (100, 100, 100)).save(_gate_ref)
        _EvidenceImage.new("RGB", (32, 16), (0, 0, 0)).save(_gate_black)
        _EvidenceImage.new("RGB", (32, 16), (40, 40, 40)).save(_gate_lit)
        (_gate_root / "checks.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "checks": [
                        {
                            "id": "judgeable",
                            "owner_layer": "1",
                            "fault_owner": "1",
                            "activates_at": "1",
                            "lifecycle": "layer",
                            "axis": "layout",
                            "frame": 1,
                            "ref": "ref.png",
                            "metric": "frame_mean",
                            "op": ">=",
                            "lo": 4,
                            "stage": "pre_grade",
                            "origin": "planner",
                            "rejects": ["black.png"],
                            "proof": {"adversary": [0.0]},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        _gate_bad, _gate_bad_rows = _pixel_contract_gate(_gate_root, "1", frame=1, ref="ref.png", render=_gate_black)
        _gate_good, _gate_good_rows = _pixel_contract_gate(_gate_root, "1", frame=1, ref="ref.png", render=_gate_lit)
        check(
            "black image contract reopens repair before an expensive critic",
            not _gate_bad and not _gate_bad_rows[0]["pass"],
            str(_gate_bad_rows),
        )
        check(
            "judgeable image contract closes the in-turn convergence gate",
            _gate_good and _gate_good_rows[0]["pass"],
            str(_gate_good_rows),
        )
        check(
            "finish comparison enables look-action advice",
            _layer_feedback_policy(_strict_shot.folder, "6")["look_actions"],
        )
        _material_groups = set(_layer_feedback_policy(_strict_shot.folder, "2")["groups"])
        check(
            "material feedback includes detail but suppresses exposure and halation",
            "detail" in _material_groups and "exposure" not in _material_groups and "halation" not in _material_groups,
            str(_material_groups),
        )
        _focus = _required_focus_requests(
            _strict_shot,
            "2",
            1,
            _owned_axes(load_axes(_strict_shot), _strict_layers["2"]),
        )
        check(
            "small contract-critical features require focus before the first judge",
            [row["id"] for row in _focus] == ["orb_interior"],
            str(_focus),
        )
        check(
            "layout owns no motion axis, so it skips motion strips",
            not _axes_need_motion(_owned_axes(load_axes(_strict_shot), _strict_layers["1"])),
        )
        check(
            "animation owns a motion axis, so it receives motion strips",
            _axes_need_motion(_owned_axes(load_axes(_strict_shot), _strict_layers["3"])),
        )
        _motion_milestone = _strict_layers["3"].as_milestone(plan_strips(_strict_shot))
        check(
            "a multi-beat motion judge sees every judge frame and every interval",
            _layer_motion_frames(_strict_layers["3"], _motion_milestone, _strict_shot.frames)
            == [1, 20, 40, 60, 80, 100, 120],
        )
        check(
            "motion ablation evaluates every owned judge frame instead of only the rest pose",
            _ablation_frames(_strict_shot, _strict_layers["3"]) == [1, 40, 80, 120],
        )
        check(
            "static ablation remains a single primary-frame comparison",
            _ablation_frames(_strict_shot, _strict_layers["1"]) == [1],
        )

        _inherited_only = _scene_completion_state([{"authoritative": True, "pass": True, "owner_layer": "1"}], "3")
        _current_owned = _scene_completion_state(
            [
                {"authoritative": True, "pass": True, "owner_layer": "1"},
                {"authoritative": True, "pass": True, "owner_layer": "3"},
            ],
            "3",
        )
        check(
            "passing inherited interfaces cannot seal an untouched downstream layer",
            _inherited_only["interfaces_ready"] and not _inherited_only["may_seal"],
            str(_inherited_only),
        )
        check(
            "a passing current-layer contract can seal its own mutation gate",
            _current_owned["interfaces_ready"] and _current_owned["may_seal"],
            str(_current_owned),
        )

        _all_green = {
            "pass": False,
            "issues": [],
            "reference_unusable": False,
            "evidence": [{"authoritative": True, "pass": True, "owner_layer": "1", "fault_owner": "1"}],
        }
        check(
            "Layer 1 stops speculative revision after authoritative convergence",
            _evidence_convergence_stop(_strict_layers["1"], _all_green),
        )
        check(
            "the same evidence cannot prematurely stop a later layer",
            not _evidence_convergence_stop(_strict_layers["2"], _all_green),
        )

        from vfx_harness.evidence.compare_panels import focus_signal, focus_views, save_context_sheet, save_focus_sheet

        _ref_full = _evroot / "focus_ref.png"
        _candidate_full = _evroot / "focus_candidate_full.png"
        _candidate_crop = _evroot / "focus_candidate_crop.png"
        _EvidenceImage.new("RGB", (200, 100), (0, 0, 180)).save(_ref_full)
        _EvidenceImage.new("RGB", (200, 100), (180, 0, 0)).save(_candidate_full)
        _EvidenceImage.new("RGB", (400, 240), (180, 0, 0)).save(_candidate_crop)
        check(
            "flat empty focus crops are rejected as having no visual signal",
            not focus_signal(_EvidenceImage.new("RGB", (80, 80), (20, 20, 20)))["has_signal"],
        )
        _signal_probe = _EvidenceImage.new("RGB", (80, 80), (20, 20, 20))
        for _x in range(20, 60):
            _signal_probe.putpixel((_x, 40), (240, 240, 240))
        check(
            "a small real feature keeps a focus crop judgeable",
            focus_signal(_signal_probe)["has_signal"],
        )
        _crop = [0.25, 0.2, 0.75, 0.8]
        _views, _meta = focus_views(
            _candidate_crop, _ref_full, _crop, ("side_by_side", "wipe", "overlay", "difference")
        )
        _wipe = dict(_views)["wipe"]
        check(
            "focus wipe keeps candidate left and matched reference right",
            _wipe.getpixel((_wipe.width // 4, _wipe.height // 2))[0] > 150
            and _wipe.getpixel((3 * _wipe.width // 4, _wipe.height // 2))[2] > 150,
        )
        check("focus alignment never upscales either source", not _meta["upscaled"], str(_meta))
        _focus_out = save_focus_sheet(_candidate_crop, _ref_full, _crop, _evroot / "focus_sheet.jpg")
        _context_out = save_context_sheet(_candidate_full, _ref_full, _crop, _evroot / "focus_context.jpg")
        check(
            "focus comparison writes both detail and mandatory context artifacts",
            Path(_focus_out["image_path"]).is_file() and Path(_context_out["image_path"]).is_file(),
        )
        _mismatch = _evroot / "focus_mismatch.png"
        _EvidenceImage.new("RGB", (300, 100), (180, 0, 0)).save(_mismatch)
        try:
            focus_views(_mismatch, _ref_full, _crop)
            _mismatch_rejected = False
        except ValueError:
            _mismatch_rejected = True
        check("an aspect-mismatched crop cannot masquerade as an aligned wipe", _mismatch_rejected)

        from vfx_harness.orchestration.revalidation import digest as _digest
        from vfx_harness.orchestration.revalidation import eligibility as _eligible
        from vfx_harness.orchestration.revalidation import input_manifest as _input_manifest

        _sealed = _evroot / "sealed.png"
        _ref_sealed = _evroot / "ref.png"
        _EvidenceImage.new("RGB", (16, 9), (20, 20, 20)).save(_sealed)
        _EvidenceImage.new("RGB", (16, 9), (30, 30, 30)).save(_ref_sealed)
        _manifest = {"complete": "boundary"}
        _outcome = {
            "schema": 2,
            "status": "passed",
            "revalidation_manifest": _manifest,
            "canonical": [
                {
                    "frame": 1,
                    "render": "sealed.png",
                    "render_sha256": _digest(_sealed),
                    "ref": "ref.png",
                    "ref_sha256": _digest(_ref_sealed),
                    "qualitative_defects": [],
                }
            ],
        }
        check(
            "an unchanged schema-2 sealed outcome is revalidation-eligible", _eligible(_outcome, _manifest, _evroot)[0]
        )
        check(
            "a changed deterministic input invalidates the fast path",
            not _eligible(_outcome, {"complete": "changed"}, _evroot)[0],
        )
        _fake_layer = type(
            "Layer",
            (),
            {
                "id": "1",
                "script": "build/01_layout.py",
                "judges": ((1, "ref.png"),),
                "stages": (type("Unit", (), {"plan": "plans/01_layout.md"})(),),
            },
        )()
        (_evroot / "plans").mkdir(exist_ok=True)
        (_evroot / "plans" / "01_layout.md").write_text("unit plan\n", encoding="utf-8")
        (_evroot / "layers.json").write_text(
            json.dumps(
                {
                    "schema": 4,
                    "layers": [
                        {
                            "id": "1",
                            "script": "build/01_layout.py",
                            "primary_judge": 1,
                            "judge": [{"frame": 1, "ref": "ref.png"}],
                            "stages": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        _m1 = _input_manifest(_evroot, _fake_layer, blender_version="5.2")
        (_evroot / "runtime_checks.json").write_text("[]\n", encoding="utf-8")
        _m2 = _input_manifest(_evroot, _fake_layer, blender_version="5.2")
        check("runtime evidence is part of the revalidation cache key", _m1 != _m2)

    print("\n[metrics]")
    ref = str(shot.folder / "refs/f100_city.jpg")
    v = look_vector(ref)
    check("look vector has banded metrics", {"detail_bot", "points_bot", "structure_mid", "hot_core"} <= set(v))
    check("identical images -> no deltas", compare(v, v) == [])
    d = compare({**v, "points_bot": v["points_bot"] * 0.1}, v)
    check("large drop is blocking", any(x.blocking for x in d))
    check("zero-ref does not explode", "~0" in str(compare({"chroma_spread": 900.0}, {"chroma_spread": 0.0})[0]))

    # halation divides by a count of blown-out pixels. When that count is a handful the
    # ratio is an artifact of its denominator, and `if hot else 0.0` published it anyway:
    # f045_pullback read 0.0 at four widths and 1769.5 at two, same picture. An UNMEASURABLE
    # metric must be absent, because 0.0 is not neutral here — it is the loudest claim the
    # metric can make ("no glow at all"), and the builder acts on it.
    _hot = str(shot.folder / "refs/f300_widen.jpg")  # 506 hot px — real core
    _cold = str(shot.folder / "refs/f045_pullback.jpg")  # 4 hot px — nothing to divide by
    check("halation is reported where there IS a hot core", "halation" in look_vector(_hot))
    check(
        "halation is ABSENT, not 0.0, where there is not",
        "halation" not in look_vector(_cold),
        f"{look_vector(_cold).get('halation')}",
    )
    check("hot_core is reported either way", {"hot_core"} <= set(look_vector(_cold)))
    _at = {w: look_vector(_cold, width=w).get("halation") for w in (320, 480, 640, 960)}
    check("an unmeasurable halation stays unmeasurable under resampling", set(_at.values()) == {None}, f"{_at}")
    # ...and a real one stays put. Absence must come from the image, not from the width.
    _real = {w: look_vector(_hot, width=w).get("halation") for w in (480, 640, 960)}
    check("a measurable halation survives resampling", all(x is not None for x in _real.values()), f"{_real}")
    # The signal the floor could have silently eaten: a render with no blown core where the
    # reference has one used to surface as `halation 0.0 vs ref 11.5`. hot_core keeps it.
    check(
        "no-core-vs-core still produces a delta",
        any(x.key == "hot_core" for x in compare({**look_vector(_hot), "hot_core": 0.0}, look_vector(_hot))),
    )

    # FORM AND SKY. Every other metric here is a histogram or an edge count, so a flat card
    # and a solid tower with the same pixel statistics are indistinguishable to all of them.
    # `structure_top` is credited in this module's docstring with catching "flat fog-wall
    # skies vs wispy structured cloud" and does not: on the layer-5 render against
    # f001_open it reads 15% apart against its own 45% tolerance while the two skies share
    # nothing to look at.
    _flat = str(shot.folder / "renders/5_best.png")  # banded sky, no atmosphere
    _turb = str(shot.folder / "refs/f001_open.jpg")  # turbulent green-teal cloud
    _fv, _tv = look_pair(_flat, _turb)
    check("the look vector carries the form metrics", {"aniso_top", "local_range"} <= set(_fv))
    _keys = {d.key for d in compare(_fv, _tv)}
    check("structure_top is SILENT on two skies that share nothing", "structure_top" not in _keys, f"{sorted(_keys)}")
    check("...and aniso_top is not", "aniso_top" in _keys, f"{sorted(_keys)}")
    check(
        "aniso_top can decide a moment (blocking)", any(d.key == "aniso_top" and d.blocking for d in compare(_fv, _tv))
    )
    # A metric that fires on a plate compared with ITSELF is measuring the instrument.
    for _p in ("refs/f001_open.jpg", "refs/f195_black.jpg", "refs/f440_final.jpg"):
        _q = str(shot.folder / _p)
        check(
            f"no self-comparison false positive on {Path(_p).name}",
            not [d for d in compare(*look_pair(_q, _q)) if d.key in ("aniso_top", "local_range")],
        )
    # Layer 1 is layout: no sky exists yet, so the sky metric must not accuse it.
    _l1 = str(shot.folder / "renders/1@f100_canonical_f100.png")
    check(
        "aniso_top stays quiet on a layer that has not built a sky",
        "aniso_top" not in {d.key for d in compare(*look_pair(_l1, str(shot.folder / "refs/f100_city.jpg")))},
    )

    # compare_frame used to force BOTH images to height 512 AFTER the render had already
    # been shrunk by `scale`, so the reference (always full-res) downscaled and stayed
    # sharp while a default scale=0.4 render was UPSCALED 1.33x — every detail metric read
    # soft, and two calls at different scales were not comparable to each other. Measuring
    # an image against ITSELF must therefore give the same answer at every scale.
    from PIL import Image as _I

    from vfx_harness.blender.tools import _compare_image as _ci

    _src = _I.open(ref).convert("RGB")
    _sigs = set()
    for _s in (0.35, 0.4, 0.6, 1.0):
        _p = Path(tempfile.mkdtemp()) / f"s{_s}.png"
        _src.resize((round(_src.width * _s), round(_src.height * _s)), _I.LANCZOS).save(_p)
        _lines = _ci(str(_p), Path(ref), "x")["content"][0]["text"].splitlines()
        # exposure + per-band structure must not depend on the scale knob
        _sigs.add(_lines[1] + " | " + _lines[2].split("· halation")[0])
    check(
        "metrics are scale-invariant for an image vs itself",
        len(_sigs) == 1,
        f"{len(_sigs)} variants: {sorted(_sigs)[:2]}",
    )
    _out = _ci(str(shot.folder / "renders/1@f100_canonical_f100.png"), Path(ref), "x")
    check(
        "compare_frame states the SIGNED gap, not just raw values",
        "LOW" in _out["content"][0]["text"] or "HIGH" in _out["content"][0]["text"],
    )

    print("\n[plan grounding]")
    # The plan is the only stage whose output nothing checked, and its fingerprints are the
    # numeric targets every later layer aims at. Each verdict is exercised against a real
    # plate, because a checker nobody has watched fire is not a checker.
    from vfx_harness.evaluation import grounding as _gr

    _truth = _gr.measure(shot.folder / "refs/f300_widen.jpg")  # a plate WITH hot core
    _cold_t = _gr.measure(shot.folder / "refs/f045_pullback.jpg")  # 73 ppm — no denominator

    _fp = f"mean {_truth['mean']:.0f} · detail {_truth['detail']:.1f}"
    _rows, _ = _gr.check_fingerprint(_fp, _truth)
    check(
        "a fingerprint read off its own plate is grounded",
        len(_rows) == 2 and all(r["verdict"] == "ok" for r in _rows),
        f"{_rows}",
    )

    _rows, _ = _gr.check_fingerprint(f"mean {_truth['mean'] * 2:.0f}", _truth)
    check(
        "a target that is not in the picture is a MISMATCH", [r["verdict"] for r in _rows] == ["MISMATCH"], f"{_rows}"
    )

    # The verdict a plain numeric diff cannot produce. barrel_roll M1 claimed "halation 0.0"
    # on a plate with no blown pixels at all; against the old always-0.0 metric that read as
    # a perfect match, so the target survived into every build that aimed at it.
    _rows, _ = _gr.check_fingerprint("halation 0.0", _cold_t)
    check(
        "a target the plate cannot support is UNMEASURABLE, not a match",
        [r["verdict"] for r in _rows] == ["UNMEASURABLE"],
        f"{_rows}",
    )
    _rows, _ = _gr.check_fingerprint("halation 2340.1", _cold_t)
    check(
        "...and so is the same claim carrying a large value",
        [r["verdict"] for r in _rows] == ["UNMEASURABLE"],
        f"{_rows}",
    )

    # A checker that silently ignores tokens it does not understand reports "all grounded"
    # while checking nothing — the failure class this module exists to catch.
    _rows, _un = _gr.check_fingerprint("mean 40 · wibble 123.4", {**_truth, "mean": 40.0})
    check("an unrecognised number is reported, not skipped", "123.4" in _un, f"{_un}")

    print("\n[plan gate]")
    # Every check gets a deliberately broken fixture. A gate nobody has watched fire is not
    # a gate — and this one decides whether a plan is fit to build on.
    from vfx_harness.evaluation import plan_gate as _pg

    _lab = Path(tempfile.mkdtemp())
    (_lab / "logs").mkdir()
    (_lab / "real.out").write_text("line one\nline two\n")

    _f, _ = _pg._check_citations(_lab, "see `logs/../real.out` for the measurement")
    check("a citation that resolves is not a finding", not _f, f"{_f}")
    _f, _ = _pg._check_citations(_lab, "measured in `logs/plan_lab/spike_99.out`")
    check("a citation that does NOT resolve is blocking", len(_f) == 1 and _f[0].blocking, f"{_f}")
    _f, _ = _pg._check_citations(_lab, "see `real.out` line 9")
    check("a cited line past end-of-file is blocking", len(_f) == 1 and "9" in _f[0].what, f"{_f}")
    # The planner prompt itself tells the verifier to search `../*/build/*.py`. Quoting a
    # search instruction is not a claim about a file, and reporting it as rot would train
    # the reader to skim the gate — which is how a check stops being read at all.
    _f, _ = _pg._check_citations(_lab, "search prior work in `../*/build/*.py` first")
    check("a glob is a search instruction, not a citation", not _f, f"{_f}")
    # One repair per dead path, however many times the plan leans on it.
    _f, _ = _pg._check_citations(_lab, "`a/x.out` and `a/x.out` line 3 and `a/x.out`")
    check("a dead path is reported once, not once per mention", len(_f) == 1, f"{_f}")

    _f, _ = _pg._check_evidence(_lab, "**G10·T1 · Rig**  [known ✓spiked — verified]\n- no cite\n")
    check("a ✓spiked ticket with no lab file is blocking", any(f.blocking for f in _f), f"{_f}")
    # The word "spike_04" is not evidence; the file it names is. Substring-matching the tag
    # would have passed every barrel_roll ticket, whose lab was archived and whose spike
    # references resolve to nothing today.
    _cite = "**G10·T1 · Rig**  [known ✓spiked]\n- proof in `logs/plan_lab/spike_01.out`\n"
    _f, _ = _pg._check_evidence(_lab, _cite)
    check(
        "a ✓spiked ticket naming a file that does not exist is still blocking", any(f.blocking for f in _f), f"{_f}"
    )
    (_lab / "logs" / "plan_lab").mkdir(parents=True)
    (_lab / "logs" / "plan_lab" / "spike_01.out").write_text("max|vert| 0.0 -> 45.0\n")
    _f, _ = _pg._check_evidence(_lab, _cite)
    check("a legacy stdout artifact cannot self-certify a spiked claim", any(f.blocking for f in _f), f"{_f}")
    evidence_dir = _lab / "plans" / "evidence" / "spikes"
    evidence_dir.mkdir(parents=True)
    script_bytes = b"print('mechanism')\n"
    output_bytes = b"Blender 5.0.0\nmechanism\n"
    (evidence_dir / "spike.py").write_bytes(script_bytes)
    (evidence_dir / "spike.out").write_bytes(output_bytes)
    contract = {"id": "mechanism", "kind": "bbox_width", "frame": 1, "op": "band", "lo": 0.1, "hi": 0.9}
    (_lab / "scene_checks.json").write_text(json.dumps({"schema": 2, "contracts": [contract]}))
    (evidence_dir / "spike.json").write_text(json.dumps({
        "schema": "vfx-harness.plan-spike/v1",
        "script": {"path": "spike.py", "sha256": hashlib.sha256(script_bytes).hexdigest()},
        "output": {"path": "spike.out", "sha256": hashlib.sha256(output_bytes).hexdigest()},
        "blender": {"executable": "/snap/bin/blender", "version": "Blender 5.0.0"},
        "contracts": [contract],
        "results": [{"id": "mechanism", "value": 0.5, "pass": True}],
        "passed": True,
    }))
    typed_cite = (
        "**G10·T1 · Rig**  [known ✓spiked]\n"
        "- proof in `plans/evidence/spikes/spike.json`\n"
    )
    _f, _ = _pg._check_evidence(_lab, typed_cite)
    check("a typed hash-pinned spike record satisfies the citation claim", not _f, f"{_f}")
    # A bare tag remains non-authoritative even when old scratch output happens to exist.
    _f, _ = _pg._check_evidence(_lab, "**G20·T1 · Roll**  [known ✓spiked — spike_01]\n- x\n")
    check("a bare `spike_NN` tag cannot replace typed authority", any(f.blocking for f in _f), f"{_f}")
    _f, _ = _pg._check_evidence(_lab, "**G30·T1 · Gone**  [known ✓spiked — spike_99]\n- x\n")
    check("...and a bare tag naming a missing spike is blocking", any(f.blocking for f in _f), f"{_f}")
    _f, _ = _pg._check_evidence(_lab, "**G20·T1 · Novel**  [researched]\n- trust me\n")
    check("a researched ticket with no source link is flagged (warn)", len(_f) == 1 and not _f[0].blocking, f"{_f}")
    (_lab / "scene_checks.json").unlink()

    (_lab / "critic_axes.json").write_text('[{"key": "lighting", "desc": "d"}]')
    (_lab / "acceptance.json").write_text("[]")
    (_lab / "layers.json").write_text(
        json.dumps(
            {
                "schema": 4,
                "layers": [
                    {
                        "id": "1",
                        "script": "build/01_test.py",
                        "title": "test",
                        "primary_judge": 1,
                        "owns": ["lighting", "typo_axis"],
                        "reads": "test",
                        "judge": [{"frame": 1, "ref": "ref.png"}],
                        "stages": [
                            {
                                "id": "complete",
                                "title": "complete",
                                "plan": "plans/01_test.md",
                                "depends_on": [],
                                "mutates": {
                                    "mode": "scoped",
                                    "roles": [],
                                    "controls": [],
                                    "script_spans": ["build/01_test.py"],
                                },
                                "protects": {
                                    "selector": "all_active_upstream_interfaces",
                                    "resolve_to_explicit_ids_at": "freeze",
                                },
                                "evaluation": {
                                    "primary_judge": 1,
                                    "judge": [{"frame": 1, "ref": "ref.png"}],
                                    "temporal_evidence": "none",
                                    "claims": [
                                        {
                                            "id": "test.contracts",
                                            "proposition": "contracts pass",
                                            "axis": "lighting",
                                            "property": "illumination",
                                            "subject_roles": ["hero"],
                                            "subject_controls": [],
                                            "moments": [1],
                                            "kind": "atomic",
                                            "required": True,
                                            "authority": "executable_required",
                                            "repair_owner": "complete",
                                            "evidence": [
                                                {"kind": "scene_contract", "id": "bad-scene-check"}
                                            ],
                                        }
                                    ],
                                },
                                "completion": "all_required_claims_and_protected_contracts_pass",
                            }
                        ],
                    }
                ],
            }
        )
    )
    _f, _ = _pg._check_contracts(_lab)
    check(
        "a layer owning an axis the rubric lacks is blocking",
        any(f.blocking and "typo_axis" in f.what for f in _f),
        f"{_f}",
    )
    check(
        "an axis no layer owns is a warn, not a block",
        all(not f.blocking for f in _f if "no layer owns" in f.what),
        f"{_f}",
    )
    _f, _ = _pg._check_contracts(_lab, require_scene_checks=True)
    check(
        "new planning cannot pass clean without live-scene contracts",
        any(f.blocking and f.where == "scene_checks.json" for f in _f),
        f"{_f}",
    )
    (_lab / "scene_checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "contracts": [
                    {
                        "id": "bad-scene-check",
                        "owner_layer": "1",
                        "fault_owner": "1",
                        "activates_at": "1",
                        "lifecycle": "persistent",
                        "axis": "lighting",
                        "frame": 1,
                        "kind": "guess_from_jpeg",
                        "roles": ["hero"],
                        "op": "min",
                        "lo": 1,
                    }
                ],
            }
        )
    )
    _f, _ = _pg._check_contracts(_lab)
    check(
        "an unsupported live-scene contract cannot enter a build",
        any(f.blocking and "unsupported kind" in f.what for f in _f),
        f"{_f}",
    )

    print("\n[planner inputs]")
    # The planner writes every target the rest of the run aims at, and the harness used to
    # hand it a list of FILENAMES. Whether it ever SAW the shot came down to whether it
    # happened to try Read on a .jpg. The critic has long been guaranteed its images
    # ("cannot score a frame it never saw"); the stage that WRITES the targets was not.
    from vfx_harness.agents import planner as _pl
    from vfx_harness.agents.prompts import planner_user_prompt as _pup

    _blocks = _pl._kickoff_blocks(_pup(shot), shot)
    _imgs = [b for b in _blocks if b["type"] == "image"]
    check(
        "every reference still is attached to the kickoff",
        len(_imgs) == len(shot.refs),
        f"{len(_imgs)} images for {len(shot.refs)} refs",
    )
    check(
        "...in the API content-block shape the SDK needs",
        all(b["source"]["type"] == "base64" and b["source"]["media_type"] == "image/jpeg" for b in _imgs),
    )
    check("the kickoff text still leads", _blocks[0]["type"] == "text")

    # probe_video / contact_sheet / extract_frames were defined with @tool and never passed
    # to create_sdk_mcp_server — unreachable on EVERY shot. An absent tool is
    # indistinguishable from one the model declined to call, so nothing noticed.
    import tempfile as _tf

    _names = _pl.build_plan_tools(shot.folder, lab_dir=Path(_tf.mkdtemp()))[1]
    _short = {n.split("__")[-1] for n in _names}
    check(
        "a stills-only shot registers the stills tools",
        {"measure_ref", "spike", "ask_supervisor"} <= _short,
        f"{_short}",
    )
    check(
        "...and does NOT carry video tools that could only fail",
        not ({"contact_sheet", "extract_frames", "probe_video"} & _short),
        f"{_short}",
    )
    _vid = Path(_tf.mkdtemp())
    (_vid / "refs").mkdir()
    (_vid / "refs" / "source.mp4").write_bytes(b"")
    _vshort = {n.split("__")[-1] for n in _pl.build_plan_tools(_vid, lab_dir=Path(_tf.mkdtemp()))[1]}
    check(
        "a shot WITH video gets its scene-read tools",
        {"contact_sheet", "extract_frames", "probe_video"} <= _vshort,
        f"{_vshort}",
    )

    print("\n[planner inputs]")
    # The planner writes every target the rest of the run aims at, and the harness used to
    # hand it a list of FILENAMES. Whether it ever SAW the shot came down to whether it
    # happened to try Read on a .jpg. The critic has long been guaranteed its images
    # ("cannot score a frame it never saw"); the stage that WRITES the targets was not.
    from vfx_harness.agents import planner as _pl
    from vfx_harness.agents.prompts import planner_user_prompt as _pup

    _blocks = _pl._kickoff_blocks(_pup(shot), shot)
    _imgs = [b for b in _blocks if b["type"] == "image"]
    check(
        "every reference still is attached to the kickoff",
        len(_imgs) == len(shot.refs),
        f"{len(_imgs)} images for {len(shot.refs)} refs",
    )
    check(
        "...in the API content-block shape the SDK needs",
        all(b["source"]["type"] == "base64" and b["source"]["media_type"] == "image/jpeg" for b in _imgs),
    )
    check("the kickoff text still leads", _blocks[0]["type"] == "text")

    # probe_video / contact_sheet / extract_frames were defined with @tool and never passed
    # to create_sdk_mcp_server — unreachable on EVERY shot. An absent tool is
    # indistinguishable from one the model declined to call, so nothing noticed.
    import tempfile as _tf

    _names = _pl.build_plan_tools(shot.folder, lab_dir=Path(_tf.mkdtemp()))[1]
    _short = {n.split("__")[-1] for n in _names}
    check(
        "a stills-only shot registers the stills tools",
        {"measure_ref", "spike", "ask_supervisor"} <= _short,
        f"{_short}",
    )
    check(
        "...and does NOT carry video tools that could only fail",
        not ({"contact_sheet", "extract_frames", "probe_video"} & _short),
        f"{_short}",
    )
    _vid = Path(_tf.mkdtemp())
    (_vid / "refs").mkdir()
    (_vid / "refs" / "source.mp4").write_bytes(b"")
    _vshort = {n.split("__")[-1] for n in _pl.build_plan_tools(_vid, lab_dir=Path(_tf.mkdtemp()))[1]}
    check(
        "a shot WITH video gets its scene-read tools",
        {"contact_sheet", "extract_frames", "probe_video"} <= _vshort,
        f"{_vshort}",
    )

    print("\n[executable checks]")
    # ROOT CAUSE, not symptom. Every gate before this one read English and inferred
    # structure, and every one needed three rounds of false-positive repair. A check is now
    # a record that must be RUN before it may enter a plan. All four defects an independent
    # review found by hand are rejected here by construction.
    from vfx_harness.evidence.checks import Check as _C
    from vfx_harness.evidence.checks import verify as _verify

    _R = shot.folder / "refs"
    _corpus = sorted((shot.folder / "renders").glob("*_best.png"))
    _banded = "shots/barrel_roll/renders/5_best.png"  # the flat/banded render

    _v = _verify(
        _C(
            "pier",
            "region_ratio",
            "band",
            1.35,
            2.20,
            regions={"a": (0.44, 0.35, 0.50, 0.85), "b": (0.50, 0.35, 0.56, 0.85)},
        ),
        _R / "f440_final.jpg",
        _corpus,
    )
    check(
        "rule 1 rejects a target the reference cannot reach",
        not _v.ok and any("UNREACHABLE" in r for r in _v.reasons),
        f"{_v.reasons}",
    )

    _v = _verify(_C("gr", "green_excess", "<=", float("-inf"), 1.08), _R / "f440_final.jpg", _corpus)
    check(
        "...including the G/R check the plan had already caught once and repeated",
        not _v.ok and any("UNREACHABLE" in r for r in _v.reasons),
        f"{_v.reasons}",
    )

    _v = _verify(
        _C("floor", "region_mean", ">=", 14.0, float("inf"), regions={"r": (0.50, 0.35, 0.56, 0.85)}),
        _R / "f440_final.jpg",
        _corpus,
    )
    check(
        "rule 2 rejects a FLOOR aimed at a CEILING defect",
        not _v.ok and any("TOOTHLESS" in r for r in _v.reasons),
        f"{_v.reasons}",
    )

    # Rule 2 is only as strong as the negative it is graded against. Scoring a sky on band
    # sigma looked discriminating only because a LAYOUT render with no sky at all failed it,
    # while the banded render it actually targets sailed through. Naming the adversary is
    # the author's real work.
    _v = _verify(
        _C(
            "sky_sigma",
            "region_sigma",
            ">=",
            26.0,
            float("inf"),
            regions={"r": (0.0, 0.0, 1.0, 0.33)},
            rejects=[_banded],
        ),
        _R / "f001_open.jpg",
        _corpus,
        root=Path.cwd(),
    )
    check("a named adversary exposes a check that only looked discriminating", not _v.ok, f"{_v.reasons}")
    _v = _verify(
        _C("sky_aniso", "frame_aniso_top", "band", 0.6, 1.0, rejects=[_banded]),
        _R / "f001_open.jpg",
        _corpus,
        root=Path.cwd(),
    )
    check("...and the metric that genuinely separates that pair PASSES", _v.ok, f"{_v.reasons}")

    _v = _verify(_C("nofloor", "frame_mean", "band", 0.0, 255.0), _R / "f100_city.jpg", _corpus)
    check("a check naming no adversary is reported as WEAK", any("WEAK" in r for r in _v.reasons), f"{_v.reasons}")
    _v = _verify(_C("bogus", "no_such_metric", ">=", 1.0), _R / "f100_city.jpg", _corpus)
    check("an unknown metric is rejected at plan time, not mid-build", not _v.ok)

    # RULE 4 — the shipped spec must reproduce the proof recorded with it.
    # L3c-2 shipped a region measuring 18.50/13.01 while carrying "ref 23.74, adversary 5.67"
    # in free-text `note`: the planner tested one box and shipped another, and nothing tied
    # the two together. Moving the CHECK out of prose while leaving its PROOF in prose left
    # the last mile self-certified — the same shape as [unknown]=0 and zero source URLs.
    _adv = str(shot.folder.parent / "barrel_roll_v2/refs/layer2/f240_reveal_L2.png")
    _V2 = shot.folder.parent / "barrel_roll_v2"
    _shipped = _C(
        "L3c-2",
        "region_sigma",
        ">=",
        12.0,
        regions={"r": (0.05, 0.70, 0.95, 0.74)},
        rejects=[_adv],
        proof={"ref": 23.74, "adversary": [5.67]},
    )
    _v = _verify(_shipped, _V2 / "refs/f100_city.jpg", [], root=Path.cwd())
    check(
        "a spec that does not reproduce its own proof is rejected",
        not _v.ok and any("PROOF DOES NOT REPRODUCE" in r for r in _v.reasons),
        f"{_v.reasons}",
    )
    _tested = _C(
        "L3c-2b",
        "region_sigma",
        ">=",
        12.0,
        regions={"r": (0.05, 0.73, 0.95, 0.77)},
        rejects=[_adv],
        proof={"ref": 21.54, "adversary": [5.55]},
    )
    check(
        "...and the region actually tested passes every rule",
        _verify(_tested, _V2 / "refs/f100_city.jpg", [], root=Path.cwd()).ok,
    )

    # RULE 5 — a verdict that depends on exactly where the box was put is not a verdict.
    # Threshold threaded between ref 22.36 and adversary 21.36; a 1% nudge inverts them.
    _frag = _C("frag", "region_sigma", ">=", 21.86, regions={"r": (0.05, 0.56, 0.95, 0.59)}, rejects=[_adv])
    _v = _verify(_frag, _V2 / "refs/f100_city.jpg", [], root=Path.cwd())
    check(
        "a check whose verdict flips on a 1% region nudge is rejected",
        not _v.ok and any("FRAGILE" in r for r in _v.reasons),
        f"{_v.reasons}",
    )

    # The scaffold tripwire that lived here has been honoured: checks.json is live on
    # barrel_roll_v2 and eval/done_checks.py is deleted. grounding.py stays — acceptance.json
    # fingerprints are still prose and checks.json is a different artifact, so the tripwire's
    # own instruction to reduce it was over-specified. Removing scaffolding is only correct
    # for the part actually superseded.
    check("the prose done-check parser is gone", not (Path("src/vfx_harness/evaluation/done_checks.py")).exists())

    print("\n[completion gate]")
    # "Write the delta script before you finish" was prompt text, and prompt text is what
    # reads zero here: [unknown] used 0 times across four plan documents, ask_supervisor
    # never fired, no plan carried a source URL. A Stop hook is the same instruction as a
    # condition the harness evaluates, so "finished" stops being the model's own opinion.
    import tempfile as _tf2

    from vfx_harness.agents.guardrails import (
        builder_hooks,
        builder_phase_guard,
        compaction_notice,
        completion_gate,
    )
    from vfx_harness.orchestration.layer_state import load as _load_layer_state
    from vfx_harness.orchestration.layer_state import start as _start_layer_state

    _sf = Path(_tf2.mkdtemp())
    _phase = {"mode": "live"}
    _gate = completion_gate(_sf, "build/01_layout.py", _phase).hooks[0]
    _r = anyio.run(lambda: _gate({}, None, None))
    check("LIVE_BUILD can stop so the harness can enter finalize", not _r.get("decision"), f"{_r}")
    _phase["mode"] = "finalize"
    _r = anyio.run(lambda: _gate({}, None, None))
    check(
        "FINALIZE_SCRIPT blocks when the layer published no script",
        _r.get("decision") == "block" and "01_layout.py" in _r.get("reason", ""),
        f"{_r}",
    )
    (_sf / "build").mkdir(parents=True)
    (_sf / "build/01_layout.py").write_text("import bpy\n")
    check("...and allows finishing once it exists", not anyio.run(lambda: _gate({}, None, None)).get("decision"))
    _empty = completion_gate(_sf, "build/99_missing.py", _phase).hooks[0]
    check(
        "an empty file does not count as published",
        anyio.run(lambda: _empty({}, None, None)).get("decision") == "block",
    )

    _phase = {"mode": "live"}
    _phase_hook = builder_phase_guard(_phase, "build/01_layout.py").hooks[0]

    def _phase_call(tool):
        return (
            anyio.run(
                _phase_hook,
                {
                    "tool_name": tool,
                    "tool_input": {"file_path": str(_sf / "build/01_layout.py")},
                },
                None,
                None,
            )
            .get("hookSpecificOutput", {})
            .get("permissionDecision")
        )

    check("LIVE_BUILD cannot publish the script", _phase_call("Write") == "deny")
    _phase["mode"] = "finalize"
    check("FINALIZE_SCRIPT allows first publication", _phase_call("Write") is None)
    check("FINALIZE_SCRIPT does not patch before replay", _phase_call("Edit") == "deny")
    _phase["mode"] = "repair"
    check("REPAIR_SCRIPT blocks full replacement", _phase_call("Write") == "deny")
    check("REPAIR_SCRIPT allows a local patch", _phase_call("Edit") is None)
    _phase.update({"mode": "live", "scene_contracts_passed": True})
    check(
        "a passing live scene contract closes speculative mutation immediately",
        _phase_call("mcp__blender__run_bpy") == "deny",
    )
    _phase["scene_contracts_passed"] = False
    check("a critic-backed revision reopens live mutation", _phase_call("mcp__blender__run_bpy") is None)
    _live_opts = _builder_options(shot, {}, [], [], script_rel="build/01_layout.py")
    _final_opts = _script_options(shot, mode="finalize", script_rel="build/01_layout.py")
    _repair_opts = _script_options(shot, mode="repair", script_rel="build/01_layout.py")
    check(
        "LIVE_BUILD has no file mutation tools in its tool surface",
        {"Write", "Edit"} <= set(_live_opts.disallowed_tools)
        and not {"Write", "Edit"}.intersection(_live_opts.allowed_tools),
    )
    check(
        "FINALIZE_SCRIPT can publish but cannot patch",
        "Write" in _final_opts.allowed_tools and "Edit" in _final_opts.disallowed_tools,
    )
    check(
        "REPAIR_SCRIPT can patch but cannot replace",
        "Edit" in _repair_opts.allowed_tools
        and {"Write", "Glob"} <= set(_repair_opts.disallowed_tools),
    )
    check(
        "critic has enough structured-output protocol headroom",
        _critic_options(shot, [("composition", "framing")]).max_turns >= 3,
    )

    _start_layer_state(_sf, "1", [(1, "refs/frame.png")])
    _precompact = compaction_notice(_sf).hooks[0]
    _compact_result = anyio.run(_precompact, {"trigger": "auto"}, None, None)
    _compact_context = (_compact_result.get("hookSpecificOutput") or {}).get("additionalContext", "")
    check(
        "PreCompact durably checkpoints layer state",
        _load_layer_state(_sf).get("precompact", {}).get("trigger") == "auto",
    )
    check(
        "PreCompact continuation capsule preserves mode and plan authority",
        "LIVE_BUILD" in _compact_context and "shot-root plan.md" in _compact_context,
    )

    # A failed run_bpy can half-mutate the live scene while the only account of what was
    # attempted lives in a context window compaction will discard.
    _rec = builder_hooks(_sf, [_sf])["PostToolUseFailure"][0].hooks[0]
    anyio.run(
        lambda: _rec(
            {"tool_name": "run_bpy", "error": "boom", "tool_input": {"script": "bpy.ops.explode()"}}, None, None
        )
    )
    _fl = _ra.select(_sf).logs / "tool_failures.jsonl"
    check(
        "PostToolUseFailure durably records the failed call",
        _fl.is_file() and "run_bpy" in _fl.read_text() and "explode" in _fl.read_text(),
    )

    print("\n[import levels]")
    # Three `from .metrics` / `from .ledger` inside vfx_harness/agents/ — one level short, so
    # they resolve to vfx_harness.agents.metrics, which does not exist. All three sat in
    # function-local imports on paths that only run AFTER a layer passes (_ablate, the
    # metric report, the milestone reload), so nothing exercised them until layer 1 cleared
    # canonical replay and crashed on the step after it. The layer had done all its work and
    # was left unmarked. A lazy import is only checked when it fires; static analysis is.
    import ast as _ast2

    _leaf = {
        "metrics",
        "checks",
        "facade",
        "log",
        "brief",
        "ledger",
        "recipes",
        "prompts",
        "guardrails",
        "costlog",
        "resilience",
        "plan_tools",
        "sandbox",
        "runlog",
        "transcript",
        "escalate",
        "script_map",
        "preflight",
        "config",
    }
    _wrong = [
        f"{f.name}:{n.lineno} from .{n.module}"
        for f in Path("src/vfx_harness/agents").glob("*.py")
        for n in _ast2.walk(_ast2.parse(f.read_text()))
        if isinstance(n, _ast2.ImportFrom) and n.level == 1 and n.module in _leaf
    ]
    check("no agents/ module imports a top-level sibling at the wrong level", not _wrong, str(_wrong))

    # The accounting hole, found by using the accounting. costlog was hooked into
    # log_message, but the critic loop consumes messages with _structured_or_text and never
    # calls it — so critic sessions were never recorded, and rows that appeared under
    # role="critic" were whatever else finished while the bind was active. The one
    # experiment costlog existed for (is the critic's xhigh effort worth 70% of a layer?)
    # came back with a verdict comparison and no cost data at all.
    import inspect as _insp

    from vfx_harness.agents import builder as _bld

    check(
        "the critic loop records its own cost, not via log_message", "costlog.record" in _insp.getsource(_bld._critique)
    )

    print("\n[necessity]")
    # THE structural flaw: checks are authored by the stage with the LEAST information.
    # The planner writes every check before any work exists, from reference images alone —
    # so all 15 metrics compare pixels to a plate, 42 of 52 checks are post_grade, and the
    # layout layer got ONE check that cannot run at its own stage. Meanwhile layer 1
    # re-derived the hero's roof at NDC 0.89 and a mirrored roll ladder, and had nowhere to
    # record it. verify_necessity is the question only the builder can answer: does this
    # check pass on what I built AND fail on the state before I ran?
    from vfx_harness.evidence.checks import Check as _NC
    from vfx_harness.evidence.checks import verify_necessity

    _lit = shot.folder / "renders/5_best.png"  # a lit render
    _dark = shot.folder / "renders/1_best.png"  # the layout state before lighting

    # after 36.6 / before 32.3 — the band must sit BETWEEN them or it is not necessary.
    _c = _NC("L5-necessity", "frame_mean", ">=", 35.0, float("inf"))
    _v = verify_necessity(_c, _lit, _dark)
    check(
        "a check that holds after and fails before is NECESSARY",
        _v.ok,
        f"after {_v.ref_value} before {_v.bad_values} {_v.reasons}",
    )

    # The failure this catches: a check that was already true before the layer ran.
    _c2 = _NC("stolen", "frame_mean", ">=", 1.0, float("inf"))
    _v2 = verify_necessity(_c2, _lit, _dark)
    check(
        "a check that ALSO passes before the layer proves nothing about it",
        not _v2.ok and any("NOT NECESSARY" in r for r in _v2.reasons),
        f"{_v2.reasons}",
    )

    _c3 = _NC("wrong", "frame_mean", ">=", 250.0, float("inf"))
    _v3 = verify_necessity(_c3, _lit, _dark)
    check(
        "a check its own render fails does not describe what was built",
        not _v3.ok and any("DOES NOT HOLD" in r for r in _v3.reasons),
        f"{_v3.reasons}",
    )

    # Layer 1 has no prior layer; its adversary is the empty scene.
    check(
        "the first layer verifies with no prior state",
        verify_necessity(_NC("L1", "frame_mean", ">=", 1.0, float("inf")), _dark, None).ok,
    )

    print("\n[lit occupancy]")
    # The brief's FIRST anti-goal — "windows on a regular grid, identical spacing, one
    # colour" — shipped in layer 2 and scored 3. Nothing could see it: every metric here is
    # a histogram or an edge count, and a uniform lattice with varied per-cell brightness
    # satisfies region_sigma completely.
    #
    # The first fix measured spatial PERIODICITY by autocorrelation and was thrown away: the
    # references scored MORE periodic than the render (0.857/0.885 vs 0.689), because real
    # towers do have regular window columns. The anti-goal is about OCCUPANCY, not spacing.
    from vfx_harness.evidence.checks import Check as _LC
    from vfx_harness.evidence.checks import evaluate as _lev
    from vfx_harness.evidence.checks import noise_floor as _lnf

    _V2 = shot.folder.parent / "barrel_roll_v2"
    _lv = _LC("lit", "region_lit_variance", ">=", 0.0, regions={"r": (0.38, 0.60, 0.62, 0.95)})
    _ref = _lev(_lv, _V2 / "refs/f001_open.jpg")
    _ren = _lev(_lv, _V2 / "renders/2@f1_canonical_f1.png")
    check(
        "an evenly-lit lattice reads LOWER occupancy spread than a real facade",
        _ren < _ref * 0.75,
        f"render {_ren:.3f} vs ref {_ref:.3f}",
    )
    # The separation must beat the instrument, or it is a coin flip (rule 3).
    _fl = max(_lnf(_lv, _V2 / "refs/f001_open.jpg"), _lnf(_lv, _V2 / "renders/2@f1_canonical_f1.png"))
    check(
        "...by far more than the metric's own resampling noise",
        abs(_ref - _ren) > 10 * _fl,
        f"gap {abs(_ref - _ren):.4f} vs floor {_fl:.4f}",
    )
    check(
        "region_lit_variance is reachable from a check spec",
        "region_lit_variance" in __import__("vfx_harness.evidence.checks", fromlist=["x"]).METRICS,
    )
    # A metric in the registry that no tool description names is unreachable in practice —
    # the same shape as contact_sheet being defined and never registered. The vocabulary is
    # now GENERATED from METRICS so the two cannot drift apart again.
    from vfx_harness.agents.plan_tools import _metric_list as _ml
    from vfx_harness.evidence.checks import METRICS as _MET

    check(
        "every registered metric is advertised to the agents",
        all(k in _ml() for k in _MET),
        sorted(k for k in _MET if k not in _ml()),
    )

    print("\n[stale builder checks]")
    # A builder check is authored MID-layer against the render in front of it. A later
    # attempt rebuilds the scene and replaces every render, so a check proven in attempt 2
    # can describe a picture that no longer exists by attempt 3 — measured on layer 1:
    # three checks proven at 0.354 / 2.052 / 0.675 read 1.532 / 1.000 / 8.107 against the
    # final renders, and nothing distinguished that from a wrong check.
    from PIL import Image as _I3

    from vfx_harness.evidence.checks import revalidate_layer as _rv

    _sd = Path(_tf2.mkdtemp())
    (_sd / "renders").mkdir()
    _img = _sd / "renders" / "9@f1_canonical_f1.png"
    _I3.new("RGB", (128, 64), (200, 200, 200)).save(_img)
    (_sd / "runtime_checks.json").write_text(
        json.dumps(
            [
                {
                    "id": "holds",
                    "layer": "9",
                    "frame": 1,
                    "origin": "builder",
                    "metric": "frame_mean",
                    "op": ">=",
                    "lo": 100,
                    "ref": "refs/x.jpg",
                    "proof": {"ref": 200},
                },
                {
                    "id": "stale",
                    "layer": "9",
                    "frame": 1,
                    "origin": "builder",
                    "metric": "frame_mean",
                    "op": "<=",
                    "hi": 20,
                    "ref": "refs/x.jpg",
                    "proof": {"ref": 5},
                },
            ]
        )
    )
    (_sd / "checks.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "checks": [
                    {
                        "id": "planner",
                        "owner_layer": "9",
                        "fault_owner": "9",
                        "activates_at": "9",
                        "lifecycle": "layer",
                        "origin": "planner",
                        "metric": "frame_mean",
                        "op": ">=",
                        "lo": 999,
                        "ref": "refs/x.jpg",
                    },
                ],
            }
        )
    )
    _r = _rv(_sd, "9", lambda c: _img)
    check(
        "a stale builder check is dropped at layer end", [d[0] for d in _r["dropped"]] == ["stale"], f"{_r['dropped']}"
    )
    check("...one that still holds is kept", _r["kept"] == 1, f"{_r}")
    _left = {d["id"] for d in json.loads((_sd / "checks.json").read_text())["checks"]}
    check("...and a PLANNER check is never touched by this", "planner" in _left, f"{_left}")

    print("\n[cost attribution]")
    # run_layerN.json says layer 5 cost $18.63 across 11 turns and 9,082 output tokens —
    # MORE than layer 2's $9.77 across 37 turns. The aggregate said the expensive layer was
    # the one that barely ran and could not say why. It was not the builder: all 16 of
    # layer 5's rounds went to a best-of-three critic panel carrying four images each.
    # A row per session with its ROLE turns that from an inference into a groupby.
    from typing import ClassVar

    from vfx_harness.observability import costlog as _cl

    _cs = Path(_tf2.mkdtemp())

    class _R:  # the shape of a ResultMessage
        total_cost_usd, duration_ms, num_turns = 3.10, 41000, 2
        subtype, model = "success", "claude-opus-5"
        usage: ClassVar[dict] = {"output_tokens": 400, "cache_read_input_tokens": 540000}

    check("nothing is recorded when unbound", (_cl.record(_R()), True)[1] and not (_cs / "runs").exists())
    _cl.bind(_cs, role="critic", layer="5")
    _cl.record(_R())
    _cl.record(_R())
    _cl.record(_R())
    _cl.bind(_cs, role="builder", layer="5")
    _cl.record(_R())
    _cl.unbind()
    _cost_path = _ra.select(_cs).logs / "cost.jsonl"
    _rows = [json.loads(x) for x in _cost_path.read_text().splitlines()]
    check("a row is written per session, carrying its role", len(_rows) == 4, f"{len(_rows)}")
    check(
        "...and the role/layer labels survive",
        sum(1 for r in _rows if r["role"] == "critic" and r["layer"] == "5") == 3,
    )
    _sum = _cl.summarise(_cs)
    check(
        "the summary splits spend by role — the cut the aggregate could not give",
        "critic" in _sum and "builder" in _sum and "$12.40" in _sum,
        _sum,
    )
    _cs2 = Path(_tf2.mkdtemp())
    _cl.bind(_cs2, role="builder", phase="live_build", layer="1", run_id="R1", attempt=5)
    _cl.record(_R())
    with _cl.scoped(role="critic", phase="critic"):
        _cl.record(_R())
    with _cl.scoped(role="finalizer", phase="finalize_script"):
        _cl.record(_R())
    _cl.record(_R())
    _cl.unbind()
    _tot = _cl.attempt_totals(_cs2, run_id="R1", attempt=5)
    check(
        "nested phase binding restores the builder after critic/finalizer",
        _tot["sessions"] == 4 and _tot["by_role"] == {"builder": 6.2, "critic": 3.1, "finalizer": 3.1},
        str(_tot),
    )
    check(
        "attempt totals aggregate every phase instead of the last session",
        abs(_tot["cost_usd"] - 12.4) < 1e-9 and _tot["turns"] == 8,
        str(_tot),
    )
    _cs3 = Path(_tf2.mkdtemp())

    class _C(_R):
        def __init__(self, cost, turns):
            self.total_cost_usd, self.num_turns = cost, turns
            self.session_id = "same-streaming-session"

    _cl.bind(_cs3, role="builder", phase="live_build", layer="1", run_id="R2", attempt=1)
    _cl.record(_C(1.0, 4))
    _cl.record(_C(1.8, 7))
    _cl.unbind()
    _cumulative = _cl.attempt_totals(_cs3, run_id="R2", attempt=1)
    check(
        "cumulative ResultMessages from one streaming session are not double-counted",
        _cumulative["sessions"] == 1 and _cumulative["cost_usd"] == 1.8 and _cumulative["turns"] == 7,
        str(_cumulative),
    )
    check("a broken row never breaks the run", (_cl.bind(_cs, role="x"), _cl.record(object()), _cl.unbind(), True)[3])

    print("\n[resilience]")
    # Two repair rounds ($9 and 25 min each) were lost to a session that raised
    # "error result: success" at $0.0007 having written nothing. The real cause was in the
    # session's own text — "Repeated 529 Overloaded errors" — so the exception a caller sees
    # carries none of the information needed to decide what to do. And that shape is
    # indistinguishable from a spend-limit failure, which will fail identically forever.
    from vfx_harness.agents.resilience import classify, run_session

    for _t, _want in (
        ("Repeated 529 Overloaded errors. The API is at capacity", "transient"),
        ("connection reset by peer", "transient"),
        ("rate limit exceeded", "transient"),
        ("invalid x-api-key", "terminal"),
        ("Your credit balance is too low", "terminal"),
        ("You have reached your specified API usage limits", "terminal"),
        ("401 Unauthorized", "terminal"),
        ("TypeError: NoneType is not subscriptable", "unknown"),
    ):
        check(f"classify: {_want:<9} <- {_t[:38]}", classify(_t) == _want, classify(_t))
    # A terminal error mentioning a timeout is still terminal: retrying a bad credential
    # ten times is how it turns into half an hour of silence.
    check(
        "terminal wins over transient when both match",
        classify("401 Unauthorized (connection timed out)") == "terminal",
    )

    _n = {"i": 0}

    async def _flaky():
        _n["i"] += 1
        if _n["i"] < 3:
            return "API Error: Repeated 529 Overloaded errors"  # no exception, no output
        return "done"

    anyio.run(lambda: run_session(_flaky, succeeded=lambda: _n["i"] >= 3, label="t", attempts=4, base_delay=0.001))
    check("a transient failure is retried until the POST-CONDITION holds", _n["i"] == 3)

    _m = {"i": 0}

    async def _dead():
        _m["i"] += 1
        raise RuntimeError("invalid x-api-key")

    try:
        anyio.run(lambda: run_session(_dead, succeeded=lambda: False, label="t", base_delay=0.001))
        check("a terminal failure raises without retrying", False, "did not raise")
    except RuntimeError:
        check("a terminal failure raises without retrying", _m["i"] == 1, f"{_m['i']} tries")

    print("\n[plan gate · loop]")
    # The loop's stopping rule. Two rounds with the same findings means the repair pass
    # changed nothing that matters, and paying for the same answer again helps nobody.
    _a = _pg.GateResult("s", [_pg.Finding("citations", True, "x", "gone")])
    _b = _pg.GateResult("s", [_pg.Finding("citations", True, "x", "gone")])
    _c = _pg.GateResult("s", [_pg.Finding("citations", True, "y", "gone")])
    check("identical findings produce an identical signature (stall)", _a.signature() == _b.signature())
    check("different findings do not", _a.signature() != _c.signature())
    check(
        "a repair brief carries only blocking findings",
        "x" in _pg.feedback(_a) and not _pg.feedback(_pg.GateResult("s", [])),
    )

    from vfx_harness.agents.plan_tools import _CheckBatchBudget
    from vfx_harness.agents.planner import _planner_tool_policy

    _allow, _deny = _planner_tool_policy(True)
    check("plan repair gets Edit directly", "Edit" in _allow and "Edit" not in _deny)
    check("plan repair cannot delegate mechanical edits", {"Task", "Agent"} <= set(_deny))
    _allow, _deny = _planner_tool_policy(False)
    check("draft and verify can patch their transaction", "Edit" in _allow and "Edit" not in _deny)
    _budget = _CheckBatchBudget()
    check("two exploratory single checks are allowed", _budget.take_single() and _budget.take_single())
    check("a third single check is redirected to batching", not _budget.take_single())
    _budget.reset_after_batch()
    check("a batch reopens targeted exploration", _budget.take_single())

    print("\n[sandbox]")

    async def sb():
        chk = path_sandbox(shot.folder, cwd=shot.folder).hooks[0]

        async def dec(p):
            r = await chk({"tool_name": "Read", "tool_input": {"file_path": p}}, None, None)
            return r.get("hookSpecificOutput", {}).get("permissionDecisionReason", "ALLOW")

        return (
            await dec("/home/sahan/Desktop/vfx-harness/refs/f100_city.jpg"),
            await dec("/etc/passwd"),
            await dec(str(shot.folder / "brief.md")),
        )

    wrong, forbidden, ok = anyio.run(sb)
    check("wrong path -> redirect", wrong.startswith("WRONG PATH"))
    check("forbidden path -> hard deny", "outside this agent" in forbidden)
    check("own file allowed", ok == "ALLOW")
    check("no escape via absolute component", _relocate(Path("/etc/passwd"), [shot.folder]) is None)

    print("\n[guardrails]")

    async def gr():
        g = api_guardrails().hooks[0]
        w = web_allowlist().hooks[0]
        a = await g({"tool_name": "mcp__blender__run_bpy", "tool_input": {"script": "x.glare_type='B'"}}, None, None)
        b = await g(
            {"tool_name": "mcp__blender__run_bpy", "tool_input": {"script": "bvfx_glare_bloom(0.5,0.6,0.3)"}},
            None,
            None,
        )
        c = await w({"tool_name": "WebFetch", "tool_input": {"url": "https://news.ycombinator.com"}}, None, None)
        d_ = await w({"tool_name": "WebFetch", "tool_input": {"url": "https://docs.blender.org/api/"}}, None, None)
        return a, b, c, d_

    bad, good, offsite, docs = anyio.run(gr)
    check("blocks Blender-4 idiom", bad["hookSpecificOutput"]["permissionDecision"] == "deny")
    check("allows the helper", good == {})
    check("blocks off-domain web", offsite["hookSpecificOutput"]["permissionDecision"] == "deny")
    check("allows Blender docs", docs == {})

    # An error HINT teaches one SESSION; every layer is a fresh process, so bare next()
    # raised StopIteration in one run, was hinted and absorbed, then raised again in the
    # next run. A PreToolUse deny is the only form of help that crosses that boundary.
    from vfx_harness.agents.guardrails import script_sanity

    _ss = script_sanity().hooks[0]

    def _blocked(code):
        r = anyio.run(_ss, {"tool_name": "mcp__blender__run_bpy", "tool_input": {"script": code}}, None, None)
        return r.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    check("bare next() is blocked before it runs", _blocked("n = next(n for n in nt.nodes if n.type=='EMISSION')"))
    check("next(gen, None) is allowed", not _blocked("n = next((n for n in nt.nodes if n.type=='X'), None)"))
    check("bare next nested in another call is still caught", _blocked("print(len(next(g for g in gs if g)))"))
    check("a variable named next is not a call", not _blocked("next = 5\nprint(next)"))
    check("a syntax error never reaches Blender", _blocked("for i in range(3)\n    print(i)"))
    check("ordinary scripts pass untouched", not _blocked("import bpy\nbpy.ops.mesh.primitive_cube_add()"))
    check(
        "indexed bmesh faces are blocked without a prior lookup table",
        _blocked("import bmesh\nbm=bmesh.new()\nx=bm.faces[0]"),
    )
    check(
        "indexed bmesh faces are allowed after lookup-table initialization",
        not _blocked("import bmesh\nbm=bmesh.new()\nbm.faces.ensure_lookup_table()\nx=bm.faces[0]"),
    )
    check(
        "a lookup-table call after indexed access is too late",
        _blocked("import bmesh\nbm=bmesh.new()\nx=bm.faces[0]\nbm.faces.ensure_lookup_table()"),
    )

    from vfx_harness.agents.guardrails import execution_authority_guard

    _authority_root = Path(tempfile.mkdtemp())
    (_authority_root / "runtime_checks.json").write_text("[]\n", encoding="utf-8")
    (_authority_root / "plans").mkdir()
    (_authority_root / "plans/01.md").write_text("authority\n", encoding="utf-8")
    _authority_hook = execution_authority_guard(_authority_root, {"mode": "live"}).hooks[0]

    def _authority_decision(tool, path=None):
        payload = {"tool_name": tool, "tool_input": {}}
        if path is not None:
            payload["tool_input"]["file_path" if tool == "Read" else "path"] = path
        result = anyio.run(_authority_hook, payload, None, None)
        return result.get("hookSpecificOutput", {}).get("permissionDecision")

    check(
        "live builder cannot read the runtime evidence ledger",
        _authority_decision("Read", "runtime_checks.json") == "deny",
    )
    check("recursive live grep cannot leak the runtime evidence ledger", _authority_decision("Grep") == "deny")
    check(
        "live builder can still read its authoritative layer plan", _authority_decision("Read", "plans/01.md") is None
    )

    # Every run_bpy call is a FRESH NAMESPACE, which the system prompt states and the
    # builder ignored: it defined build_window_nodes in one call and used it in the next.
    # Under-reports on purpose — a false positive blocks legitimate work, which is worse
    # than a NameError the builder would see anyway.
    check("a helper from a previous run_bpy call is blocked", _blocked("m = build_window_nodes(mat, 8)"))
    check("a helper defined in THIS call is fine", not _blocked("def f(a):\n    return a\nx = f(1)"))
    check("injected bvfx helpers are in scope", not _blocked("bvfx_emissive_windows(o, density=8)"))
    check("dynamic binding disables the check rather than guessing", not _blocked("g = globals()\nmystery_fn(1)"))
    import ast as _a2
    import glob as _g2

    from vfx_harness.agents.guardrails import _undefined_names as _un

    _fp = [
        f
        for f in _g2.glob("shots/*/build/*.py") + _g2.glob("shots/*/logs/journals/*.py")
        if _un(_a2.parse(Path(f).read_text(encoding="utf-8")))
    ]
    check("no real build script trips the undefined-name check", not _fp, str(_fp[:3]))

    print("\n[script map]")
    tmp = Path(tempfile.mkdtemp()) / "s.py"
    tmp.write_text(
        "import bpy\ndef helper():\n    pass\no = bpy.data.objects.new('tower', None)\n"
        "m = bpy.data.materials.new('sky_mat')\nx = bpy.data.objects['prior_thing']\n"
    )
    o = outline(tmp)
    check("outline finds created names", "tower" in o and "sky_mat" in o)
    check("outline flags cross-layer refs", "prior_thing" in o)
    check("outline is far cheaper than the file", len(o) < len(tmp.read_text()) * 3)
    check("find_lines locates", "tower" in find_lines(tmp, "tower"))

    print("\n[recipes]")
    recs = _all()
    check("cookbook non-empty", len(recs) >= 20, f"{len(recs)}")
    check("index is compact", len(recipe_index()) // 4 < 1500)
    check("every recipe indexed", all(r["name"] in recipe_index() for r in recs if r["when"]))
    check(
        "search finds by intent", "night-city-field" in [h["name"] for h in search_recipes("make the city look real")]
    )
    filtered_index = recipe_index(context="camera barrel roll timing", limit=5)
    check("ticket-aware recipe index keeps the relevant discovery", "camera-roll-rig" in filtered_index, filtered_index)
    check("ticket-aware recipe index is bounded", filtered_index.count("\n  - ") <= 5)

    print("\n[builder context is ticket-aware]")
    from vfx_harness.agents.build_prompts import axes_own_look, builder_system, ticket_guidance_names

    cam_context = _builder_ticket_context(
        "Camera performs a barrel roll with smooth timing",
        "composition and motion",
        [("camera_motion", "stable framing during roll")],
    )
    cam_prompt = builder_system(
        [("camera_motion", "stable framing during roll")], recipe_index(context=cam_context), ticket_context=cam_context
    )
    full_prompt = builder_system([("camera_motion", "stable framing during roll")], recipe_index())
    check("camera tickets load camera guidance", ticket_guidance_names(cam_context) == ("camera/motion",))
    check(
        "camera prompt carries the rig but not unrelated cloud/facade lore",
        "bvfx_camera_rig" in cam_prompt
        and "DENSE ROLLING CLOUD" not in cam_prompt
        and "HERO SURFACES" not in cam_prompt,
    )
    check(
        "ticket selection materially reduces resident context",
        len(cam_prompt) < len(full_prompt) * 0.8,
        f"camera={len(cam_prompt)} full={len(full_prompt)}",
    )
    check(
        "coordinate contract is always resident",
        "SCREEN COORDINATE CONTRACT" in cam_prompt and "origin TOP-LEFT" in cam_prompt,
    )
    layout_prompt = builder_system([("composition", "layout silhouette and camera framing")], ticket_context="layout")
    check(
        "layout prompt forbids tuning appearance owned by later layers",
        "FORM/LAYOUT ONLY" in layout_prompt and "Do not tune lighting" in layout_prompt,
    )
    check(
        "negative look prose cannot invert explicit layout ownership",
        not axes_own_look([("layout_and_architecture", "placement only — not materials, not light, not level")]),
    )
    check(
        "runtime evidence is explicitly excluded from live build authority",
        "runtime_checks.json` is evaluation-only and MUST NOT be read" in layout_prompt,
    )

    print("\n[escalation]")
    q = Path(tempfile.mkdtemp())
    qid = ask(q, layer="PLAN", question="aspect?", assumption="2:1", global_decision=True)
    ask(q, layer="PLAN", question="aspect?", assumption="2:1", global_decision=True)
    check("duplicate suppressed", len(lq(q)) == 1)
    check("open question blocks", not lq(q)[0].get("answer"))
    answer(q, qid, "2:1")
    check("answer becomes law", "2:1" in answers_block(q))
    ask(q, layer="PLAN", question="timing?", assumption="ease", affected_layers=["3"], affected_axes=["animation"])
    _layout = type("Layer", (), {"id": "1", "owns": ("layout",)})()
    _anim = type("Layer", (), {"id": "3", "owns": ("animation",)})()
    check("a downstream question does not block an unrelated early layer", unanswered_for_layer(q, _layout) == [])
    check(
        "a question blocks the layer named by its impact metadata",
        [row["question"] for row in unanswered_for_layer(q, _anim)] == ["timing?"],
    )

    print("\n[layer context]")
    # Pick the layer by what it OWNS, not by id — ids shift whenever the stack is
    # restructured, and a test keyed on "layer 6" silently starts asserting about a
    # different layer instead of failing honestly.
    lay = next(l for l in layers.values() if "roll_and_blackout" in l.owns)
    fp_frame = lay.judges[0][0]
    p = write_layer_context(shot, lay, axes, {fp_frame: "mean 0.5"})
    body = p.read_text()
    check("names its layer", f"# Layer {lay.id}" in body)
    check("lists every judge frame", all(f"f{f}" in body for f, _ in lay.judges))
    check("carries fingerprints", "mean 0.5" in body)
    check("has summary instructions", "Summary instructions" in body)
    clear_layer_context(shot)
    check("clear_layer_context removes it", not (shot.folder / "CLAUDE.md").exists())

    print("\n[tools]")

    class S:
        pass

    _, names = build_blender_tools(S(), shot_dir=shot.folder, layer_id="1")
    short = [n.split("__")[-1] for n in names]
    check("render_frames registered", "render_frames" in short)
    check("transactional semantic control sweep registered", "probe_control" in short)
    check("script_map + worklist registered", {"script_map", "worklist"} <= set(short))
    check("ask_supervisor NOT in build tools", "ask_supervisor" not in short)
    import vfx_harness.blender.tools as T

    check("no undefined _encode", "_encode" not in Path(T.__file__).read_text())

    print("\n[observability]")
    from vfx_harness.observability.runlog import bump, reset_counts, snapshot_counts, summary

    reset_counts()
    bump("sandbox_denied", 2)
    check("hook counters accumulate", snapshot_counts() == {"sandbox_denied": 2})
    s_no = summary({"layer": "1", "title": "t", "status": "passed", "hooks": {}})
    check("silent hooks are flagged", "NOTHING FIRED" in s_no)
    s_nm = summary({"layer": "1", "title": "t", "status": "passed", "hooks": {"sandbox_denied": 1}})
    check("missing metric feedback is flagged", "NO objective metric feedback" in s_nm)
    s_ok = summary({"layer": "1", "title": "t", "status": "passed", "hooks": {"metric_feedback": 9}})
    check("healthy run is not flagged", "NO objective metric" not in s_ok)
    import ast as _ast

    def _mute(handler) -> bool:
        """Does this handler swallow the failure without anyone finding out?

        A handler is NOT mute if it logs, prints, re-raises — or RETURNS the problem to
        its caller, which several legitimately do (an is_error tool result, a "verdict
        INCONCLUSIVE" note, a list of problems). The first version of this check only
        looked for log/print/raise and so counted 53 handlers, most of which were
        reporting perfectly well through their return value. Flagging those trains
        everyone to ignore the check, which costs more than the handlers do.
        """
        src = _ast.unparse(handler)
        if any(k in src for k in ("log(", "print(", "raise", "bump(")):
            return False
        for n in _ast.walk(handler):
            # a return/append carrying an f-string or the exception name is a report
            if (
                isinstance(n, (_ast.Return, _ast.Assign))
                and _ast.unparse(n).count("e")
                and (
                    "JoinedStr" in str(type(getattr(n, "value", None)))
                    or "is_error" in _ast.unparse(n)
                    or "error" in _ast.unparse(n).lower()
                )
            ):
                return False
        return True

    # The hook path specifically: a hook that dies quietly is how metrics_feedback
    # no-opped for an entire build phase with no trace at all.
    crit = {"guardrails.py", "recipes.py", "sandbox.py", "runlog.py", "layer_state.py"}
    quiet = [
        f"{f.name}:{n.lineno}"
        for f in Path("src/vfx_harness").rglob("*.py")
        if f.name in crit
        for n in _ast.walk(_ast.parse(f.read_text()))
        if isinstance(n, _ast.ExceptHandler) and _mute(n)
    ]
    check("no silent handlers in hook code", not quiet, str(quiet))

    # The recipe frontmatter guard: enforced at the WRITE, not merely requested in the
    # distiller prompt. Prompt-only, a self-declared `verified: true` lands quietly and
    # fails a LATER suite run on a file nobody in that session meant to write.
    import anyio as _anyio

    from vfx_harness.agents.guardrails import recipe_write_guard

    _g = recipe_write_guard().hooks[0]

    def _denied(path, body):
        r = _anyio.run(_g, {"tool_name": "Write", "tool_input": {"file_path": path, "content": body}}, None, None)
        return r.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

    _fm = "---\nname: x\nverified: %s\n---\nbody\n"
    check("recipe claiming verified:true is blocked", _denied("src/vfx_harness/knowledge/recipes/x.md", _fm % "true"))
    check("recipe with verified:false is allowed", not _denied("src/vfx_harness/knowledge/recipes/x.md", _fm % "false"))
    check(
        "spike scaffolding in a recipe body is blocked",
        _denied("src/vfx_harness/knowledge/recipes/x.md", (_fm % "false") + "SPIKE_ARGS['a']=1\n"),
    )
    check("the guard does not touch non-recipe writes", not _denied("shots/b/build/01_layout.py", _fm % "true"))

    # And the handlers that were individually judged to lose information (#7).
    fixed = {
        "src/vfx_harness/blender/session.py": "artifact sweep failed",
        "src/vfx_harness/assets/_normalize_bpy.py": "normalize SKIPPED",
        "src/vfx_harness/knowledge/skills.py": "skills: skipping",
        "src/vfx_harness/orchestration/escalate.py": "is not valid JSON and was SKIPPED",
    }
    missing = [p for p, marker in fixed.items() if marker not in Path(p).read_text(encoding="utf-8")]
    check("degradations that lose data announce themselves", not missing, str(missing))

    # The mesh is a reconstruction OF the plate; nothing checked it still looked like it.
    # The cost of not checking was paid three layers later, as a critic demanding a
    # "stepped podium" at every frame with no way to tell whether the mesh lacked one or
    # the render was hiding it. Ratios only — the preview and the plate are framed
    # differently, so absolute widths are not comparable but base-flare/shaft is.
    print("\n[asset fidelity]")
    from vfx_harness.assets.normalize import compare_to_plate

    _plate = shot.folder / "assets/sr2_tower/isolated/view_0.png"
    _prev = shot.folder / "assets/sr2_tower/preview.png"
    if _plate.is_file() and _prev.is_file():
        _f = compare_to_plate(_plate, _prev)
        check(
            "the committed asset matches its design plate",
            _f["verdict"] == "consistent",
            f"{_f['verdict']}: {_f['note'][:80]}",
        )
        from PIL import Image as _I2

        _im = _I2.open(_prev).convert("L")
        _w, _h = _im.size
        _cut = Path(tempfile.mkdtemp()) / "nopodium.png"
        _im.crop((0, 0, _w, int(_h * 0.66))).resize((_w, _h)).save(_cut)
        check(
            "a mesh that lost its base massing is caught",
            compare_to_plate(_plate, _cut)["verdict"] == "mesh-lost-structure",
        )
    else:
        check("asset fidelity fixture present", False, "plate/preview missing")

    print("\n[ledger]")
    t2 = Path(tempfile.mkdtemp()) / "br"
    shutil.copytree(shot.folder, t2, ignore=shutil.ignore_patterns("refs", "assets", "renders"))
    s2 = load_shot(t2)
    m1 = Milestone("1", 1, "r", "")
    a, b = Ledger(s2), Ledger(s2)
    a.mark(m1, "passed")
    b.mark(Milestone("2", 2, "r", ""), "failed")
    keys = json.loads((t2 / "shot.json").read_text())["milestones"]
    check("concurrent writes do not clobber", {"1", "2"} <= set(keys))
    c = Ledger(s2)
    c.data["acceptance"] = {"run": 1}
    c.save()
    d2 = Ledger(s2)
    d2.data["acceptance"] = {"run": 2}
    d2.save()
    check("top-level key updates persist", json.loads((t2 / "shot.json").read_text())["acceptance"] == {"run": 2})

    # ---- BEGIN recipe-verification block ------------------------------------------
    # (added with the verify_recipes rewrite; self-contained, safe to move/merge)
    from vfx_harness.knowledge.verify_recipes import (
        LEDGER,
        SPIKES,
        _blocks,
        _static,
        audit,
        current_sha,
        load_ledger,
    )

    print("\n[recipe verification]")
    # The bug this whole tool exists to close: a call INSIDE a def is not evidence the def
    # ever ran. An injected 4.x API sat in an uncalled function and was reported "verified".
    defs, called, err = _static("def f(x):\n    bpy.thing()\n\ny = 1\n")
    check("a call inside a def is not a top-level call", (defs, called, err) == (["f"], [], None))
    defs, called, _ = _static("def f(x):\n    pass\n\nf(1)\n")
    check("a real top-level call is seen", (defs, called) == (["f"], ["f"]))
    check("a syntax error is reported, not swallowed", _static("def f(:\n")[2] is not None)

    body = (
        "prose\n```python\n  a = 1\n  b = 2\n```\n"
        "```python skip\nthis is not code at all\n```\n"
        "```python\nglare.glare_type = 'BLOOM'   # 4.x\n```\n"
    )
    blk = _blocks(body)
    check("indented fences are dedented", blk == ["a = 1\nb = 2\n"], repr(blk))
    check("skip + 4.x contrast fences are not executed", len(blk) == 1)

    recs = _all()
    ledger = load_ledger()
    check("spike ledger exists", LEDGER.is_file(), str(LEDGER))
    rows = audit(recs, ledger)
    liars = [r["name"] for r, entitled, _w in rows if r["verified"] and not entitled]
    check("no recipe claims verification without live spike evidence", not liars, str(liars))
    lying_low = [r["name"] for r, entitled, _w in rows if entitled and not r["verified"]]
    check("no proven recipe is left flagged unverified", not lying_low, str(lying_low))
    check(
        "every ledger entry names a real recipe",
        set(ledger) <= {r["name"] for r in recs},
        str(set(ledger) - {r["name"] for r in recs}),
    )
    stale = [n for n, e in ledger.items() if e.get("verdict") == "stale"]
    check("no recipe is stale against this Blender", not stale, str(stale))

    # editing a recipe must invalidate its evidence, or `verified:` decays into a rumour
    if recs and ledger:
        r0 = next((r for r in recs if r["verified"] and _blocks(r["body"])), None)
        if r0:
            tampered = dict(r0, body=r0["body"] + "\n```python\nx = 1\n```\n")
            check("an edited recipe loses its verified claim", not audit([tampered], ledger)[0][1])
            check("code hash changes with the code", current_sha(tampered) != current_sha(r0))

    # scaffolding must not leak into what find_recipe hands the builder
    check(
        "spike fixtures live outside the .md",
        not any(f.suffix == ".md" and f.name != "README.md" for f in SPIKES.glob("*")) if SPIKES.is_dir() else True,
    )
    check("no recipe body mentions SPIKE_ARGS", not [r["name"] for r in recs if "SPIKE_ARGS" in r["body"]])
    # ---- END recipe-verification block --------------------------------------------

    # ---- BEGIN eval-harness block (vfx_harness/evaluation/) ------------------------------
    # Self-contained, safe to move/merge. Added with A7.
    #
    # These test the INSTRUMENT, never the current state of the repo. A check that
    # asserted "artifact integrity passes for barrel_roll" would encode today's shot
    # folder into the suite: it would fail the moment someone starts a build, and it
    # would say nothing about whether the checker can see a problem. So every checker
    # here is pointed at a fixture whose answer is known by construction. What the repo
    # actually scores is a finding, and findings belong in `python -m vfx_harness.evaluation.cli
    # check`, not in a pass/fail suite.
    from vfx_harness.evaluation import baseline as EB
    from vfx_harness.evaluation import compare as EC
    from vfx_harness.evaluation import variance as EV
    from vfx_harness.evaluation.determinism import RENDER_SCALES, Result, metric_scale_consistency
    from vfx_harness.evaluation.integrity import _problems as integrity_problems

    print("\n[evals · baseline]")
    t3 = Path(tempfile.mkdtemp()) / "br"
    shutil.copytree(shot.folder, t3, ignore=shutil.ignore_patterns(".artifacts", ".snapshots", ".versions", "assets"))
    from vfx_harness.observability import run_artifacts as _run_artifacts
    _fixture_run = _run_artifacts.create(t3, "baseline-fixture")
    for _report in (t3 / "logs").glob("run_layer*.json"):
        _layer_id = _report.stem.removeprefix("run_layer")
        shutil.copyfile(_report, _fixture_run.reports / "layers" / f"layer-{_layer_id}.json")
    for _render in (t3 / "renders").glob("*"):
        if _render.is_file():
            shutil.copyfile(_render, _fixture_run.evidence / "renders" / _render.name)
    s3 = load_shot(t3)
    rec = EB.freeze(s3, label="unit", note="fixture")
    check("baseline names its shot and schema", rec["shot"] == "barrel_roll" and rec["schema"] == EB.SCHEMA)
    check("baseline carries per-layer telemetry", rec["layers"]["1"]["telemetry"]["cost_usd"] is not None)
    check(
        "baseline carries run_id + attempt for pairing",
        "run_id" in rec["layers"]["1"] and "attempt" in rec["layers"]["1"],
    )
    # Assert the PROPERTY (the body is stored verbatim), not a string that happens to be
    # in it. This originally checked for "import bpy" and passed only because one script
    # in the fixture contained it — build scripts do not import bpy, since the harness
    # injects it into the namespace. Moving an unrelated aborted script out of build/
    # removed the coincidence and the test failed while nothing was broken.
    check(
        "baseline stores script bodies, not pointers",
        bool(rec["scripts"])
        and all(
            s.get("text") == (t3 / name).read_text(encoding="utf-8") and s.get("text")
            for name, s in rec["scripts"].items()
            if (t3 / name).is_file()
        ),
    )
    check("baseline hashes every judged render", all(v for v in rec["renders"].values()) and len(rec["renders"]) > 0)
    # Absent acceptance must read as absent, never as zero: "0/10 passed" and "never
    # judged" support opposite conclusions and conflating them turns a partial build
    # into a quality regression.
    check("missing acceptance is 'unavailable', not 0", rec["final"]["present"] is False and "why" in rec["final"])
    check("baseline records code identity", rec["git"]["commit"] != "")
    (_fixture_run.evidence / "renders" / "zzz_new.png").write_bytes(b"not really a png")
    rec2 = EB.freeze(s3, label="unit2")
    check(
        "a new render shows up in a re-freeze",
        any(path.endswith("/zzz_new.png") for path in rec2["renders"])
        and not any(path.endswith("/zzz_new.png") for path in rec["renders"]),
    )

    print("\n[evals · integrity]")
    # Fixture with three planted defects, one of each class the checker claims to find.
    t4 = Path(tempfile.mkdtemp()) / "br"
    shutil.copytree(shot.folder, t4, ignore=shutil.ignore_patterns(".artifacts", ".snapshots", ".versions", "assets"))
    led = json.loads((t4 / "shot.json").read_text())
    led["milestones"]["1"]["rounds"][0]["render"] = "renders/vanished.png"
    # Plant the defect explicitly rather than relying on layer 3 happening to have no
    # script. It had none this morning and has one now, so the fixture silently stopped
    # planting anything and the check passed on repo state instead of on the property.
    led["milestones"]["3"] = {"status": "passed", "rounds": [{"round": 1, "render": ""}]}
    for _s in (t4 / "build").glob("03_*.py"):
        _s.unlink()
    (t4 / "build" / "99_experiment.py").write_text("# stray\n")
    (t4 / "shot.json").write_text(json.dumps(led, indent=2))
    errs, warns, data = integrity_problems(load_shot(t4))
    check("a vanished judged render is an ERROR", any("vanished.png" in e for e in errs), str(errs))
    check(
        "a 'passed' layer with no script on disk is an ERROR",
        any("layer 3" in e and "03_city.py" in e for e in errs),
        str(errs),
    )
    check("an orphan build script is reported", "99_experiment.py" in str(warns) or "99_experiment.py" in str(errs))
    check("orphans are listed in the data, not just prose", "99_experiment.py" in data.get("orphan_scripts", []))
    clean_errs, _w, _d = integrity_problems(load_shot(t3))
    check("the checker does not invent errors on an untouched shot", not clean_errs, str(clean_errs))

    print("\n[evals · metric self-consistency]")
    # The one invariant that holds both before and after the in-flight compare_frame fix:
    # scale 1.0 is the identity, so it cannot disagree with itself. Whether the SUB-1.0
    # scales agree is the open finding this check exists to report — asserting either
    # answer here would bake today's bug (or tomorrow's fix) into the suite.
    r_ident = metric_scale_consistency([Path(ref)], scales=(1.0,))
    check("identity scale is self-consistent", r_ident.ok is True, r_ident.detail)
    check("no images -> SKIP, not PASS", metric_scale_consistency([Path("/nonexistent.png")]).ok is None)
    # The sweep must model the PIPELINE's geometry: a render is scale × the delivery
    # resolution. The first version of this check swept already-downscaled 960x480
    # stashed renders by a further 0.25 — a scale of 0.125 that nothing renders at — and
    # reported "11 blocking failures". Overstating a finding is the same defect as
    # missing one, so a plate that cannot serve as a full-quality control is SKIPPED.
    small = Path(tempfile.mkdtemp()) / "half.png"
    from PIL import Image as _PILImage

    _PILImage.open(ref).resize((960, 480)).save(small)
    r_small = metric_scale_consistency([small], delivery=(1920, 960))
    check(
        "an under-resolution plate is skipped, not judged",
        r_small.ok is None and "960x480" in r_small.detail,
        r_small.detail[:120],
    )
    r_mixed = metric_scale_consistency([Path(ref), small], delivery=(1920, 960))
    check(
        "the full-res plate is still swept when a small one is dropped",
        r_mixed.ok is not None and "half.png" in str(r_mixed.data.get("skipped")),
    )
    r_all = metric_scale_consistency([Path(ref)], scales=RENDER_SCALES, delivery=shot.resolution)
    print(
        f"    (informational, not a check) {Path(ref).name} across "
        f"{list(RENDER_SCALES)}: {r_all.state} — {r_all.detail.splitlines()[0][:110]}"
    )
    check("a skipped check never reads as a pass", Result("x", ok=None, detail="").state == "SKIP")

    print("\n[evals · statistics]")
    # The whole point of the compare stage is refusing to call small differences results.
    check("no discordant pairs -> p=1", EC.sign_test_p(0, 0) == 1.0)
    check("one flip is not significant", EC.sign_test_p(1, 0) > 0.05)
    check("5 one-way flips still are not", EC.sign_test_p(5, 0) > 0.05)
    check("6 one-way flips are", EC.sign_test_p(6, 0) <= 0.05)
    check(
        "the design states its own resolution",
        EC.min_discordant_for_significance() == 6,
        str(EC.min_discordant_for_significance()),
    )
    check("the test is symmetric", EC.sign_test_p(2, 5) == EC.sign_test_p(5, 2))

    print("\n[evals · compare]")
    import copy as _copy

    ba = EB.freeze(s3, label="A")
    bb = _copy.deepcopy(ba)
    bb["label"] = "B"
    txt = EC.report(ba, bb)
    check(
        "final task success leads the report",
        txt.index("PRIMARY: FINAL TASK SUCCESS") < txt.index("layer verdicts") < txt.index("telemetry only"),
    )
    check("no acceptance -> the primary statistic is UNAVAILABLE", "UNAVAILABLE" in txt)
    check("secondary statistics are labelled gameable", "GAMEABLE" in txt)
    check("cost is labelled telemetry, not quality", "NEVER a quality argument" in txt)

    def _acc(flags):
        return {
            "present": True,
            "passed": sum(flags.values()),
            "total": len(flags),
            "moments": {
                k: {
                    "frame": 1,
                    "pass": v,
                    "critic_pass": v,
                    "mean": 3.5,
                    "decided_by": "critic",
                    "metric_failures": [],
                    "scores": {},
                    "render": "",
                    "render_sha": None,
                }
                for k, v in flags.items()
            },
        }

    ids = [f"M{i}" for i in range(1, 11)]
    ba["final"] = _acc(dict.fromkeys(ids, True))
    one = dict.fromkeys(ids, True)
    one["M9"] = False
    bb["final"] = _acc(one)
    txt1 = EC.report(ba, bb)
    check("a single moment flip is reported as noise, not a delta", "WITHIN NOISE" in txt1 and "NOT a result" in txt1)
    big = dict.fromkeys(ids, True)
    for m in ids[:7]:
        big[m] = False
    bb["final"] = _acc(big)
    txt2 = EC.report(ba, bb)
    check("a 7-moment regression IS called a regression", "REGRESSED" in txt2 and "WITHIN NOISE" not in txt2)
    bb["final"] = _acc(dict.fromkeys(ids[:5], True))
    txt3 = EC.report(ba, bb)
    check("a changed acceptance suite is flagged as unpaired", "acceptance suites DIFFER" in txt3)
    ba["shot"], bb["shot"] = "a", "b"
    check("comparing two different shots is refused loudly", "DIFFERENT SHOTS" in EC.report(ba, bb))

    print("\n[evals · variance]")
    d = EV._dispersion([2.0, 4.0, 3.0, 3.0])
    check("dispersion reports the spread that flips verdicts", d["spread"] == 2.0 and d["median"] == 3.0)
    fake = {
        "at": "2026-01-01T00:00:00+00:00",
        "shot": "x",
        "render": "r",
        "ref": "f",
        "layer": "1",
        "scope": "layer",
        "n": 3,
        "critic_model": "m",
        "motion_strip": False,
        "axes_in_rubric": ["a"],
        "per_axis": {"a": {**EV._dispersion([2.0, 3.0, 4.0]), "n_a": 0, "scope_unstable": False}},
        "mean": EV._dispersion([2.0, 3.0, 4.0]),
        "verdicts": [],
        "pass_count": 2,
        "flip_rate": 0.33,
        "unanimous": False,
        "thresholds": {"PASS_MEAN": 3.1, "PASS_MIN": 2, "ADJUDICATE_BAND": 0.4},
        "band_evidence": 1.0,
        "enough_for_band": False,
    }
    rep = EV.report(fake)
    check("a low-N run refuses to recommend a band", "TOO SMALL TO RECOMMEND A BAND" in rep)
    check("the no-motion-strip proxy is declared in the output", "NO strip" in rep)
    fake["n"], fake["enough_for_band"] = 12, True
    check(
        "a sufficient-N run quotes a band AND its caveat",
        "should be AT LEAST" in EV.report(fake) and "ONE render/reference pair" in EV.report(fake),
    )
    # Drift tripwire: variance.layer_scope MIRRORS the scope block build_layer builds
    # inline. If build_agent's wording moves, the eval silently starts measuring the
    # critic under a prompt production never sends.
    ba_src = Path("src/vfx_harness/agents/builder.py").read_text(encoding="utf-8")
    sc = EV.layer_scope(_strict_shot, _strict_layers["1"])
    check(
        "layer scope mirrors build_agent's block",
        all(mark in ba_src and mark in sc for mark in ("THIS LAYER OWNS:", "one build stage of many")),
        sc[:120],
    )
    check("layer scope names the layer's own axes", all(a in sc for a in _strict_layers["1"].owns))
    # The band stopped being a constant when it became granularity-aware, and this
    # module's import of the old flat `_ADJUDICATE_BAND` was never updated — so the ONE
    # module whose job is to re-measure judge noise could not be imported at all, and
    # `evals variance` died before it scored anything. Import it the way `measure` does.
    from vfx_harness.agents.builder import _adjudicate_band as _ab

    check(
        "variance can import the band it reports",
        callable(_ab) and _ab(1) >= _ab(8),
        "the band must NARROW as axis count rises",
    )
    check(
        "no stale flat-constant import survives",
        "import"
        not in [
            ln
            for ln in Path("src/vfx_harness/evaluation/variance.py").read_text(encoding="utf-8").splitlines()
            if "_ADJUDICATE_BAND" in ln and "#" not in ln.split("_ADJ")[0]
        ]
        or not any(
            "from ..agents.builder import" in ln and "_ADJUDICATE_BAND" in ln
            for ln in Path("src/vfx_harness/evaluation/variance.py").read_text(encoding="utf-8").splitlines()
        ),
    )

    print("\n[evals · ICC(2,1)]")
    from vfx_harness.evaluation.icc import icc_2_1

    # Raters that agree on how the targets RANK → high ICC.
    agree = icc_2_1([[4, 4, 4], [3, 3, 3], [1, 1, 1]])
    # Raters that disagree completely on the same targets → near zero or below.
    disagree = icc_2_1([[4, 1, 3], [1, 4, 2], [3, 2, 4]])
    check("ICC is high when raters agree on the ranking", agree["ok"] and agree["icc"] > 0.9, str(agree.get("icc")))
    check("ICC collapses when raters disagree", disagree["ok"] and disagree["icc"] < 0.4, str(disagree.get("icc")))
    check(
        "ICC reports the between/within split, not just a coefficient",
        {"between", "within", "BMS", "EMS"} <= set(agree),
    )
    # One target cannot produce an ICC — there is no between-target variance to
    # compare against. Refusing is the only honest answer; returning a number here is
    # how a repeat-only sample gets quoted as a reliability coefficient.
    lone = icc_2_1([[3, 4, 3]])
    check(
        "a single target REFUSES rather than inventing a coefficient", not lone["ok"] and lone["icc"] is None, str(lone)
    )
    check("ragged input is refused", not icc_2_1([[1, 2], [1]])["ok"])

    print("\n[evals · blank-frame control]")
    from vfx_harness.evaluation import blank as EBL

    check("an axis scoring 2.0 on black is called a language prior", EBL.language_prior({"values": [2.0, 2.0]}))
    check("an axis that collapses on black is NOT flagged", not EBL.language_prior({"values": [0.0, 1.0]}))
    check("no values means no claim either way", not EBL.language_prior({"values": []}))
    brep = EBL.report(
        {
            "shot": "x",
            "ref": "refs/f.jpg",
            "layer": "5",
            "scope": "layer",
            "mean": 2.0,
            "pass": False,
            "per_axis": {"lighting_and_form": {"values": [2.0], "n_a": 0, "language_prior": True}},
            "language_prior_axes": ["lighting_and_form"],
            "gate": "Phase 2 is premature for: lighting_and_form",
        }
    )
    check(
        "the blank-frame report names the axis AND what it implies",
        "LANGUAGE PRIOR" in brep and "rubric problem" in brep,
    )
    # ---- END eval-harness block ---------------------------------------------------

    # ---- BEGIN judgment-free checks block (vfx_harness/blender/geom.py + blender/checks.py)
    # The arithmetic is tested here; that the checks FIRE on a broken scene is tested
    # in Blender by `python -m vfx_harness.evaluation.cli checks`, because a check nobody has
    # watched fail is not a check.
    print("\n[geometry · motion]")
    from vfx_harness.blender.geom import framing_from_ndc, mesh_issues, motion_from_positions, scale_issues

    # A→B→A in three frames: the classic "it moved and came back" that reads as motion
    # in a still and as a broken move in the curve.
    m_bad = motion_from_positions([1, 2, 3], [(0, 0, 0), (4, 0, 0), (0, 0, 0)])
    check("a reversing path is not one unbroken move", not m_bad["unbroken"])
    check(
        "peak speed is attributed to a FRAME, not just a value",
        m_bad["peak_speed_frame"] == 2,
        str(m_bad["peak_speed_frame"]),
    )
    m_ok = motion_from_positions([1, 2, 3, 4], [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)])
    check(
        "a constant-velocity move is unbroken with zero accel",
        m_ok["unbroken"] and m_ok["max_accel"] == 0.0 and m_ok["max_speed"] == 1.0,
    )
    m_held = motion_from_positions(
        [1, 12, 24, 30, 36, 40, 80, 120],
        [(0, 0, 0), (0, 0, 0), (0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0), (3, 0, 0), (3, 0, 0)],
    )
    check(
        "intentional hold-move-hold choreography is one unbroken move",
        m_held["unbroken"]
        and m_held["active_frame_span"] == [24, 40]
        and m_held["leading_hold_segments"] == 2
        and m_held["trailing_hold_segments"] == 2,
        str(m_held),
    )
    m_restart = motion_from_positions(
        [1, 2, 3, 4, 5],
        [(0, 0, 0), (1, 0, 0), (2, 0, 0), (2, 0, 0), (3, 0, 0)],
    )
    check("a stop and restart inside the active interval still fails", not m_restart["unbroken"])
    # Non-uniform frame gaps are the normal case for a judge-frame list, so speed must
    # be per FRAME and not per sample — otherwise a strip of [1, 24, 48] reports a
    # 24x-too-large speed and every travel target is met by accident.
    m_gap = motion_from_positions([1, 25], [(0, 0, 0), (24, 0, 0)])
    check("speed is per frame, not per sample", m_gap["max_speed"] == 1.0, str(m_gap["max_speed"]))
    check("one sample cannot yield a velocity", not motion_from_positions([1], [(0, 0, 0)])["ok"])
    try:
        motion_from_positions([2, 1], [(0, 0, 0), (1, 0, 0)])
        check("non-increasing frames are refused", False)
    except ValueError:
        check("non-increasing frames are refused", True)

    print("\n[geometry · framing]")
    # world_to_camera_view: x,y in 0..1 on screen, z>0 in front of the camera.
    on = framing_from_ndc([(0.4, 0.4, 5.0), (0.6, 0.7, 5.0)])
    check(
        "an on-screen bbox reports width, height and centre",
        on["on_screen"] == 1.0
        and on["width"] == 0.2
        and on["bbox"] == [0.4, 0.3, 0.6, 0.6]
        and on["centre"] == [0.5, 0.45],
        str(on),
    )
    off = framing_from_ndc([(1.8, 0.4, 5.0), (2.0, 0.7, 5.0)])
    check("an off-screen bbox reports 0% on screen", off["on_screen"] == 0.0)
    behind = framing_from_ndc([(0.5, 0.5, -3.0)])
    check("geometry BEHIND the camera is not counted as framed", behind["on_screen"] == 0.0, str(behind))

    print("\n[geometry · mesh + scale]")
    check("non-manifold geometry is reported", any("non-manifold" in i for i in mesh_issues({"nonmanifold_edges": 6})))
    check("a second island is reported", any("island" in i for i in mesh_issues({"islands": 2})))
    check(
        "a clean mesh reports nothing",
        mesh_issues(
            {"nonmanifold_edges": 0, "islands": 1, "degenerate_faces": 0, "loose_verts": 0, "poles": 0, "ngons": 0}
        )
        == [],
    )
    check("unapplied scale is reported", scale_issues((4.0, 4.0, 4.0)))
    check("applied scale is silent", scale_issues((1.0, 1.0, 1.0)) == [])

    print("\n[checks · the report reads as a finding]")
    from vfx_harness.blender.tools import _check_report

    rep = _check_report(
        "motion",
        {
            "ok": False,
            "max_speed": 4.66,
            "max_accel": 0.39,
            "max_jerk": 0.0,
            "unbroken": False,
            "peak_speed_frame": 24,
            "issues": ["Cam path reverses or stops mid-move"],
        },
    )
    check("issues come FIRST and the numbers follow", rep.index("reverses") < rep.index("4.66") and "ISSUES" in rep)
    rep_ok = _check_report(
        "mesh", {"ok": True, "counts": {"verts": 8, "edges": 12, "faces": 6, "islands": 1}, "issues": []}
    )
    check("a passing check still states the measured record", "PASS" in rep_ok and "8 verts" in rep_ok)
    rep_bb = _check_report(
        "bbox", {"ok": True, "bbox": [0.4, 0.2, 0.6, 0.9], "width": 0.2, "height": 0.7, "centre": [0.5, 0.55]}
    )
    check("the bbox report says what to DO with the box", "render_pass" in rep_bb)
    check("the bbox report declares the shared top-left origin", "TOP-LEFT" in rep_bb)

    print("\n[render modes · every mode ships a caption]")
    # A visual channel with no caption measured WORSE than not adding the channel at
    # all (−10.6pp alone vs +7.7pp with text), so a mode with no caption is a
    # regression, not a missing nicety.
    import importlib.util as _ilu

    _sp = _ilu.spec_from_file_location("_rext", "src/vfx_harness/blender/render_ext.py")
    _rext = _ilu.module_from_spec(_sp)
    _sp.loader.exec_module(_rext)
    for _pass in ("beauty", "diffuse_direct", "emit", "shadow", "ao", "normal", "depth", "crypto"):
        cap = _rext.caption_for(_pass, "beauty", None, None, None)
        check(f"caption for pass {_pass}", len(cap) > 30 and cap.strip() != "")
    check(
        "the diffuse_direct caption says emission must be absent",
        "mission" in _rext.caption_for("diffuse_direct", "beauty", None, None, None),
    )
    for _shade in ("clay", "silhouette", "matcap:check_normal+y"):
        check(f"caption for shade {_shade}", len(_rext.caption_for("beauty", _shade, None, None, None)) > 30)
    cap = _rext.caption_for("beauty", "beauty", "key", [0.1, 0.2, 0.3, 0.4], 400)
    check(
        "a light group, a crop and a zoom are all declared in the caption",
        "key" in cap and "crop" in cap and "400" in cap,
    )
    check("crop captions use the public top-left convention", "top-left" in cap and "bottom-left" not in cap)

    class _RenderSettings:
        use_border = False
        use_crop_to_border = False
        border_min_x = border_min_y = 0.0
        border_max_x = border_max_y = 1.0
        resolution_percentage = 100

    class _Scene:
        render = _RenderSettings()

    _crop_scene = _Scene()
    _rext._apply_crop(_crop_scene, [0.1, 0.2, 0.3, 0.4], 400, 0.5)
    check(
        "top-left crop converts only at Blender's border boundary",
        _crop_scene.render.border_min_x == 0.1
        and _crop_scene.render.border_max_x == 0.3
        and _crop_scene.render.border_min_y == 0.6
        and _crop_scene.render.border_max_y == 0.8,
    )
    # Socket names were MEASURED on 5.2 (Diffuse Direct / Emission, not DiffDir / Emit).
    # Getting these wrong renders a perfect copy of the beauty frame under a caption
    # promising isolation — the worst possible outcome, so pin them.
    check(
        "the 5.x pass socket names are the ones that exist",
        _rext._PASS_SOCKETS["diffuse_direct"][0] == "Diffuse Direct" and _rext._PASS_SOCKETS["emit"][0] == "Emission",
    )

    print("\n[display size vs metric size]")
    import vfx_harness.blender.tools as _T

    # These answer DIFFERENT questions and the reasoning for one has leaked into the
    # other before. Metric height must stay low (never upscale a render); display
    # height must clear the critic's 28px patch grid by enough that a facade pier is
    # more than a fifth of one patch.
    check("metric height is unchanged and still below every accepted render", _T._METRIC_H == 320)
    check("display height clears several patch rows", _T._DISPLAY_H >= 1024, f"{_T._DISPLAY_H}")
    check("display is never the size metrics are taken at", _T._DISPLAY_H != _T._METRIC_H)
    _tools_src = Path(_T.__file__).read_text(encoding="utf-8")
    check("the two constants carry the reason they differ", "28x28" in _tools_src or "patch" in _tools_src)
    # ---- END judgment-free checks block --------------------------------------------

    print("\n[canonical repair · what a round achieved]")
    from vfx_harness.agents.builder import _repair_delta

    def vs(*frames):
        """(frame, mean, pass) triples -> the verdict shape _repair_delta consumes."""
        return [((f, f"r{f}.png"), {"mean": m, "pass": p}) for f, m, p in frames]

    # THE LAYER-2 CASE, and the reason this function exists. The improving frame is NOT
    # the binding one: f440 lifts 2.0 -> 2.75 while f200 sits at 1.0. The old test summed
    # the failing frames, saw the total rise, and bought another repair round — but the
    # layer passes only when its WORST frame clears the bar, and that frame did not move.
    # (An earlier draft of this test used the worst frame as the improving one, which is
    # genuine progress and rightly returns True. The defect needs a non-binding frame.)
    pre = vs((45, 3.0, True), (100, 3.0, True), (200, 1.0, False), (440, 2.0, False))
    post = vs((45, 3.0, True), (100, 3.0, True), (200, 1.0, False), (440, 2.75, False))
    d = _repair_delta(pre, post)
    check(
        "a rising SUM with a static worst frame is not progress",
        not d["progressed"],
        f"worst {d['was_worst']}->{d['now_worst']}",
    )
    check(
        "  ... and the sum really did rise (the old test would have passed it)",
        sum(d["now"][f] for f in (200, 440)) > sum(d["was"][f] for f in (200, 440)),
    )

    # The worst frame moving IS progress, even when the total is unchanged.
    d = _repair_delta(vs((200, 1.0, False), (440, 3.0, False)), vs((200, 2.0, False), (440, 2.0, False)))
    check("the worst failing frame improving is progress", d["progressed"], f"worst {d['was_worst']}->{d['now_worst']}")

    # So is clearing a frame outright, even if the remaining worst is untouched.
    d = _repair_delta(vs((200, 2.0, False), (440, 2.0, False)), vs((200, 3.0, True), (440, 2.0, False)))
    check("one fewer failing frame is progress", d["progressed"], f"{d['was_failing']}->{d['now_failing']} failing")

    # Regression detection is independent of progress: a repair can lift the worst
    # failing frame AND break a passing one. The caller reverts on `broke` first.
    d = _repair_delta(vs((45, 4.0, True), (440, 1.0, False)), vs((45, 2.0, False), (440, 3.0, True)))
    check("a trade against a passing frame is reported as broken", d["broke"] == [45], str(d["broke"]))
    check(
        "a regressed first repair rolls back and uses the remaining attempt",
        _repair_action(d, attempt=1, max_attempts=2) == "rollback_retry",
    )
    check(
        "a regressed final repair rolls back and stops",
        _repair_action(d, attempt=2, max_attempts=2) == "rollback_stop",
    )

    check(
        "nothing broken when every passing frame holds",
        _repair_delta(vs((45, 4.0, True), (440, 1.0, False)), vs((45, 4.0, True), (440, 2.0, False)))["broke"] == [],
    )
    _accepted_delta = _repair_delta(
        vs((45, 4.0, True), (440, 1.0, False)),
        vs((45, 4.0, True), (440, 2.0, False)),
    )
    check("a monotonic repair remains committed", _repair_action(_accepted_delta, 1, 2) == "accept")
    _rejected_summary = _repair_change_summary("energy = 10\n", "energy = 20\n")
    check(
        "the next repair receives the rejected script delta",
        "-energy = 10" in _rejected_summary and "+energy = 20" in _rejected_summary,
        _rejected_summary,
    )

    # A frame absent from the post-repair verdicts has no score to compare. Scoring its
    # absence as 0 would read as a regression and mask the real state.
    d = _repair_delta(vs((200, 2.0, False), (440, 2.0, False)), vs((200, 3.0, True)))
    check(
        "a frame missing after repair is not scored as zero",
        d["now_worst"] == 3.0 and d["broke"] == [],
        f"worst {d['now_worst']}",
    )

    _contract_pre = [
        (
            (1, "r1.png"),
            {"mean": 2.0, "pass": False, "evidence": [{"id": "ring", "authoritative": True, "pass": False}]},
        )
    ]
    _contract_post = [
        ((1, "r1.png"), {"mean": 2.0, "pass": False, "evidence": [{"id": "ring", "authoritative": True, "pass": True}]})
    ]
    d = _repair_delta(_contract_pre, _contract_post)
    check(
        "clearing an executable contract is progress even when critic score is flat",
        d["progressed"] and d["was_contract_failures"] == 1 and d["now_contract_failures"] == 0,
        str(d),
    )

    print("\n[tool usage is recorded, and measuring-instead-of-looking is flagged]")
    # Answering "which tools did the builder use" required grepping a console log that
    # only survived by luck — three of the four had already been cleaned. The counts now
    # live in the run report. Fixtures below are the REAL profiles from barrel_roll.
    from vfx_harness.observability.log import TOOL_USE, reset_tool_use, tool_use_summary
    from vfx_harness.observability.runlog import summary as _rsum

    _conflict_summary = _rsum(
        {
            "layer": "1",
            "title": "layout",
            "status": "judge_conflict",
            "canonical": [{"frame": 1, "mean": 2.0, "pass": False, "judge_conflict": True}],
        }
    )
    check(
        "run reports distinguish judge conflict from a broken replay",
        "JUDGE_CONFLICT" in _conflict_summary and "judge-conflict" in _conflict_summary,
        _conflict_summary,
    )
    _revalidation_summary = _rsum(
        {
            "layer": "1",
            "title": "layout",
            "status": "passed",
            "revalidation": True,
            "canonical": [{"frame": 1, "mean": 4.0, "pass": True, "decided_by": "deterministic_revalidation"}],
        }
    )
    check(
        "deterministic revalidation does not invent missing-hook/feedback warnings",
        "NOTHING FIRED" not in _revalidation_summary and "NO objective metric feedback" not in _revalidation_summary,
        _revalidation_summary,
    )
    _layout_summary = _rsum(
        {
            "layer": "1",
            "title": "layout",
            "status": "passed",
            "tools": {"total": 3, "look_feedback_applicable": False, "adoption": {}, "unused_required_tools": []},
            "hooks": {"automatic_scene_contract_probe": 3},
        }
    )
    check(
        "intentional layout metric suppression is not reported as missing feedback",
        "NO objective metric feedback" not in _layout_summary and "metric feedback n/a" in _layout_summary,
        _layout_summary,
    )
    reset_tool_use()
    check("no tool calls -> no telemetry", tool_use_summary() == {})
    TOOL_USE["mcp__blender__compare_frame"] = 4
    TOOL_USE["mcp__blender__measure_regions"] = 32
    TOOL_USE["mcp__blender__render_frame"] = 14
    s5 = tool_use_summary()
    check(
        "counts per tool and separates looking from measuring",
        s5["compared"] == 4 and s5["measured"] == 32 and s5["looked"] == 18,
        str(s5),
    )
    check(
        "look_per_measure below 1 when it measures more than it looks",
        s5["look_per_measure"] < 1.0,
        str(s5.get("look_per_measure")),
    )
    check("reset clears it between layers", (reset_tool_use(), tool_use_summary())[1] == {})
    TOOL_USE["mcp__blender__run_bpy"] = 4
    _automatic = tool_use_summary(automatic_scene_checks=4, look_feedback_applicable=False)
    check(
        "automatic scene-contract probes count as verification adoption",
        _automatic["verified"] == 4
        and _automatic["adoption"]["check_scene"] == 4
        and "check_scene" not in _automatic["unused_required_tools"],
        str(_automatic),
    )
    reset_tool_use()
    TOOL_USE["mcp__blender__run_bpy"] = 2  # one denied before execution, one accepted
    _one_real_mutation = tool_use_summary(automatic_scene_checks=1)
    check(
        "a pre-execution denial cannot make change-diff tools applicable",
        not _one_real_mutation["applicability"]["diff_frames"]
        and not _one_real_mutation["applicability"]["verify_change"],
        str(_one_real_mutation),
    )
    reset_tool_use()

    def _rec(tools):
        return {"layer": "x", "title": "t", "status": "failed", "rounds": [], "canonical": [], "tools": tools}

    # Layer 5 (failed 3x): measured 32, looked 18.
    warn5 = "MEASURED MORE THAN IT LOOKED" in _rsum(
        _rec(
            {
                "calls": {"mcp__blender__measure_regions": 32},
                "total": 93,
                "looked": 18,
                "measured": 32,
                "compared": 4,
                "look_per_measure": 0.56,
            }
        )
    )
    # Layer 4 (passed): 41 compares, ZERO measurements — must not warn.
    warn4 = "MEASURED MORE THAN IT LOOKED" in _rsum(
        _rec({"calls": {"mcp__blender__compare_frame": 41}, "total": 104, "looked": 43, "measured": 0, "compared": 41})
    )
    # Layer 2 (passed): high on BOTH — measuring is fine, it just must not replace looking.
    warn2 = "MEASURED MORE THAN IT LOOKED" in _rsum(
        _rec(
            {
                "calls": {"mcp__blender__compare_frame": 38},
                "total": 178,
                "looked": 45,
                "measured": 25,
                "compared": 38,
                "look_per_measure": 1.8,
            }
        )
    )
    check("warns on the profile that failed three times", warn5)
    check("silent on a layer that only looked", not warn4)
    check("silent on a layer high in BOTH — measuring is not the sin", not warn2)

    print("\n[the look/measure ratio counts the NEW tools too]")
    # The ratio is the leading indicator of a layer in trouble, and adding render_pass /
    # diff/verify tools without teaching it about them made it blind in the exact direction that
    # matters: a builder doing the right thing (isolating a pass to see what it is judged
    # on) would have been counted as not looking, and warned about for it.
    reset_tool_use()
    TOOL_USE["mcp__blender__render_pass"] = 12
    TOOL_USE["mcp__blender__diff_frames"] = 3
    TOOL_USE["mcp__blender__verify_change"] = 2
    TOOL_USE["mcp__blender__measure_regions"] = 10
    s6 = tool_use_summary()
    check("render_pass and change diffs count as LOOKING", s6["looked"] == 17, str(s6))
    check(
        "  ... so a pass-isolating builder is not warned at it",
        s6["look_per_measure"] > 1.0,
        str(s6.get("look_per_measure")),
    )
    # check_scene is its own category on purpose: the failure the ratio detects is
    # optimising against self-chosen image statistics, and a judgment-free scene fact is
    # the opposite of that. Folding it into `measured` would penalise verifying a claim.
    reset_tool_use()
    TOOL_USE["mcp__blender__check_scene"] = 9
    TOOL_USE["mcp__blender__compare_frame"] = 4
    s7 = tool_use_summary()
    check(
        "check_scene is VERIFYING, not measuring",
        s7["verified"] == 9 and s7["measured"] == 0 and s7["looked"] == 4,
        str(s7),
    )
    check(
        "  ... and does not drag the look/measure ratio",
        s7.get("look_per_measure") is None,
        str(s7.get("look_per_measure")),
    )

    print("\n[adoption of the diagnostic tools is reported, not assumed]")
    check("names only applicable tools that were never called", s7["unused_required_tools"] == ["render_pass"], str(s7))
    check(
        "static no-edit work marks change diagnostics not applicable",
        set(s7["not_applicable_tools"]) == {"diff_frames", "verify_change"},
        str(s7),
    )
    check("counts the ones that were", s7["adoption"]["check_scene"] == 9)
    unused_txt = _rsum(_rec(s7))
    check("the layer report says so loudly", "NEVER CALLED" in unused_txt, unused_txt)
    check("  ... and blames the PROMPT, not the tool", "PROMPT" in unused_txt)
    reset_tool_use()
    TOOL_USE["mcp__blender__run_bpy"] = 2
    _edited = tool_use_summary()
    check(
        "multiple scene mutations make change verification applicable",
        {"diff_frames", "verify_change"} <= set(_edited["unused_required_tools"]),
        str(_edited),
    )
    reset_tool_use()
    for t in ("render_pass", "check_scene", "diff_frames", "verify_change"):
        TOOL_USE[f"mcp__blender__{t}"] = 5
    all_used = _rsum(_rec(tool_use_summary()))
    check("silent when every diagnostic tool saw use", "NEVER CALLED" not in all_used)
    check("  ... but still reports the counts", "diagnostics" in all_used, all_used)
    reset_tool_use()

    print("\n[the durable transcript records input, output and every tool call]")
    # log_message printed all of this to STDOUT and nowhere else, and run_shot inherited
    # the stream — so the reasoning trace of a $6 layer lived in a terminal scrollback.
    import tempfile as _tf

    from vfx_harness.observability import transcript as _tr

    class _TB:
        def __init__(s, t):
            s.text = t

    class _TU:
        def __init__(s, n, i):
            s.name, s.input, s.id = n, i, "tu1"

    class _TR:
        def __init__(s, c, e=False):
            s.content, s.is_error, s.tool_use_id = c, e, "tu1"

    class _AM:
        def __init__(s, c):
            s.content = c

    _TB.__name__, _TU.__name__ = "TextBlock", "ToolUseBlock"
    _TR.__name__, _AM.__name__ = "ToolResultBlock", "AssistantMessage"

    _tmp = Path(_tf.mkdtemp())
    _p = _tr.bind(_tmp, "build", label="layer9", run_id="TEST")
    _script = "import bpy\n" + "\n".join(f"# line {i}" for i in range(300))
    _b64 = "iVBORw0KGgo" + "A" * 300_000
    _tr.prompt("build layer 9 against refs/hero.png", role="kickoff")
    _tr.message(_AM([_TU("mcp__blender__run_bpy", {"script": _script})]))
    _tr.message(_AM([_TR([{"type": "text", "text": "ok"}, {"type": "image", "data": _b64, "mimeType": "image/jpeg"}])]))
    _tr.message(_AM([_TR([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64}}])]))
    _tr.event("critic", frame=24, mean=3.5, verdict="pass", scores={"a": 3})
    _tr.unbind()
    _evs = _tr.read(_p)
    _kinds = [e["kind"] for e in _evs]
    check("the INPUT is recorded, not just the replies", "prompt" in _kinds, str(_kinds))
    check(
        "tool calls and results are recorded",
        _kinds.count("tool_use") == 1 and _kinds.count("tool_result") == 2,
        str(_kinds),
    )
    check("the critic verdict has a durable home", "critic" in _kinds)
    # A render reaches the model as ~300-450KB of base64 and the critic attaches up to
    # four per call; storing them would put hundreds of MB of pixels in a file whose
    # value is the text. The placeholder keeps the FACT of the image.
    check(
        "base64 image payloads are stripped",
        _p.stat().st_size < 100_000,
        f"{_p.stat().st_size} bytes for 600KB of base64",
    )
    check(
        "  ... but the image is still accounted for",
        all(
            c.get("bytes", 0) > 100_000
            for e in _evs
            if isinstance(e.get("content"), list)
            for c in e["content"]
            if isinstance(c, dict) and c.get("type") == "image"
        ),
    )
    # The console clips scripts to 120 lines because a terminal is unreadable otherwise.
    # This file is diffed against the next attempt, and a clipped script cannot be.
    _tu_ev = next(e for e in _evs if e["kind"] == "tool_use")
    check(
        "run_bpy scripts survive VERBATIM (the console clips, the record must not)",
        _tu_ev["input"]["script"] == _script,
        f"{len(_tu_ev['input']['script'])} vs {len(_script)}",
    )
    # A killed process leaves half a line; refusing to parse the file because of it would
    # throw away the record of the very thing that killed it.
    with _p.open("a") as _fh:
        _fh.write('{"seq": 99, "kind": "tool_u')
    check("a truncated final line is tolerated, not fatal", _tr.read(_p)[-1]["kind"] == "unparseable")
    import os as _os

    _os.environ["VFXH_NO_TRANSCRIPT"] = "1"
    check("recording can be switched off", _tr.bind(_tmp, "build") is None and not _tr.is_bound())
    del _os.environ["VFXH_NO_TRANSCRIPT"]

    print("\n[the digest says whether a run is on track]")
    from vfx_harness.application.inspect_run import _findings, _trajectory, adoption

    # A trajectory is the shape, not the last number: the cases that need attention are
    # the ones that look fine at a glance because the final value is the highest.
    check("flat rounds are called flat", _trajectory([2.0, 2.0, 2.0]).startswith("FLAT"))
    check("rising is rising", _trajectory([2.0, 3.0, 3.5]) == "rising")
    check(
        "a net gain that lost ground on the way is a sawtooth",
        _trajectory([3.0, 2.0, 3.0, 2.0, 3.33]).startswith("sawtooth"),
        _trajectory([3.0, 2.0, 3.0, 2.0, 3.33]),
    )
    check("falling is named", _trajectory([4.0, 3.0]) == "FALLING")
    check("one round is not a trend", _trajectory([3.0]) == "single round")
    check("no rounds is not a trend either", _trajectory([]) == "unscored")
    # An OLD report has no tool telemetry. Counting that as "the tool was never used"
    # would invent evidence of neglect on runs that could not have called it.
    _old = adoption([{"layer": "1", "tools": {"total": 90}}])
    check(
        "a report predating the telemetry is UNMEASURED, not zero",
        _old["never_used"] == [] and _old["unmeasured_layers"] == ["1"],
        str(_old),
    )
    _new = adoption(
        [
            {
                "layer": "1",
                "tools": {
                    "total": 90,
                    "adoption": {"render_pass": 0, "check_scene": 4, "diff_frames": 0, "verify_change": 0},
                },
            }
        ]
    )
    check(
        "a measured layer that skipped a tool IS a finding",
        set(_new["never_used"]) == {"render_pass", "diff_frames", "verify_change"},
        str(_new),
    )
    _f = _findings(
        {
            "layers": [
                {
                    "layer": "3",
                    "trajectory": "FLAT — rounds are not moving the score",
                    "rounds": 6,
                    "turns": 20,
                    "means": [2.0] * 6,
                    "canonical_pass": True,
                    "no_metric_feedback": False,
                    "status": "failed",
                    "look_per_measure": None,
                }
            ],
            "adoption": _new,
            "transcripts": [{"file": "x", "unparseable": 0}],
        }
    )
    check("the digest leads with the flat layer", any("without moving" in x for x in _f), str(_f))
    check("  ... and with the unused tool", any("never called" in x for x in _f))

    print("\n[a module cannot call a name it never imported]")
    # build_agent.build_unit() called reset_tool_use() and tool_use_summary() and
    # build_agent imported NEITHER. The call is at the top of the function outside any
    # try, so every layer build raised NameError before doing any work — the main build
    # path was dead. Nothing caught it: the telemetry's own tests import the functions
    # from vfx_harness.observability.log directly, and an unbound global does not fail at import time.
    #
    # Detecting this now lives in ruff (F821), enforced by tests/integration/test_suite.py and CI,
    # rather than in the bespoke bytecode walker that used to be here. That walker was
    # reimplementing a mature linter, and it shipped with a false positive of its own: it
    # did not count an annotated assignment (`_ERRORS: list[str] = []`) as binding a name,
    # so its first run reported four names in build_agent that were perfectly fine.
    #
    # What is worth keeping here is the cheap, specific part: the names that were actually
    # missing must be reachable from the module that calls them.
    import vfx_harness.agents.builder as _ba

    for _name in ("reset_tool_use", "tool_use_summary", "TOOL_USE", "empty_success"):
        check(f"build_agent resolves {_name}", hasattr(_ba, _name))

    print("\n[a credential nothing reads is caught before it costs a layer]")
    # A key was added as CLAUDE_API_KEY. The SDK reads ANTHROPIC_API_KEY, so it was
    # ignored and the stale OAuth token was used — and that subscription was over its
    # monthly spend limit. The failure arrived as subtype=success, cost $0.00, one turn,
    # whose entire output was the limit message.
    from vfx_harness.application.preflight import auth as _auth
    from vfx_harness.application.preflight import empty_success as _empty

    _saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_API_KEY")}
    try:
        os.environ["CLAUDE_API_KEY"] = "sk-ant-api03-" + "x" * 40
        a = _auth()
        check(
            "names the variable nothing reads",
            not a["ok"] and "NOTHING READS IT" in a["problems"][0],
            str(a["problems"]),
        )
        check("  ... says which variable to use instead", "ANTHROPIC_API_KEY" in a["problems"][0])
        check("  ... and that there is now no usable credential at all", a["using"] is None, str(a["using"]))
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-" + "y" * 40
        a2 = _auth()
        check(
            "with the correct name set, the dead one is only dead weight",
            a2["using"] == "ANTHROPIC_API_KEY" and "dead weight" in a2["problems"][0],
            str(a2["problems"]),
        )
        del os.environ["CLAUDE_API_KEY"]
        check("clean once the decoy is gone", _auth()["ok"])
        # Prefix mismatch: an OAuth token pasted into the API-key variable is catchable
        # without sending it anywhere.
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-oat01-" + "z" * 40
        check(
            "catches an OAuth token in the API-key variable",
            "expects a API key" in " ".join(_auth()["problems"])
            or "expects an API key" in " ".join(_auth()["problems"]),
            str(_auth()["problems"]),
        )
    finally:
        for k, v in _saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    # The observed spend-limit shape. Cost AND tools, not either alone: a cheap turn is
    # not suspicious on its own, but nothing that spent $0 and touched no tool has built.
    check(
        "a zero-cost, zero-tool 'success' is not a build",
        _empty({"subtype": "success", "turns": 1, "cost": 0.0}, 0) is not None,
    )
    check(
        "  ... and the message points at preflight",
        "preflight" in _empty({"subtype": "success", "turns": 1, "cost": 0.0}, 0),
    )
    check(
        "a session that called tools is a build", _empty({"subtype": "success", "turns": 12, "cost": 0.0}, 40) is None
    )
    check("a session that spent money is a build", _empty({"subtype": "success", "turns": 1, "cost": 0.31}, 0) is None)
    check(
        "a real error path is left to its own handler",
        _empty({"subtype": "error_max_turns", "turns": 40, "cost": 0.0}, 0) is None,
    )

    print("\n[a layer learns from its own failed attempts]")
    # The ledger held every critic round with its issues and NONE of it reached the
    # builder: each attempt started blind to the last one's corrections. Layer 5's second
    # attempt rebuilt a six-light rig not knowing the first had twice been told the hero
    # was not light-linked. The existing critique feedback only carries WITHIN an attempt.
    from vfx_harness.agents.build_prompts import builder_kickoff, recurring_complaints

    h5 = recurring_complaints(shot, layers["5"].as_milestone())
    check("surfaces complaints that recur across attempts", "ATTEMPTED" in h5, h5[:80])
    check("  ... names how many attempts raised each", "separate attempts" in h5)
    check("  ... and forbids silently skipping one", "do not silently skip" in h5)
    check("silent for a layer that passed first time", recurring_complaints(shot, layers["1"].as_milestone()) == "")
    # Recurrence, not volume: one round's note is noise, a note that survives an
    # independent attempt describes something the layer keeps getting wrong.
    check(
        "needs >=2 attempts before it says anything",
        recurring_complaints(shot, layers["5"].as_milestone(), min_attempts=99) == "",
    )
    _history_file = shot.folder / "shot.json"
    _history_before = _history_file.read_text(encoding="utf-8")
    try:
        _history_data = json.loads(_history_before)
        _history_data["milestones"]["1"]["history"] = [
            {
                "attempt": 1,
                "status": "failed",
                "rounds": [
                    {
                        "attempt": 1,
                        "kind": "canonical",
                        "pass": False,
                        "issues": ["pier inner face remains flat at f120"],
                    }
                ],
            }
        ]
        _history_data["milestones"]["1"]["rounds"] = []
        _history_file.write_text(json.dumps(_history_data), encoding="utf-8")
        _archived = recurring_complaints(shot, layers["1"].as_milestone(), min_attempts=99)
        check(
            "an archived canonical failure reaches the very next attempt",
            "PREVIOUS ATTEMPT FAILED CANONICAL" in _archived and "pier inner face" in _archived,
            _archived,
        )
    finally:
        _history_file.write_text(_history_before, encoding="utf-8")
    _retry_file = Path(tempfile.mkdtemp()) / "04_lighting.py"
    _retry_file.write_text("# prior measured artifact\n", encoding="utf-8")
    check("failed artifacts warm-start a retry", _retry_warm_start("failed", _retry_file))
    check(
        "passed artifacts use deterministic revalidation, not warm start", not _retry_warm_start("passed", _retry_file)
    )
    check(
        "the kickoff actually carries it", "ATTEMPTED" in builder_kickoff(shot, layers["5"].as_milestone(), history=h5)
    )
    check(
        "the kickoff declares LIVE_BUILD mode",
        builder_kickoff(shot, layers["1"].as_milestone()).startswith("MODE: LIVE_BUILD"),
    )
    _scene_contract = shot.folder / "scene_checks.json"
    _scene_contract_before = _scene_contract.read_text(encoding="utf-8") if _scene_contract.is_file() else None
    try:
        _scene_contract.write_text("[]\n", encoding="utf-8")
        check(
            "the kickoff makes scene object selectors an explicit interface",
            "read `scene_checks.json` BEFORE" in builder_kickoff(shot, layers["1"].as_milestone()),
        )
    finally:
        if _scene_contract_before is None:
            _scene_contract.unlink(missing_ok=True)
        else:
            _scene_contract.write_text(_scene_contract_before, encoding="utf-8")

    print("\n[the plan agent knows the departments]")
    # Everything learned on barrel_roll lived only as hand-edits to that shot's plan, so a
    # new brief would have been planned by an agent that had never heard any of it — no
    # lighting stage, no lookdev, and the same $78 of geometry added over geometry.
    from vfx_harness.agents.prompts import PLANNER_SYSTEM as _PS

    check(
        "names the department order incl. LIGHTING",
        "LIGHTING → FX" in _PS and "layout → set dressing → environment" in _PS,
    )
    check(
        "requires lookdev before shot work for a hero asset", "LOOKDEV BEFORE SHOT WORK" in _PS and "turntable" in _PS
    )
    check("warns that an imported asset may already carry its look", "ALREADY CARRY" in _PS and "baked maps" in _PS)
    check("gives the lighting axis its anti-gaming clause", "legible only because its windows glow" in _PS)
    check(
        "one axis, one subject, one owner", "ONE AXIS, ONE SUBJECT, ONE OWNER" in _PS and "owned by TWO layers" in _PS
    )
    check(
        "carries the sun/volume physics into PLAN decisions",
        "infinitely distant" in _PS and "BOUNDED volume domain" in _PS,
    )
    check("says an emission shader cannot be lit", "EMISSION shader cannot be lit" in _PS)
    # Ticket granularity — the same bundling defect as axes, one level down.
    check("one ticket, one control", "ONE TICKET, ONE CONTROL" in _PS and "set INDEPENDENTLY" in _PS)
    check(
        "every control the approach can vary gets a target",
        "NAME EVERY CONTROL" in _PS and "any value, not scored" in _PS,
    )
    check(
        "an assumption that drives the approach must be checked first",
        "MARK THE PREMISE" in _PS and "Unchecked assumptions" in _PS,
    )
    # Lighting is where the bundling rule is easiest to break — I broke it myself, in the
    # same session I added the rule, by writing an axis covering both hero form AND
    # set-wide exposure hierarchy. Four attempts failed between a ticket that forbade
    # extra lights and an axis that required them.
    check(
        "lighting axes split form from exposure hierarchy",
        "LIGHTING IS THE EASIEST ONE TO GET WRONG" in _PS and "DIFFERENT SUBJECTS" in _PS,
    )

    print("\n[sun-in-volume trap]")
    # The runtime check needs bpy, so it is verified empirically (no sun -> silent;
    # sun + world volume -> warns; volume unlinked -> silent again). What IS testable
    # here is that the warning and its guidance still exist in all three places, since
    # the failure mode is someone tidying away a comment and restoring a $78 trap.
    worker_src = Path("src/vfx_harness/blender/worker.py").read_text(encoding="utf-8")
    prompts_src = Path("src/vfx_harness/agents/build_prompts.py").read_text(encoding="utf-8")
    tools_src = Path("src/vfx_harness/blender/tools.py").read_text(encoding="utf-8")
    check(
        "the worker warns on SUN + world volume", "_scene_warnings" in worker_src and "SUN + WORLD VOLUME" in worker_src
    )
    check(
        "every render path surfaces worker warnings",
        tools_src.count("_warn_suffix(r)") >= 3,
        str(tools_src.count("_warn_suffix(r)")),
    )
    check("the render result carries warnings", '"warnings": _scene_warnings()' in worker_src)
    check(
        "the builder prompt names the trap and the fix",
        all(s in prompts_src for s in ("SUN CONTRIBUTES ALMOST NOTHING", "AREA/POINT/SPOT")),
    )
    check("bvfx_volumetric_world's docstring carries the warning", "infinitely distant" in worker_src)
    check(
        "imported assets are normalised out of QUATERNION mode",
        'rotation_mode = "XYZ"' in worker_src and "silent no-op" in worker_src,
    )

    print("\n[a verdict is about a script, not a layer id]")
    # 01_layout.py was edited after layer 1 was recorded `passed`, and nothing anywhere
    # noticed that the verdict and the canonical renders now described the previous
    # script. provenance.py does this for plan artifacts vs brief.md; build scripts had
    # no equivalent.
    import tempfile as _tf2

    with _tf2.TemporaryDirectory() as td:
        dst = Path(td) / "barrel_roll"
        shutil.copytree(shot.folder, dst, symlinks=True, ignore=shutil.ignore_patterns("renders", "logs", "artifacts"))
        from vfx_harness.domain.brief import load_shot as _ls

        s2 = _ls(str(dst))
        led2, lay2 = Ledger(s2), load_layers(s2)
        m1 = lay2["1"].as_milestone()
        led2.mark(m1, "passed")
        check("marking a pass records the script digest", led2.script_digest(m1))
        check("an untouched script is not stale", led2.stale(m1) is None)
        sp = dst / lay2["1"].script
        sp.write_text(sp.read_text(encoding="utf-8") + "\n# an edit\n", encoding="utf-8")
        why = led2.stale(m1)
        check("editing the script makes the recorded pass stale", bool(why))
        check(
            "  ... and the reason names the layer and the script",
            bool(why) and "layer 1" in why and Path(lay2["1"].script).name in why,
            str(why)[:90],
        )
        led2.mark(m1, "failed")
        check("a non-passed layer is never reported stale", led2.stale(m1) is None)
    check("chaining treats a stale prior like an unpassed one", "ledger.stale(g.as_milestone())" in ba_src)

    print("\n[the asset gate can see a facade]")
    # The silhouette gate (#30) passes sr2_tower at 1.73x base flare vs the plate's 1.85x
    # while the facade the pipeline rendered had the WRONG POLARITY. A silhouette
    # statistic cannot see a facade.
    #
    # BOTH DIRECTIONS ARE TESTED, because the first version of this gate passed the very
    # defect it was written for: it made L1 primary at a 0.25 threshold, and the bad case
    # scores 0.242. A gate proved only against the good case is not a gate.
    from vfx_harness.assets.normalize import facade_vs_plate as _fvp

    _plate = shot.folder / "assets/sr2_tower/isolated/view_0.png"
    _bad = ROOT_DIR / "docs/research/probes/L39_turntable_front.png"  # bvfx_emissive_windows
    if _plate.is_file() and _bad.is_file():
        rb = _fvp(_plate, _bad)
        check(
            "catches a facade whose polarity is lost",
            rb["verdict"] == "facade-polarity-lost",
            f"{rb['verdict']} (outer/core {rb.get('mesh_outer_core')} vs "
            f"{rb.get('plate_outer_core')}, L1 {rb.get('profile_l1')})",
        )
        check("  ... and says so in terms a builder can act on", "CANNOT fix it by shading" in (rb.get("note") or ""))
        # The plate compared with ITSELF must pass — a gate that fails everything is as
        # useless as one that passes everything.
        rg = _fvp(_plate, _plate)
        check("passes an exact match", rg["verdict"] == "consistent", str(rg["verdict"]))
    else:
        check("facade gate fixtures present", False, f"{_plate} / {_bad}")

    print("\n[textured assets keep their facade]")
    # sr2_tower ships three 2048² maps that reproduce its design plate; the procedural
    # window helper cleared the material slots and threw them away, inverting the facade
    # polarity (L1 0.184/ratio 4.41 native -> 0.242/1.91 procedural) and deleting the sign.
    check("a texture-preserving emission helper exists", "_bvfx_emissive_from_texture" in worker_src)
    check(
        "  ... and is exposed to build scripts",
        '"bvfx_emissive_from_texture": _bvfx_emissive_from_texture' in worker_src,
    )
    check("it ADDS emission rather than replacing the shader", "ShaderNodeAddShader" in worker_src)
    check(
        "the destructive helper warns before discarding textures",
        "_warn_if_textured(obj)" in worker_src and "about to DISCARD" in worker_src,
    )
    check(
        "the builder prompt steers imported assets to the right helper",
        "USE THIS" in prompts_src and "bvfx_emissive_from_texture" in prompts_src,
    )
    check("the prompt states what the destructive one costs", "CLEARS THE OBJECT'S MATERIAL SLOTS" in prompts_src)

    print("\n[facade profile]")
    from PIL import Image as _Im

    from vfx_harness.application.facade import compare_profiles, facade_profile

    def synth(path, strips, w=200, h=600, bg=255, body=40, win=250):
        """A synthetic tower: `strips` are (x0,x1) fractions of the shaft that are lit."""
        im = _Im.new("L", (w, h), bg)
        px = im.load()
        sx0, sx1 = int(0.25 * w), int(0.75 * w)
        for y in range(int(0.05 * h), int(0.95 * h)):
            for x in range(sx0, sx1):
                f = (x - sx0) / (sx1 - sx0)
                lit = any(a <= f <= b for a, b in strips) and (y // 6) % 2 == 0
                px[x, y] = win if lit else body
        im.save(path)

    import tempfile as _tf

    with _tf.TemporaryDirectory() as td:
        outer_p = Path(td) / "outer.png"
        centre_p = Path(td) / "centre.png"
        # OUTER strips against a dark core -- the plate's polarity.
        synth(outer_p, [(0.02, 0.22), (0.78, 0.98)])
        # The inverse: bright centre, dark edges. This is what layer 1 actually renders,
        # and the instrument exists to tell these two apart by number.
        synth(centre_p, [(0.40, 0.60)])
        o = facade_profile(outer_p)
        c = facade_profile(centre_p)
        check(
            "outer-strip tower reads a high outer/core ratio", o["outer_core_ratio"] > 3.0, str(o["outer_core_ratio"])
        )
        check("centre-lit tower reads a low outer/core ratio", c["outer_core_ratio"] < 1.0, str(c["outer_core_ratio"]))
        check(
            "the two polarities are far apart in profile L1",
            compare_profiles(c, o)["l1"] > 0.3,
            str(compare_profiles(c, o)["l1"]),
        )
        check("a profile compared with itself is identical", compare_profiles(o, o)["l1"] == 0.0)
        check("shaft width excludes the background", abs(o["shaft_px"] - 100) <= 4, str(o["shaft_px"]))

        # Segmentation must FAIL LOUDLY rather than profile the backdrop. A hardcoded
        # 240 threshold silently classified a whole 720px turntable render as subject,
        # and every angle then returned an identical "measurement".
        flat = Path(td) / "flat.png"
        _Im.new("L", (200, 600), 211).save(flat)
        try:
            r = facade_profile(flat)
            check(
                "a frame with no separable subject is flagged, not silently profiled",
                bool(r.get("warning")),
                str(r.get("warning")),
            )
        except ValueError:
            check("a frame with no separable subject is flagged, not silently profiled", True)

        # The backdrop is segmented by its ACTUAL value, not a constant: a 211-grey
        # backdrop must still yield a ~100px shaft, which the old threshold could not do.
        grey_bg = Path(td) / "greybg.png"
        synth(grey_bg, [(0.02, 0.22), (0.78, 0.98)], bg=211)
        g = facade_profile(grey_bg)
        check(
            "segments against a non-white backdrop (211, not 255)",
            abs(g["shaft_px"] - 100) <= 4 and not g.get("warning"),
            f"shaft {g['shaft_px']} warn {g.get('warning')}",
        )

    print(
        f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}  ({'0' if not FAILS else len(FAILS)} failed)"
    )
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
