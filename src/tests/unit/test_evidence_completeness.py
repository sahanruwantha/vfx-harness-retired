"""Required evidence cannot pass by absence.

`may_seal` was presence-based: it required at least one current-layer contract and no
failures. A contract that was never evaluated — selector matched nothing, probe errored,
frame group never ran — is neither, so a unit could seal while a required claim had no
evidence at all. Run 20260823T154920Z reported "2/4 pass" against a six-contract view
and nothing asked which two were missing."""

from __future__ import annotations

from vfx_harness.blender.tools import _scene_completion_state


def _row(cid: str, *, owner: str = "1", passes: bool = True) -> dict:
    return {"id": cid, "authoritative": True, "owner_layer": owner, "pass": passes}


def test_absent_required_evidence_blocks_sealing() -> None:
    evidence = [_row("produced")]
    required = {"produced", "never_evaluated"}

    state = _scene_completion_state(evidence, "1", required)

    assert state["missing"] == ["never_evaluated"]
    assert state["may_seal"] is False
    assert state["interfaces_ready"] is False
    assert not state["failures"]  # the point: absence is not a failure row


def test_complete_and_passing_evidence_still_seals() -> None:
    evidence = [_row("a"), _row("b")]

    state = _scene_completion_state(evidence, "1", {"a", "b"})

    assert state["missing"] == []
    assert state["may_seal"] is True


def test_failing_evidence_still_blocks_when_complete() -> None:
    evidence = [_row("a"), _row("b", passes=False)]

    state = _scene_completion_state(evidence, "1", {"a", "b"})

    assert state["missing"] == []
    assert state["may_seal"] is False


def test_legacy_units_without_a_required_set_are_unchanged() -> None:
    """A layer with no declaring unit cannot know its required ids; behaviour there
    stays presence-based rather than failing closed on unknowable completeness."""
    evidence = [_row("a")]

    state = _scene_completion_state(evidence, "1", None)

    assert state["missing"] == []
    assert state["may_seal"] is True


def test_upstream_only_evidence_never_seals_the_current_layer() -> None:
    evidence = [_row("upstream", owner="0")]

    state = _scene_completion_state(evidence, "1", {"upstream"})

    assert state["may_seal"] is False  # no current-layer contract
    assert state["interfaces_ready"] is True


def test_scene_pass_with_unpaid_image_debts_is_not_selector_miss_or_handoff() -> None:
    """HIR-0048: 5/5 scene rows with two unpaid look ids is a payment note."""
    from vfx_harness.blender.tools import followup_after_image_gate_pass, followup_after_scene_contracts_pass

    state = {
        "look_unsettled": False,
        "image_evidence_required": True,
        "unpaid_image_debts": [
            {
                "id": "materials-energy-look-f72",
                "frame": 72,
                "property": "render_region_stat",
                "axis": "material_energy_language",
            },
            {
                "id": "materials-energy-look-f150",
                "frame": 150,
                "property": "render_region_stat",
                "axis": "material_energy_language",
            },
        ],
    }
    note = followup_after_scene_contracts_pass(state)
    assert "SCENE CONTRACTS PASS" in note
    assert "IMAGE-CONTRACT DEBTS UNPAID" in note
    assert "materials-energy-look-f72" in note
    assert "materials-energy-look-f150" in note
    assert "CRITIC HANDOFF" not in note
    assert "selector matching no object" not in note
    image_note = followup_after_image_gate_pass(state, [])
    assert "IMAGE-CONTRACT DEBTS UNPAID" in image_note
    assert "CRITIC HANDOFF" not in image_note


def test_unmeasurable_metric_reads_as_inapplicable_not_failed() -> None:
    """`smooth_fraction` over a camera rig reads None because there is no mesh to shade.
    Reporting that as "fails" sent two repair rounds after something no build could fix
    (run 20260824T060927Z). A metric that could not be measured is a binding defect."""
    import inspect

    from vfx_harness.agents import builder

    source = inspect.getsource(builder._scene_contract_issue)
    assert 'if row.get("value") is None:' in source
    assert "INAPPLICABLE to its subject" in source
    assert "binding defect, not a build defect" in source
    assert "re-materialization, not a repair" in source


def test_projection_failure_names_its_reason() -> None:
    """`bbox_height` read None at f1 with 50 housing objects demonstrably in frame, and
    the report said only "could not be measured" — a live-scene probe was the only way
    to learn more. The evidence row's own error must reach the builder, and the
    projection must distinguish no-objects from none-in-front."""
    import inspect

    from vfx_harness.agents import builder
    from vfx_harness.evidence import scene_checks

    assert (
        'why = str(row.get("error") or row.get("note") or "").strip()'
        in inspect.getsource(builder._scene_contract_issue)
    )

    probe = inspect.getsource(scene_checks._blender_probe)
    assert "selector matched no objects" in probe
    assert "intersects the camera frustum" in probe
    # points must not leak across objects: a failed to_mesh reused the previous
    # object's vertices and projected geometry that was never selected.
    assert "mesh=None; points=[]" in probe
