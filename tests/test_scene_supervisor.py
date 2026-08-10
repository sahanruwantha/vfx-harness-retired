"""The supervisor — methodology parsing (no SDK) + the routing leaf with fallback (fakes)."""

from __future__ import annotations

import asyncio

import agents.scene_supervisor as sup
from agents.scene_supervisor import Methodology, ShotElement, decide_methodology, parse_methodology
from develop.ledger import BeatEntry, Clip, Intent, Realization
from scene.supervisor import make_supervised_realizer


# --- methodology parsing ------------------------------------------------------------


def test_parse_reads_kind_motion_rationale_challenges():
    m = parse_methodology('{"kind":"plate","motion":false,"rationale":"atmosphere","challenges":["sky","haze"]}')
    assert m.kind == "plate" and m.motion is False
    assert m.rationale == "atmosphere" and m.challenges == ("sky", "haze")


def test_parse_coerces_kind_case_and_extracts_embedded_json():
    m = parse_methodology('sure —\n{"kind":"3d","motion":true}\nthanks')
    assert m.kind == "3D" and m.motion is True


def test_parse_unparseable_falls_back_to_3d():
    m = parse_methodology("no json here", default_motion=False)
    assert m.kind == "3D" and m.motion is False and "fail-safe" in m.rationale


def test_parse_invalid_kind_falls_back_to_3d():
    assert parse_methodology('{"kind":"claymation","motion":true}').kind == "3D"


def test_routing_key_maps_kind_and_motion():
    assert Methodology("plate", False).routing_key == "plate"
    assert Methodology("hybrid", True).routing_key == "hybrid"
    assert Methodology("3D", True).routing_key == "3d_motion"
    assert Methodology("3D", False).routing_key == "3d_still"


# --- per-element breakdown ----------------------------------------------------------


def test_parse_reads_the_element_breakdown():
    m = parse_methodology(
        '{"kind":"3D","motion":false,"elements":['
        '{"name":"data tower","discipline":"3d","note":"labelled server monolith"},'
        '{"name":"storm sky","discipline":"fx","note":"volumetric green storm"}]}'
    )
    assert [e.name for e in m.elements] == ["data tower", "storm sky"]
    assert m.elements[1].discipline == "fx" and "volumetric" in m.elements[1].note


def test_needs_fx_is_true_iff_an_element_is_fx_tagged():
    fx = Methodology("3D", False, elements=(ShotElement("storm", "fx"), ShotElement("tower", "3d")))
    assert fx.needs_fx is True and [e.name for e in fx.fx_elements] == ["storm"]
    clean = Methodology("3D", False, elements=(ShotElement("chair", "3d"),))
    assert clean.needs_fx is False and clean.fx_elements == ()
    assert Methodology("3D", False).needs_fx is False  # no breakdown → no fx


def test_parse_drops_malformed_elements_and_unknown_disciplines():
    m = parse_methodology(
        '{"kind":"3D","motion":true,"elements":['
        '{"name":"","discipline":"fx"},'          # no name → skipped
        '"not-an-object",'                          # wrong type → skipped
        '{"name":"blob","discipline":"claymation"}]}'  # unknown tag → coerced to safe "3d"
    )
    assert [(e.name, e.discipline) for e in m.elements] == [("blob", "3d")]
    assert m.needs_fx is False  # an unknown discipline must never silently conjure an fx pass


def test_parse_tolerates_a_missing_elements_field():
    assert parse_methodology('{"kind":"3D","motion":false}').elements == ()


def test_parse_recovers_a_breakdown_despite_a_stray_semicolon():
    # regression: a real supe reply used ';' instead of ',' after rationale — strict json.loads threw
    # the whole breakdown away and dropped the shot to a default with needs_fx False. It must survive.
    reply = ('{"kind": "hybrid", "motion": true, "rationale": "plate the sky, build the tower."; '
             '"elements": [{"name": "tower", "discipline": "3d"},'
             '{"name": "storm haze", "discipline": "fx", "note": "green shafts; salt dust",}]}')
    m = parse_methodology(reply)
    assert m.kind == "hybrid" and m.needs_fx is True
    assert [(e.name, e.discipline) for e in m.elements] == [("tower", "3d"), ("storm haze", "fx")]
    assert m.fx_elements[0].note == "green shafts; salt dust"  # inner ';' preserved


# --- decide_methodology with a fake query -------------------------------------------


class FakeResult:
    def __init__(self, result, is_error=False, subtype="success"):
        self.result = result
        self.is_error = is_error
        self.subtype = subtype


def _fake_query(result):
    def query(*, prompt, options):  # noqa: A002
        async def gen():
            yield result
        return gen()
    return query


