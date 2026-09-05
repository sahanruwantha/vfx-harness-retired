"""Look scope follows typed unit authority, not axis-identifier scanning.

Run 20260823T154920Z: `iris_seal_readability` contains no look word, so
`axis_feedback_groups` returned nothing and the builder was told appearance was out of
scope — while that unit's own plan owned layered machined metal, seams, fasteners, and
extreme-close-range readability. Identifiers are names; capabilities are authority."""

from __future__ import annotations

import pytest

from vfx_harness.agents.build_prompts import (
    axis_feedback_groups,
    capability_feedback_groups,
)
from vfx_harness.domain.work_units import LOOK_CAPABILITIES, parse_look_capabilities


def test_appearance_unit_whose_axis_has_no_look_word_still_gets_feedback() -> None:
    axes = [("iris_seal_readability", "layered machined metal, seams, fasteners")]
    assert axis_feedback_groups(axes) == frozenset()  # the historical silence

    groups = capability_feedback_groups(("material", "detail"))

    assert "detail" in groups and "color" in groups


def test_layout_unit_declaring_nothing_receives_no_look_prescription() -> None:
    assert capability_feedback_groups(()) == frozenset()
    assert capability_feedback_groups(("motion",)) == frozenset({"motion"})


def test_continuity_axis_scan_is_not_used_for_a_declaring_empty_unit() -> None:
    """cam_spine declared look_capabilities: [] and still got motion feedback because
    camera_continuity contains 'continuity'. Empty declared capabilities are authority."""
    axes = [("camera_continuity", "smooth path"), ("camera_collision_clearance", "clear")]
    scanned = axis_feedback_groups(axes)
    assert scanned, "the identifier scan is the trap this test pins"
    assert capability_feedback_groups(()) == frozenset()


def test_executable_only_preview_defaults_to_workbench() -> None:
    """cam_spine created a temp sun to light an EEVEE verify. Live preview on a
    declaring empty look unit defaults to solid; an explicit eevee request is honored."""
    from vfx_harness.blender.tools import preview_render_mode

    assert preview_render_mode(False, None, look_default="eevee") == "solid"
    assert preview_render_mode(False, None, look_default="draft") == "solid"
    assert preview_render_mode(False, "eevee", look_default="eevee") == "eevee"
    assert preview_render_mode(True, None, look_default="eevee") == "eevee"
    assert preview_render_mode(True, None, look_default="draft") == "draft"


def test_lookless_reference_comparison_defaults_to_workbench() -> None:
    """HIR-0131: form/layout needs a live reference diagnostic without inventing
    illumination. The round lock still wins for crops and explicit EEVEE is honored."""
    from vfx_harness.blender.tools import _comparison_mode_scale

    assert _comparison_mode_scale({}, None, look_actions=False) == ("solid", 0.4)
    assert _comparison_mode_scale({}, None, look_actions=True) == ("eevee", 0.4)
    assert _comparison_mode_scale(
        {"mode": "eevee"}, None, look_actions=False
    ) == ("eevee", 0.4)
    assert _comparison_mode_scale(
        {}, ("solid", 0.5, None), look_actions=True
    ) == ("solid", 0.5)


def test_first_comparison_scale_reaches_the_measurement_floor() -> None:
    """HIR-0174: the omitted first scale is derived from the shot's frame height so the
    default comparison never measures an upscaled plate; the round lock and an explicit
    scale still win."""
    from vfx_harness.blender.tools import _comparison_mode_scale
    from vfx_harness.blender.tools.reports import _METRIC_H, measurement_floor_scale

    assert measurement_floor_scale(640) == 0.5
    assert measurement_floor_scale(720) == 0.45
    assert measurement_floor_scale(960) == 0.4
    assert measurement_floor_scale(1080) == 0.4
    assert measurement_floor_scale(200) == 1.0
    assert measurement_floor_scale(None) == 0.4
    for height in (480, 640, 720, 960, 1080, 2160):
        assert measurement_floor_scale(height) * height >= _METRIC_H - 1e-9
    with pytest.raises(ValueError, match="resolution_y must be positive"):
        measurement_floor_scale(0)

    assert _comparison_mode_scale({}, None, look_actions=False, resolution_y=640) == ("solid", 0.5)
    assert _comparison_mode_scale({"scale": 0.35}, None, look_actions=False, resolution_y=640) == (
        "solid",
        0.35,
    )
    assert _comparison_mode_scale({}, ("solid", 0.4, None), look_actions=False, resolution_y=640) == (
        "solid",
        0.4,
    )


