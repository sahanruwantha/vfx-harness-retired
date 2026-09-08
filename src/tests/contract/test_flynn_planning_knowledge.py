"""Bounded native JIT knowledge and questions preserve exact VFX scope."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import flynn_agents_sdk as flynn
import pytest

from tests.contract.test_flynn_materialization_tools import bound as bound
from tests.contract.test_flynn_plan_publication import ObservationOnly
from vfx_harness.agents import flynn_planning_knowledge
from vfx_harness.knowledge import planning_vocabulary, recipe_lookup, recipes
from vfx_harness.orchestration import escalate
from vfx_harness.orchestration.authority_selection import SelectedAuthorityResolutionError
from vfx_harness.orchestration.plan_bundle_integrity import PlanPublicationError


def execute(bound, steps, *, check=lambda: None, interfere=lambda _: None, unit_id=None, layer_id="2"):
    layout = bound[0]
    results = []

    class Adapter:
        index = 0

        async def generate(self, request):
            name, arguments = steps[self.index]
            interfere(self.index)
            self.index += 1
            return flynn.InferenceResult.scripted(flynn.ToolCall(name, json.dumps(arguments)))

    async def run():
        tools, guard = flynn_planning_knowledge.planning_knowledge_tools(
            layout=layout, layer_id=layer_id, unit_id=unit_id, check_current=check,
        )
        grants = tuple(tool.name for tool in tools)
        with flynn.SQLiteRun.create(layout.checkpoints / "knowledge.sqlite", run_id="knowledge",
                                   initial_state="unaccepted", limits=flynn.RunLimits(10, 10, 10)) as journal:
            session = flynn.Session(
                inference=Adapter(), tools=flynn.ToolBroker(tools), evaluator=ObservationOnly(), run=journal,
                grants=grants, guards=(guard,),
                on_step=lambda step: results.append(flynn.ToolResult.from_json(step.candidate.output)),
                policy=lambda view: (flynn.SessionStop("fixture complete") if view.completed_steps == len(steps)
                                     else flynn.SessionStep("Inspect planning knowledge", grants)),
            )
            await session.execute()
    asyncio.run(run())
    return results


def question(**overrides):
    return {"question": "Which visual treatment?", "assumption": "Neutral treatment",
            "why_it_matters": "Client taste changes the proposed finish", "affected_layers": ["2"],
            "affected_axes": ["final_lock"], "global_decision": False, **overrides}


def test_vocabulary_index_and_details_match_shared_registry(bound):
    index, detail = execute(bound, [("evidence_vocabulary", {"kind": ""}),
                                    ("evidence_vocabulary", {"kind": "node_socket_value"})])
    catalog = planning_vocabulary.evidence_vocabulary()
    index_row, detail_row = (json.loads(result.data_json) for result in (index, detail))
    assert index_row["selection"]["kinds"] == {name: row["domain"] for name, row in catalog["kinds"].items()}
    assert detail_row["selection"]["definition"] == catalog["kinds"]["node_socket_value"]["definition"]
    assert detail_row["selection"]["operators"] == catalog["operators"]
    assert index_row["vocabulary_sha256"] == detail_row["vocabulary_sha256"]
    assert len(detail.content[0].text) < flynn_planning_knowledge.MAX_RESPONSE_CHARACTERS
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.remaining() == {"inference": 8, "tool": 8, "external": 10}
        assert journal.records()["commits"] == []


def test_recipe_rereads_are_charged_and_the_seventh_read_refuses(bound, monkeypatch):
    monkeypatch.setattr(recipes, "_all", lambda: [{
        "name": "fixture-api", "when": "fixture lookup", "tags": ["api"], "verified": True,
        "body": "Overview.\n```python\n# bounded code\nx = 1\n```",
    }])
    results = execute(bound, [("find_recipe", {"query": "fixture-api#snippet-1"})] * 7)
    for index, result in enumerate(results[:6]):
        record = json.loads(result.data_json)
        assert result.status == "ok"
        assert record["used"] == ["fixture-api"]
        assert record["remaining_reads"] == 5 - index
        assert not record["mutation_authorized"]
    assert results[0].content == results[1].content
    first, second = (json.loads(item.data_json) for item in results[:2])
    assert second["remaining_characters"] < first["remaining_characters"]
    assert results[-1].status == "refused"
    assert json.loads(results[-1].data_json)["status"] == "budget_refused"


def test_unit_recipe_scope_can_abstain_without_broadening_mutation(bound, monkeypatch):
    monkeypatch.setattr(recipes, "_all", lambda: [{
        "name": "fixture-light", "when": "fixture light", "tags": ["light"], "verified": True,
        "requires_roles": ["light.*"], "body": "Do not return this body.",
    }])
    result = execute(bound, [("find_recipe", {"query": "fixture-light#full"})], layer_id="1", unit_id="lock")[0]
    assert result.status == "refused"
    assert json.loads(result.data_json)["status"] == "out_of_scope"
    assert "Do not return this body" not in result.content[0].text


def test_recipe_size_refusal_does_not_emit_partial_code(bound, monkeypatch):
    monkeypatch.setattr(recipes, "_all", lambda: [{
        "name": "fixture-api", "when": "fixture", "tags": ["api"], "verified": True, "body": "x" * 13000,
    }])
    result = execute(bound, [("find_recipe", {"query": "fixture-api#full"})])[0]
    assert result.status == "refused"
    record = json.loads(result.data_json)
    assert record["remaining_reads"] == 5 and record["remaining_characters"] == 12000
    assert record["used"] == []
    assert "xxx" not in result.content[0].text


def test_duplicate_question_returns_stored_assumption_without_approval(bound):
    first, second = execute(bound, [("ask_supervisor", question()),
                                    ("ask_supervisor", question(assumption="Changed assumption"))])
    a, b = (json.loads(result.data_json) for result in (first, second))
    assert a["question"]["id"] == b["question"]["id"]
    assert b["question"]["assumption"] == "Neutral treatment"
    assert b["created"] is False and b["plan_authority_changed"] is False
    path = bound[0].shot / escalate.QUESTIONS
    records = escalate.parse_questions(path.read_bytes(), path)
    assert len(records) == 1
    assert not (bound[0].shot / escalate.ANSWERS).exists()
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.records()["commits"] == []
        assert journal.remaining()["external"] == 8


@pytest.mark.parametrize("name,args", [
    ("evidence_vocabulary", {"kind": "invented"}),
    ("evidence_vocabulary", {"kind": {}}),
    ("evidence_vocabulary", {"kind": "", "path": "../shot.json"}),
    ("find_recipe", {"query": " "}),
    ("find_recipe", {"query": "x" * 257}),
    ("ask_supervisor", question(affected_layers=["unknown"])),
    ("ask_supervisor", question(affected_layers=[], affected_axes=[])),
])
def test_invalid_requests_refuse_before_tool_reservation(bound, name, args):
    with pytest.raises(flynn.ProposalRejected):
        execute(bound, [(name, args)])
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.remaining() == {"inference": 9, "tool": 10, "external": 10}


@pytest.mark.parametrize("damage", ["owner", "brief", "decision"])
def test_changed_scope_refuses_after_inference_before_execution(bound, damage):
    live = True

    def check():
        if not live:
            raise ValueError("owner released")

    def interfere(_):
        nonlocal live
        if damage == "owner":
            live = False
        elif damage == "brief":
            with (bound[0].shot / "brief.md").open("a") as stream:
                stream.write("Changed brief")
        else:
            (bound[0].shot / "state/plan-resolutions.jsonl").write_text("{}\n")

    with pytest.raises((ValueError, PlanPublicationError, SelectedAuthorityResolutionError)):
        execute(bound, [("ask_supervisor", question())], check=check, interfere=interfere)
    assert not (bound[0].shot / escalate.QUESTIONS).exists()
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        assert journal.remaining()["tool"] == journal.remaining()["external"] == 10


def test_question_owner_loss_after_preparation_prevents_commit(bound, monkeypatch):
    live = True
    original = escalate.prepare_question

    def prepare(*args, **kwargs):
        nonlocal live
        result = original(*args, **kwargs)
        live = False
        return result

    def check():
        if not live:
            raise ValueError("owner released")

    monkeypatch.setattr(escalate, "prepare_question", prepare)
    with pytest.raises(ValueError, match="owner released"):
        execute(bound, [("ask_supervisor", question())], check=check)
    assert not (bound[0].shot / escalate.QUESTIONS).exists()


def test_native_knowledge_imports_without_claude():
    result = subprocess.run([sys.executable, "-c", "import sys; sys.modules['claude_agent_sdk'] = None; "
                             "from vfx_harness.agents.flynn_planning_knowledge import planning_knowledge_tools"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_distinct_recipe_fragment_budget_refuses_reconstruction(bound, monkeypatch):
    body = "First notes.\n```python\nx = 1\n```\nSecond notes.\n```python\ny = 2\n```"
    monkeypatch.setattr(recipes, "_all", lambda: [{
        "name": "fixture-api", "when": "fixture", "tags": ["api"], "verified": True, "body": body,
    }])
    results = execute(bound, [("find_recipe", {"query": f"fixture-api#{section}"})
                              for section in ("notes-1", "snippet-1", "notes-2", "snippet-2", "notes-1")])
    assert [item.status for item in results] == ["ok", "ok", "ok", "refused", "ok"]
    assert json.loads(results[3].data_json)["status"] == "fragment_refused"
    assert len(json.loads(results[4].data_json)["selected_fragments"]) == 3


def test_recipe_character_budget_charges_repeated_exposures(bound, monkeypatch):
    monkeypatch.setattr(recipes, "_all", lambda: [{
        "name": "fixture-api", "when": "fixture", "tags": ["api"], "verified": True, "body": "x" * 5000,
    }])
    results = execute(bound, [("find_recipe", {"query": "fixture-api#full"})] * 3)
    assert [item.status for item in results] == ["ok", "ok", "refused"]
    assert json.loads(results[2].data_json)["status"] == "size_refused"


def test_recipe_owner_loss_during_lookup_returns_no_observation(bound, monkeypatch):
    live = True

    def lookup(query, roles):
        nonlocal live
        live = False
        return recipe_lookup.RecipeLookup("unpublished recipe", ("fixture",), "found")

    def check():
        if not live:
            raise ValueError("recipe owner released")

    monkeypatch.setattr(recipe_lookup, "lookup", lookup)
    with pytest.raises(ValueError, match="recipe owner released"):
        execute(bound, [("find_recipe", {"query": "fixture"})], check=check)
    with flynn.SQLiteRun.open(bound[0].checkpoints / "knowledge.sqlite") as journal:
        operation = journal.records()["operations"][0]
        # SDK retains an uncertain dispatch when a tool raises; no result is invented.
        assert operation["stage"] == "dispatched"
        assert operation["output"] is None
        assert journal.remaining() == {"inference": 9, "tool": 9, "external": 10}
        assert not journal.records()["commits"]