def test_decide_returns_methodology_from_the_reply(monkeypatch):
    monkeypatch.setattr(sup, "ResultMessage", FakeResult)
    monkeypatch.setattr(sup, "query", _fake_query(FakeResult('{"kind":"plate","motion":false,"rationale":"storm sky"}')))
    m = asyncio.run(decide_methodology(subject="green storm skyline"))
    assert m.kind == "plate" and m.motion is False


# --- the routing leaf ---------------------------------------------------------------


def _beat():
    e = BeatEntry(id="b1", intent=Intent("reconstruct", "green storm skyline", "[Doc 1]", "1. Skyline", 0))
    e.realization = Realization("the market rises")
    return e


def _decider(methodology):
    async def decide(*, subject, vo, evidence, reference_images, default_motion, on_message):
        return methodology
    return decide


def _realizer(tag):
    def r(entry):
        return Clip(licence="KNOWN", render_meta={"built_by": tag})
    return r


def test_router_dispatches_to_the_chosen_methodology():
    realizers = {"3d_still": _realizer("3ds"), "3d_motion": _realizer("3dm"), "plate": _realizer("plate")}
    leaf = make_supervised_realizer(realizers=realizers, decide=_decider(Methodology("plate", False, "atmo")))
    clip = asyncio.run(leaf(_beat()))
    assert clip.render_meta["built_by"] == "plate"
    assert clip.render_meta["methodology"] == "plate" and clip.render_meta["routed_via"] == "plate"
    assert clip.render_meta["supe_rationale"] == "atmo"


def test_router_falls_back_when_the_chosen_realizer_is_missing():
    # hybrid decided, but no hybrid realizer → falls back hybrid → plate
    realizers = {"3d_still": _realizer("3ds"), "plate": _realizer("plate")}
    leaf = make_supervised_realizer(realizers=realizers, decide=_decider(Methodology("hybrid", True, "both")))
    clip = asyncio.run(leaf(_beat()))
    assert clip.render_meta["built_by"] == "plate"
    assert clip.render_meta["methodology"] == "hybrid" and clip.render_meta["routed_via"] == "plate"  # kind vs route


def test_router_awaits_async_realizers():
    async def async_realizer(entry):
        return Clip(licence="KNOWN", render_meta={"built_by": "async3dm"})
    leaf = make_supervised_realizer(realizers={"3d_motion": async_realizer}, decide=_decider(Methodology("3D", True)))
    clip = asyncio.run(leaf(_beat()))
    assert clip.render_meta["built_by"] == "async3dm" and clip.render_meta["routed_via"] == "3d_motion"


def test_router_gaps_when_nothing_resolves():
    leaf = make_supervised_realizer(realizers={}, decide=_decider(Methodology("3D", True)))
    clip = asyncio.run(leaf(_beat()))
    assert clip.frames == () and clip.acquisition_gap and "no realizer" in clip.acquisition_gap


# --- the router hands the per-element decision to willing realizers ------------------


def test_router_passes_methodology_to_a_realizer_that_wants_it():
    seen = {}

    def fx_aware(entry, *, methodology):  # a pipeline-building realizer reads needs_fx to add FX
        seen["needs_fx"] = methodology.needs_fx
        return Clip(licence="KNOWN", render_meta={"built_by": "3d", "ran_fx": str(methodology.needs_fx)})

    m = Methodology("3D", False, "storm over tower", elements=(ShotElement("storm", "fx"), ShotElement("tower", "3d")))
    leaf = make_supervised_realizer(realizers={"3d_still": fx_aware}, decide=_decider(m))
    clip = asyncio.run(leaf(_beat()))
    assert seen["needs_fx"] is True  # the FX decision reached the pipeline builder
    assert clip.render_meta["ran_fx"] == "True"
    # and the breakdown is stamped into the trace
    assert clip.render_meta["needs_fx"] == "True" and "storm[fx]" in clip.render_meta["elements"]


def test_router_still_calls_a_plain_realizer_without_methodology():
    # a legacy BeatEntry->Clip realizer is untouched (no methodology kwarg forced on it)
    leaf = make_supervised_realizer(realizers={"3d_still": _realizer("plain")}, decide=_decider(Methodology("3D", False)))
    clip = asyncio.run(leaf(_beat()))
    assert clip.render_meta["built_by"] == "plain"


def test_router_passes_methodology_via_kwargs_realizer():
    def kwargs_realizer(entry, **kw):
        return Clip(licence="KNOWN", render_meta={"got": str("methodology" in kw)})
    leaf = make_supervised_realizer(realizers={"plate": kwargs_realizer}, decide=_decider(Methodology("plate", False)))
    clip = asyncio.run(leaf(_beat()))
    assert clip.render_meta["got"] == "True"