def test_capabilities_are_validated_against_a_closed_vocabulary() -> None:
    assert parse_look_capabilities(["material", "detail"], "unit.look") == (
        "material",
        "detail",
    )
    with pytest.raises(ValueError, match="unknown capability"):
        parse_look_capabilities(["shiny"], "unit.look")
    # The error must name the accepted set — the session cannot look it up otherwise.
    try:
        parse_look_capabilities(["shiny"], "unit.look")
    except ValueError as exc:
        for name in LOOK_CAPABILITIES:
            assert name in str(exc)


def test_declared_capabilities_beat_identifier_scanning_in_the_tool_policy() -> None:
    from vfx_harness.blender.tools import build_blender_tools

    class _Session:
        pass

    server, names = build_blender_tools(
        _Session(), layer_id="1", feedback_groups=["detail", "color"]
    )
    assert server is not None  # wiring accepts the typed override
    assert "mcp__blender__inspect_view" in names


def test_diagnostic_artist_view_can_never_mint_image_payment() -> None:
    from vfx_harness.blender.tools import _payment_eligible_candidate

    assert _payment_eligible_candidate({"mode": "eevee", "scale": 0.5})
    assert not _payment_eligible_candidate({
        "mode": "eevee", "scale": 0.5, "diagnostic_only": True
    })


def test_work_unit_parses_and_defaults_capabilities() -> None:
    from tests.architecture.test_staged_architecture import _unit

    unit = _unit("blockout")
    assert unit.look_capabilities == ()  # omitted key parses empty; materialization requires an explicit list

    from tests.architecture.test_staged_architecture import _claim
    from vfx_harness.domain.work_units import WorkUnit

    row = {
        "id": "surfacing", "title": "Surfacing", "plan": "plans/units/surfacing.md",
        "depends_on": [],
        "mutates": {"mode": "scoped", "roles": [], "controls": [],
                    "script_spans": ["build/units/01/surfacing.py"]},
        "protects": {"selector": "all_active_upstream_interfaces",
                     "resolve_to_explicit_ids_at": "freeze"},
        "evaluation": {"primary_judge": 40,
                       "judge": [{"frame": 40, "ref": "refs/f040.png"}],
                       "temporal_evidence": "none",
                       "claims": [_claim("surfacing")]},
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material"],
    }
    parsed = WorkUnit.parse(row, "unit.surfacing")

    assert parsed.look_capabilities == ("material",)
    assert capability_feedback_groups(parsed.look_capabilities) == frozenset(
        {"detail", "color"}
    )


def test_materialization_requires_an_explicit_capability_declaration(
    tmp_path, monkeypatch
) -> None:
    """Silence is not a declaration: an omitted key is indistinguishable from
    "owns no appearance", which is how run 20260823T154920Z left an appearance-owning
    unit without image feedback. An explicit [] is the legal way to own none."""
    import json

    from tests.unit.test_plan_records import (
        _add_deferred_layer,
        _candidate,
        _jit_payload,
        _write,
        publish_current,
    )
    from vfx_harness.observability import run_artifacts
    from vfx_harness.orchestration.jit_materialization import validate_materialization

    monkeypatch.delenv(run_artifacts.ENV, raising=False)
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "capability-declaration")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    payload = _jit_payload(tmp_path, bundle.content_hash)

    data = json.loads(payload.read_text(encoding="utf-8"))
    for stage in data["layer"]["stages"]:
        stage.pop("look_capabilities", None)
    _write(payload, data)

    with pytest.raises(ValueError, match="must declare look_capabilities"):
        validate_materialization(
            bundle.root, payload, expected_bundle_hash=bundle.content_hash,
            shot_folder=bundle.root,)


