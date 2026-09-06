"""A contract is re-measured in the medium it was paid in.

hansa_silk_road layer 2, run 20260906T023403Z-ed877b: one contract, one frame, two answers.

    2@hero_facade_canonical_f51.png               unit      frame_detail 5.266  >= 2  PASS
    2@f51_finalization_group_0_canonical_f51.png  composed  frame_detail 1.826  >= 2  FAIL

The first is EEVEE -- black ground, the emissive window grid glowing. The second is
Workbench solid -- white ground, flat grey massing, and the pattern the metric measures is
absent, because solid suppresses materials. The composed group rendered solid because a
judgment debt in it declares `observation_medium: workbench_solid`, and the claims that
group evaluates are `(*unit_claims, *qualitative)` -- every unit's image contracts included.
"""

from __future__ import annotations

from types import SimpleNamespace

# Imported as a MODULE for the same reason as `judgment_debt_models` below: every symbol
# and keyword this change introduces is reached through attribute access, so on the
# pre-fix tree the behavioural test collects and fails on the ValueError it used to
# raise, rather than on an ImportError (see AGENTS.md on discriminators).
from vfx_harness.agents.builder import provisional_judgment
from vfx_harness.agents.builder.provisional_judgment import _composition_judge_unit

# Imported as a MODULE, not by name: every symbol this change introduces is reached
# through attribute access, so on the pre-fix tree the behavioural test below fails on
# the refusal not firing rather than on collection (see AGENTS.md on discriminators).
from vfx_harness.domain import judgment_debt_models


def _claim(claim_id: str, kind: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=claim_id,
        required=True,
        authority="executable_required",
        moments=(51,),
        evidence=(SimpleNamespace(kind=kind, id=f"{claim_id}-row"),),
    )


def _unit(unit_id: str, *, look: tuple[str, ...], kind: str = "image_contract") -> SimpleNamespace:
    return SimpleNamespace(
        id=unit_id,
        look_capabilities=look,
        evaluation=SimpleNamespace(claims=(_claim(f"{unit_id}-claim", kind),)),
        mutates=SimpleNamespace(roles=("hero.mass",), controls=()),
    )


def _layer(units) -> SimpleNamespace:
    return SimpleNamespace(
        id="2",
        judges=((51, "refs/f051.png"),),
        owns=("hero_dominance",),
        stages=tuple(units),
    )


def _decision(medium: str) -> dict:
    return {
        "id": "R15",
        "debt_id": "jd-95b82d78",
        "statement": "the subject reads as the hero",
        "observation_medium": medium,
        "judge_points": ((51, "refs/f051.png"),),
        "axes": ("hero_dominance",),
        "subject_roles": ("hero.mass",),
        "claim_kind": "atomic",
        "property": "subject_appearance",
        "fault_owner": "hero_facade",
    }


def test_a_look_unit_is_judged_in_beauty_and_a_look_less_one_in_solid() -> None:
    assert judgment_debt_models.unit_observation_medium(_unit("facade", look=("lighting",))) == "eevee"
    assert judgment_debt_models.unit_observation_medium(_unit("mass", look=())) == "workbench_solid"


def test_a_solid_debt_does_not_re_measure_an_eevee_image_contract() -> None:
    """The exact hansa shape: the debt's group cannot see what the contract measures.

    Previously this raised. The refusal was correct and insufficient -- it stopped the
    false failure and left the layer unable to compose at all. The claim now belongs to
    the group whose plate can show it (HIR-0241).
    """
    layer = _layer([_unit("hero_facade", look=("lighting",))])

    solid = _composition_judge_unit(layer, (_decision("workbench_solid"),))

    assert solid is not None
    assert solid.judgment_observation_medium == "workbench_solid"
    claim_ids = {claim.id for claim in solid.evaluation.claims}
    assert "hero_facade-claim" not in claim_ids
    # The debt's own qualitative claim is still there: the group has work to do.
    assert any(str(cid).startswith("judgment-debt:") for cid in claim_ids)


def test_the_layer_owes_a_second_group_for_the_medium_the_debt_does_not_cover() -> None:
    layer = _layer([_unit("hero_facade", look=("lighting",))])

    plans = provisional_judgment.composed_group_plans(layer, (_decision("workbench_solid"),))

    assert [medium for _decisions, medium in plans] == ["workbench_solid", "eevee"]
    assert plans[1][0] == ()


def test_that_second_group_measures_the_contract_and_takes_no_look_vote() -> None:
    layer = _layer([_unit("hero_facade", look=("lighting",))])
    beauty = provisional_judgment._composition_judge_unit(layer, (), medium="eevee")

    assert beauty is not None
    assert beauty.judgment_observation_medium == "eevee"
    assert "hero_facade-claim" in {claim.id for claim in beauty.evaluation.claims}
    # No look capability: it measures executable contracts, it does not judge appearance.
    assert beauty.look_capabilities == ()
    assert beauty.provisional_decisions == ()


def test_every_image_contract_lands_in_exactly_one_group() -> None:
    """The property the split exists for, asserted over the groups rather than one of them."""
    layer = _layer(
        [_unit("hero_facade", look=("lighting",)), _unit("hero_mass", look=())]
    )
    plans = provisional_judgment.composed_group_plans(layer, (_decision("workbench_solid"),))

    placements: dict[str, int] = {}
    for decisions, medium in plans:
        composed = provisional_judgment._composition_judge_unit(layer, decisions, medium=medium)
        for claim in composed.evaluation.claims:
            if str(claim.id).endswith("-claim"):
                placements[str(claim.id)] = placements.get(str(claim.id), 0) + 1

    assert placements == {"hero_facade-claim": 1, "hero_mass-claim": 1}


def test_a_matching_medium_still_composes() -> None:
    layer = _layer([_unit("hero_facade", look=("lighting",))])

    composed = _composition_judge_unit(layer, (_decision("eevee"),))

    assert composed is not None
    assert composed.judgment_observation_medium == "eevee"


def test_a_unit_with_no_image_contract_does_not_constrain_the_medium() -> None:
    """A scene claim is computed from the scene, not from the plate, so it is medium-free.

    Without this the guard would refuse every look-less geometry unit sitting beside a
    solid debt, which is the normal and correct arrangement.
    """
    layer = _layer([_unit("hero_mass", look=(), kind="scene_contract")])

    composed = _composition_judge_unit(layer, (_decision("workbench_solid"),))

    assert composed is not None
    assert composed.judgment_observation_medium == "workbench_solid"


def test_a_look_less_unit_paying_an_image_contract_matches_a_solid_debt() -> None:
    layer = _layer([_unit("hero_mass", look=())])

    composed = _composition_judge_unit(layer, (_decision("workbench_solid"),))

    assert composed is not None


def test_the_medium_and_the_render_mode_are_one_mapping() -> None:
    """Three derivations of this existed; the builder now delegates to the domain one."""
    from vfx_harness.agents.builder.evidence import _unit_raster_mode

    for unit in (
        _unit("look", look=("lighting",)),
        _unit("bare", look=()),
        SimpleNamespace(look_capabilities=(), judgment_observation_medium="eevee"),
        SimpleNamespace(look_capabilities=("lighting",), judgment_observation_medium="workbench_solid"),
    ):
        assert _unit_raster_mode(unit) == judgment_debt_models.render_mode_for_medium(
            judgment_debt_models.unit_observation_medium(unit)
        )