def test_live_scope_rule_matches_the_canonical_replay_rule() -> None:
    """Live feedback and the deterministic gate must not disagree about scope."""
    from vfx_harness.agents.builder import _scope_added_object_errors
    from vfx_harness.blender.tools import _role_in_scope

    allowed = ("iris.blades", "iris_lights.*")
    for role, expected in (
        ("iris.blades.lead", True),      # namespace owns dot-descendants
        ("iris.blades", True),
        ("iris_lights.rim", True),
        ("iris.housing", False),         # sibling namespace is NOT owned
        ("camera", False),
        ("", False),                     # untagged helper objects
    ):
        assert _role_in_scope(role, allowed) is expected, role
        errors = _scope_added_object_errors({}, {"obj": role}, allowed)
        assert bool(errors) is (not expected), role


def test_live_scope_reports_the_real_untagged_object() -> None:
    """cam_rig_spine (run 20260824T045543Z-e0e47b) created CAM_spine with no bvfx_role.
    Canonical replay rejected it at the end; the live check said nothing. The offender
    must be named, and must keep being named until it is fixed rather than suppressed
    after first sight — builders create first and tag second."""
    from vfx_harness.blender.tools import _scope_offenders

    allowed = ("cam_rig",)
    manifest = {"CAM": "cam_rig", "CAM_spine": ""}

    first = _scope_offenders(manifest, allowed)
    second = _scope_offenders(manifest, allowed)

    assert first == ["'CAM_spine' role=<none>"]
    assert second == first, "a violation must persist until fixed, not vanish"

    fixed = _scope_offenders({"CAM": "cam_rig", "CAM_spine": "cam_rig.spine"}, allowed)
    assert fixed == []


def test_camera_ownership_is_declared_not_spelled() -> None:
    """The bootstrap rule substring-matched "camera" in mutated role names, so
    `cam_rig` — the harness's own default camera-rig role, and the role the approved
    camera decision keys against — was invisible while its unit had already passed."""
    from vfx_harness.domain.work_units import UNIT_PROVIDES, parse_provides

    assert parse_provides(["camera"], "u.provides") == ("camera",)
    with pytest.raises(ValueError, match="unknown capability"):
        parse_provides(["cam_rig"], "u.provides")
    assert "camera" in UNIT_PROVIDES

    # The legacy spelling heuristic must never have decided this unit.
    assert not any("camera" in r.lower() for r in ("cam_rig",))


def test_lookless_composition_fans_in_unit_claims() -> None:
    """Run 20260827T031330Z-c4687e: both L1 units sealed executable-only, then
    composed canonical called _judge because active_unit was omitted."""
    from types import SimpleNamespace

    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.agents.builder import (
        _composition_judge_unit,
        _executable_unit_verdict,
    )

    path = _unit("cam_path")
    proxies = _unit("proxies", depends_on=["cam_path"])
    layer = SimpleNamespace(id="1", stages=(path, proxies))
    fan_in = _composition_judge_unit(layer)
    assert fan_in is not None
    assert fan_in.look_capabilities == ()
    claim_ids = {claim.id for claim in fan_in.evaluation.claims}
    assert claim_ids == {"claim.cam_path", "claim.proxies"}
    assert fan_in.worklist_units == (path, proxies)
    assert set(fan_in.mutates.roles) == set()

    axes = [("camera_continuity", "path"), ("camera_collision_clearance", "clear")]
    evidence = [
        {"id": "contract.cam_path", "pass": True, "authoritative": True},
        {"id": "contract.proxies", "pass": True, "authoritative": True},
    ]
    verdict = _executable_unit_verdict(fan_in, 40, axes, evidence)
    assert verdict is not None and verdict["pass"] is True
    assert verdict["decided_by"] == "unit_executable_evidence"


def test_executable_scene_unit_does_not_require_raster(tmp_path, monkeypatch) -> None:
    """A legal pre-camera control producer must not owe an impossible render."""
    from types import SimpleNamespace

    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.agents.builder import _unit_requires_raster

    unit = _unit("control_target")
    monkeypatch.setattr(
        "vfx_harness.evidence.scene_checks.load_rows",
        lambda _folder, _selected=None: [
            {
                "id": "contract.control_target",
                "kind": "object_property",
                "frame": 40,
            }
        ],
    )

    assert _unit_requires_raster(SimpleNamespace(folder=tmp_path), unit) is False


def test_functional_scene_contract_still_requires_raster(tmp_path, monkeypatch) -> None:
    """Binding spelling is not enough: registry-declared image metrics still render."""
    from types import SimpleNamespace

    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.agents.builder import _unit_requires_raster

    unit = _unit("response")
    monkeypatch.setattr(
        "vfx_harness.evidence.scene_checks.load_rows",
        lambda _folder, _selected=None: [
            {
                "id": "contract.response",
                "kind": "render_region_stat",
                "frame": 40,
            }
        ],
    )

    assert _unit_requires_raster(SimpleNamespace(folder=tmp_path), unit) is True


def test_executable_canonical_replay_never_calls_render(tmp_path, monkeypatch) -> None:
    """HIR-0114 regression: scene evidence must be checked before any raster call."""
    from types import SimpleNamespace

    import anyio

    from tests.architecture.test_staged_architecture import _unit
    from vfx_harness.agents import builder

    unit = _unit("control_target")
    script_rel = "build/units/01/control_target.py"
    script_path = tmp_path / script_rel
    script_path.parent.mkdir(parents=True)
    script_path.write_text("# deterministic control\n", encoding="utf-8")

    monkeypatch.setattr(builder, "_preamble", lambda _shot: "")
    monkeypatch.setattr(builder, "_run_prior_paths", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(builder, "_scene_object_manifest", lambda _session: {})
    monkeypatch.setattr(
        builder,
        "_render_evidence",
        lambda *_args, **_kwargs: [
            {
                "id": "contract.control_target",
                "pass": True,
                "authoritative": True,
            }
        ],
    )
    monkeypatch.setattr(
        "vfx_harness.evidence.scene_checks.load_rows",
        lambda _folder, _selected=None: [
            {
                "id": "contract.control_target",
                "kind": "object_property",
                "frame": 40,
            }
        ],
    )
    monkeypatch.setattr(
        builder,
        "_stash_render",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("executable-only canonical replay must not render")
        ),
    )

    class _Session:
        def run(self, *_args, **_kwargs):
            return {"result": {}}

    class _Ledger:
        def __init__(self):
            self.rounds = []

        def record_round(self, *args, **kwargs):
            self.rounds.append((args, kwargs))

    layer = SimpleNamespace(
        id="1",
        judges=((40, "refs/f040.png"),),
        stages=(unit,),
        owns=("form",),
    )
    shot = SimpleNamespace(
        folder=tmp_path,
        frontmatter={"type": "motion"},
        frames=100,
    )
    milestone = SimpleNamespace(
        id="1@control_target",
        frame=40,
        ref="refs/f040.png",
    )
    ledger = _Ledger()
    verdicts = []

    async def _run():
        return await builder._verify_script(
            shot,
            milestone,
            script_rel,
            [],
            _Session(),
            [("form", "declared form")],
            ledger,
            False,
            layer=layer,
            active_unit=unit,
            out_verdicts=verdicts,
        )

    assert anyio.run(_run) == "passed"
    assert verdicts[0][1]["decided_by"] == "unit_executable_evidence"
    assert ledger.rounds


def test_live_unit_render_is_guarded_by_typed_raster_need() -> None:
    """Pin the producing live-loop branch that failed before canonical replay."""
    import inspect

    from vfx_harness.agents import builder

    source = inspect.getsource(builder.build_unit)
    assert "raster_required = _unit_requires_raster(" in source
    assert "selected_authority=selected_authority" in source
    assert "if raster_required" in source
    assert "mode=_unit_raster_mode(active_unit)" in source
    revalidate_source = inspect.getsource(builder._try_revalidate)
    assert "require_current_layer_publication" in revalidate_source
    assert "load_layer_outcome" not in revalidate_source
    assert "raster_required = _unit_requires_raster(" in revalidate_source
    assert "selected_authority=selected_authority" in revalidate_source
    assert "deterministic_executable_revalidation" in revalidate_source
    probe_source = inspect.getsource(builder._build_probe_candidate_server)
    assert 'raster_required = bool(probe_ctx.get("raster_required", True))' in probe_source
    assert 'frame_row["raster_required"] = False' in probe_source


def test_look_owning_stage_keeps_composed_critic_path() -> None:
    from types import SimpleNamespace

    from tests.architecture.test_staged_architecture import _claim, _unit
    from vfx_harness.agents.builder import _composition_judge_unit
    from vfx_harness.domain.work_units import WorkUnit

    path = _unit("cam_path")
    row = {
        "id": "surfacing",
        "title": "Surfacing",
        "plan": "plans/units/surfacing.md",
        "depends_on": ["cam_path"],
        "mutates": {
            "mode": "scoped",
            "roles": ["lookdev.primary_material"],
            "controls": [],
            "script_spans": ["build/units/02/surfacing.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [_claim("surfacing")],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material"],
    }
    surfacing = WorkUnit.parse(row, "unit.surfacing")
    layer = SimpleNamespace(id="2", stages=(path, surfacing))
    assert _composition_judge_unit(layer) is None


def test_lookless_uncovered_frame_is_contract_gap(monkeypatch) -> None:
    """Look-less empty claims at a canonical frame used to auto-pass 5.0 (HIR-0045)."""
    from types import SimpleNamespace

    import anyio

    from vfx_harness.agents.builder import _judge_unit_or_layer

    def _boom(*_args, **_kwargs):
        raise AssertionError("uncovered judge frame must not call _judge")

    monkeypatch.setattr("vfx_harness.agents.builder._judge", _boom)
    monkeypatch.setattr(
        "vfx_harness.evidence.scene_checks.load_rows",
        lambda _folder, _selected=None: [],
    )
    unit = SimpleNamespace(
        id="cam_path_core",
        look_capabilities=(),
        evaluation=SimpleNamespace(claims=(), judges=()),
        mutates=SimpleNamespace(roles=("cam_rig",)),
    )
    shot = SimpleNamespace(folder=SimpleNamespace())
    milestone = SimpleNamespace(frame=1, id="1")

    async def _run():
        return await _judge_unit_or_layer(
            shot,
            milestone,
            "renders/black.png",
            [("camera_continuity", "path")],
            session=None,
            verbose=False,
            scope=None,
            evidence=[],
            active_unit=unit,
        )

    verdict = anyio.run(_run)
    assert verdict["pass"] is False
    assert verdict["contract_gap"] is True
    assert verdict["issues"] == []
    assert verdict["decided_by"] == "uncovered_judge_frame"


def test_lookless_nonexecutable_claim_still_skips_critic(monkeypatch) -> None:
    """If executable verdict abstains, look-less still must not enter _judge."""
    from types import SimpleNamespace

    import anyio

    from vfx_harness.agents.builder import _judge_unit_or_layer

    def _boom(*_args, **_kwargs):
        raise AssertionError("look-less evaluation must not call _judge")

    monkeypatch.setattr("vfx_harness.agents.builder._judge", _boom)
    monkeypatch.setattr(
        "vfx_harness.evidence.scene_checks.load_rows",
        lambda _folder, _selected=None: [],
    )
    unit = SimpleNamespace(
        id="cam_path_core",
        look_capabilities=(),
        evaluation=SimpleNamespace(
            claims=(
                SimpleNamespace(
                    required=True,
                    moments=(1,),
                    authority="qualified_qualitative_required",
                    evidence=(),
                ),
            )
        ),
        mutates=SimpleNamespace(roles=("cam_rig",)),
    )
    shot = SimpleNamespace(folder=SimpleNamespace())
    milestone = SimpleNamespace(frame=1, id="1")

    async def _run():
        return await _judge_unit_or_layer(
            shot,
            milestone,
            "renders/black.png",
            [("camera_continuity", "path")],
            session=None,
            verbose=False,
            scope=None,
            evidence=[],
            active_unit=unit,
        )

    verdict = anyio.run(_run)
    assert verdict["pass"] is False
    assert verdict["contract_gap"] is True
    assert verdict["decided_by"] == "lookless_requires_executable_claims"


def test_uncovered_judge_frame_does_not_call_the_critic(monkeypatch) -> None:
    """Run 20260827T050158Z-7d5924 atmosphere: claims only at f72, judge also f150."""
    from types import SimpleNamespace

    import anyio

    from tests.architecture.test_staged_architecture import _claim
    from vfx_harness.agents.builder import _judge_unit_or_layer
    from vfx_harness.domain.work_units import WorkUnit

    def _boom(*_args, **_kwargs):
        raise AssertionError("uncovered look frame must not call _judge")

    monkeypatch.setattr("vfx_harness.agents.builder._judge", _boom)
    monkeypatch.setattr(
        "vfx_harness.evidence.scene_checks.load_rows",
        lambda _folder, _selected=None: [],
    )
    row = {
        "id": "atmosphere",
        "title": "Atmosphere",
        "plan": "plans/units/atmosphere.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["world.volumetric_haze"],
            "controls": [],
            "script_spans": ["build/units/02/atmosphere.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 72,
            "judge": [
                {"frame": 72, "ref": "refs/f072.png"},
                {"frame": 150, "ref": "refs/f150.png"},
            ],
            "temporal_evidence": "none",
            "claims": [_claim("atmosphere", frame=72)],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["atmosphere"],
    }
    unit = WorkUnit.parse(row, "unit.atmosphere")
    shot = SimpleNamespace(folder=SimpleNamespace())
    milestone = SimpleNamespace(frame=150, id="2")

    async def _run():
        return await _judge_unit_or_layer(
            shot,
            milestone,
            "renders/2@atmosphere_canonical_f150.png",
            [("atmospheric_scale_reinforcement", "shafts")],
            session=None,
            verbose=False,
            scope=None,
            evidence=[],
            active_unit=unit,
        )

    verdict = anyio.run(_run)
    assert verdict["pass"] is False
    assert verdict["contract_gap"] is True
    assert verdict["issues"] == []
    assert verdict["decided_by"] == "uncovered_judge_frame"


def test_look_owning_scene_only_claims_do_not_seal_or_call_the_critic(monkeypatch) -> None:
    """Run 20260827T060800Z-8bd92a: look units sealed 5.0 on counts, then composed
    canonical was the first look vote (HIR-0046)."""
    from types import SimpleNamespace

    import anyio

    from tests.architecture.test_staged_architecture import _claim
    from vfx_harness.agents.builder import (
        _executable_unit_verdict,
        _judge_unit_or_layer,
    )
    from vfx_harness.domain.work_units import WorkUnit

    def _boom(*_args, **_kwargs):
        raise AssertionError("look without image domain must not call _judge")

    monkeypatch.setattr("vfx_harness.agents.builder._judge", _boom)
    monkeypatch.setattr(
        "vfx_harness.evidence.scene_checks.load_rows",
        lambda _folder, _selected=None: [],
    )
    row = {
        "id": "materials_energy",
        "title": "Materials",
        "plan": "plans/units/materials_energy.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["lookdev.material_primary"],
            "controls": [],
            "script_spans": ["build/units/02/materials_energy.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 72,
            "judge": [{"frame": 72, "ref": "refs/f072.png"}],
            "temporal_evidence": "none",
            "claims": [_claim("materials_energy", frame=72)],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material", "color"],
    }
    unit = WorkUnit.parse(row, "unit.materials_energy")
    axes = [("material_energy_language", "palette")]
    evidence = [{"id": "contract.materials_energy", "pass": True, "authoritative": True}]
    executable = _executable_unit_verdict(unit, 72, axes, evidence)
    assert executable is not None
    assert executable["pass"] is False
    assert executable["decided_by"] == "look_without_image_domain"
    assert executable["issues"] == []

    shot = SimpleNamespace(folder=SimpleNamespace())
    milestone = SimpleNamespace(frame=72, id="2")

    async def _run():
        return await _judge_unit_or_layer(
            shot,
            milestone,
            "renders/2@materials_energy_canonical_f72.png",
            axes,
            session=None,
            verbose=False,
            scope=None,
            evidence=evidence,
            active_unit=unit,
        )

    verdict = anyio.run(_run)
    assert verdict["pass"] is False
    assert verdict["contract_gap"] is True
    assert verdict["issues"] == []
    assert verdict["decided_by"] == "look_without_image_domain"


def test_look_owning_image_contract_still_seals_on_executable_evidence() -> None:
    from tests.architecture.test_staged_architecture import _claim
    from vfx_harness.agents.builder import _executable_unit_verdict
    from vfx_harness.domain.work_units import WorkUnit

    claim = _claim("surfacing")
    claim["asserts"] = "image"
    claim["evidence"] = [{"kind": "image_contract", "id": "surfacing-look"}]
    row = {
        "id": "surfacing",
        "title": "Surfacing",
        "plan": "plans/units/surfacing.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["lookdev.primary_material"],
            "controls": [],
            "script_spans": ["build/units/02/surfacing.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [claim],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material"],
    }
    unit = WorkUnit.parse(row, "unit.surfacing")
    verdict = _executable_unit_verdict(
        unit,
        40,
        [("material_energy_language", "palette")],
        [{"id": "surfacing-look", "pass": True, "authoritative": True}],
    )
    assert verdict is not None and verdict["pass"] is True
    assert verdict["decided_by"] == "unit_executable_evidence"


def test_missing_image_contract_is_unpaid_debt_not_selector_miss() -> None:
    from tests.architecture.test_staged_architecture import _claim
    from vfx_harness.agents.builder import _executable_unit_verdict
    from vfx_harness.domain.work_units import WorkUnit

    look = _claim("surfacing")
    look["asserts"] = "image"
    look["property"] = "render_region_stat"
    look["evidence"] = [{"kind": "image_contract", "id": "surfacing-look"}]
    scene = _claim("surfacing", cid="claim.scene")
    row = {
        "id": "surfacing",
        "title": "Surfacing",
        "plan": "plans/units/surfacing.md",
        "depends_on": [],
        "mutates": {
            "mode": "scoped",
            "roles": ["lookdev.primary_material"],
            "controls": [],
            "script_spans": ["build/units/02/surfacing.py"],
        },
        "protects": {
            "selector": "all_active_upstream_interfaces",
            "resolve_to_explicit_ids_at": "freeze",
        },
        "evaluation": {
            "primary_judge": 40,
            "judge": [{"frame": 40, "ref": "refs/f040.png"}],
            "temporal_evidence": "none",
            "claims": [scene, look],
        },
        "completion": "all_required_claims_and_protected_contracts_pass",
        "look_capabilities": ["material"],
    }
    unit = WorkUnit.parse(row, "unit.surfacing")
    missing = _executable_unit_verdict(
        unit,
        40,
        [("material_energy_language", "palette")],
        [{"id": "contract.surfacing", "pass": True, "authoritative": True}],
    )
    assert missing is not None and missing["pass"] is False
    assert missing["contract_gap"] is True
    joined = " ".join(missing["issues"])
    assert "surfacing-look" in joined
    assert "unpaid image-contract debt" in joined
    assert "check:" not in joined
    sealed = _executable_unit_verdict(
        unit,
        40,
        [("material_energy_language", "palette")],
        [
            {"id": "contract.surfacing", "pass": True, "authoritative": True},
            {"id": "surfacing-look", "pass": True, "authoritative": True},
        ],
    )
    assert sealed is not None and sealed["pass"] is True


def test_look_without_image_contracts_stays_unsettled() -> None:
    """HIR-0044: atmosphere 3/3 existence is not a critic handoff."""
    from vfx_harness.agents.builder import look_unsettled_for

    assert look_unsettled_for(set(), ("atmosphere",)) is True
    assert look_unsettled_for({"img-1"}, ("atmosphere",)) is False
    assert look_unsettled_for(set(), ()) is False


def test_probe_preview_adds_draft_only_for_look_units() -> None:
    """HIR-0042: repair 5f4489 diagnosed EEVEE look from Workbench solid and hid
    the shaft occluders that only look like slats in solid."""
    from vfx_harness.agents.builder import probe_preview_modes

    assert probe_preview_modes(()) == ("solid",)
    assert probe_preview_modes([]) == ("solid",)
    assert probe_preview_modes(("atmosphere",)) == ("solid", "draft")
