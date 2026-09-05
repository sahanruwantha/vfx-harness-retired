"""A recorded vocabulary gap closes a structural requirement, and blocks padding (HIR-0202).

Caesar run 20260904T143311Z-c0f282, requirement R20 ("reproduce the visual composition, not
the documentary's text labels"), domain projected_composition: the materializer recorded
VG-001 naming three registry kinds and why none can detect burned-in text, was told by the
tool to close with a decision, and the validator answered with two mutually exclusive
findings — "every declared domain already has contract evidence" on a binding with zero
contract ids, and "does not pay ['projected_composition']". The only shape that passed bound
twelve bbox_height rows to a text-absence proposition.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_judgment_debt_materialization import (
    BUNDLE_HASH,
    _camera_payload,
    _fixture_root,
    _form_payload,
    _overlay_camera_view,
    _write_payload,
)
from vfx_harness.orchestration.jit_materialization import validate_materialization
from vfx_harness.orchestration.jit_materialization.validate_requirements import (
    recorded_vocabulary_gap_ids,
)

STATEMENT = "The camera framing reads as authored around the hall."
FORM_STATEMENT = "The hall has a rendered form."


def _record_gap(root: Path, requirement_id: str, gap_id: str = "VG-001") -> None:
    path = root / "state" / "plan-escalations" / "vocabulary-gaps.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "schema": "vfx-harness.vocabulary-gap/v1",
                    "id": gap_id,
                    "requirement_id": requirement_id,
                    "claim": FORM_STATEMENT,
                    "attempted": [
                        {"kind": "node_count", "why_it_cannot_certify": "self-certifying tautology"},
                        {"kind": "render_region_stat", "why_it_cannot_certify": "cannot read glyphs"},
                    ],
                    "note": "",
                    "run_id": "test",
                }
            )
            + "\n"
        )


def _validated_form(root: Path, binding: dict, shot_folder: Path | None = None):
    """Validate the structural (scene-domain) form layer with one requirement binding."""
    shot = root if shot_folder is None else shot_folder
    camera = validate_materialization(
        root, _write_payload(root, "camera.json", _camera_payload()), expected_bundle_hash=BUNDLE_HASH,
        shot_folder=shot,)
    overlay = _overlay_camera_view(root, camera)
    payload = _form_payload()
    payload["requirement_bindings"] = [binding]
    return validate_materialization(
        root,
        _write_payload(root, "form.json", payload),
        expected_bundle_hash=BUNDLE_HASH,
        base_layers_path=overlay / "layers.json",
        base_scene_checks_path=overlay / "scene_checks.json",
        shot_folder=shot,)


def test_gap_records_are_read_by_requirement_id(tmp_path: Path) -> None:
    assert recorded_vocabulary_gap_ids(tmp_path) == {}

    _record_gap(tmp_path, "R20")
    _record_gap(tmp_path, "R20", "VG-002")
    _record_gap(tmp_path, "R31", "VG-003")
    assert recorded_vocabulary_gap_ids(tmp_path) == {
        "R20": ("VG-001", "VG-002"),
        "R31": ("VG-003",),
    }

    # A malformed or foreign row is ignored, never raised, inside a validator.
    path = tmp_path / "state" / "plan-escalations" / "vocabulary-gaps.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
        handle.write(json.dumps({"schema": "other/v1", "id": "X", "requirement_id": "R9"}) + "\n")
    assert "R9" not in recorded_vocabulary_gap_ids(tmp_path)


def test_a_decision_on_a_structural_requirement_is_refused_with_the_true_reason(
    tmp_path: Path,
) -> None:
    """The old message claimed contract evidence existed for a binding that had none."""
    root = _fixture_root(tmp_path)

    with pytest.raises(ValueError) as refused:
        _validated_form(
            root,
            {
                "requirement_id": "R-form",
                "decision": {"statement": FORM_STATEMENT, "decision_strength": "approved_start"},
            },
        )

    message = str(refused.value)
    assert "declares only structural domains" in message
    assert "escalate_vocabulary_gap" in message, "the message names the path that makes it legal"
    assert "already has contract evidence" not in message, (
        "the false finding told the model to delete the only binding it had"
    )


def test_a_recorded_gap_lets_the_decision_pay_the_structural_domain(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _record_gap(root, "R-form")

    validated = _validated_form(
        root,
        {
            "requirement_id": "R-form",
            "decision": {"statement": FORM_STATEMENT, "decision_strength": "approved_start"},
        },
    )

    assert validated is not None, "the escalation path the tool prescribes now terminates in a pass"


def test_a_recorded_gap_refuses_closing_the_same_requirement_with_contracts(
    tmp_path: Path,
) -> None:
    """The padding that actually happened: rows that cannot measure the statement."""
    root = _fixture_root(tmp_path)
    _record_gap(root, "R-form")

    with pytest.raises(ValueError) as refused:
        _validated_form(root, dict(_form_payload()["requirement_bindings"][0]))

    message = str(refused.value)
    assert "recorded vocabulary gap(s) ['VG-001']" in message
    assert "cannot then be closed by contract bindings" in message
    assert "approved_start" in message and "retract the gap" in message


def test_a_recorded_gap_is_read_from_the_shot_folder_not_the_plan_bundle(
    tmp_path: Path,
) -> None:
    """HIR-0218: production's plan bundle root is not the shot folder.

    ``escalate_vocabulary_gap`` writes under ``<shot>/state/plan-escalations/``; the
    validator read ``<bundle>/state/plan-escalations/``. A content-addressed plan bundle
    has no ``state/`` directory, so the read returned ``{}`` in every shot and every run
    — the branch that lets a recorded gap close a structural-only requirement had never
    executed. Every fixture passed one directory as both roots, so the rule passed its
    own tests throughout.

    This asserts the outcome the refusal promises — "a recorded gap makes the decision
    legal here" — with the two directories genuinely apart, because a test of the reader
    alone passes on both sides of the bug.
    """
    shot = tmp_path
    bundle_root = shot / "runs" / "r1" / "checkpoints" / "plans" / "bundles" / "h0"
    bundle_root.mkdir(parents=True)
    root = _fixture_root(bundle_root)
    assert root != shot and not (root / "state").exists()

    # Without the escalation the decision is refused, naming the escalation as the path.
    with pytest.raises(ValueError) as refused:
        _validated_form(
            root,
            {
                "requirement_id": "R-form",
                "decision": {"statement": FORM_STATEMENT, "decision_strength": "approved_start"},
            },
            shot_folder=shot,
        )
    assert "declares only structural domains" in str(refused.value)

    # A gap recorded where the OLD reader looked — inside the plan bundle — is inert.
    # This is the assertion that discriminates the two implementations by behaviour
    # rather than by signature: pre-fix it closed the requirement, post-fix it cannot,
    # because a content-addressed bundle is not where the escalation tool writes.
    _record_gap(root, "R-form")
    with pytest.raises(ValueError) as still_refused:
        _validated_form(
            root,
            {
                "requirement_id": "R-form",
                "decision": {"statement": FORM_STATEMENT, "decision_strength": "approved_start"},
            },
            shot_folder=shot,
        )
    assert "declares only structural domains" in str(still_refused.value)

    # Recording it where the tool actually writes must change that outcome.
    _record_gap(shot, "R-form")
    validated = _validated_form(
        root,
        {
            "requirement_id": "R-form",
            "decision": {"statement": FORM_STATEMENT, "decision_strength": "approved_start"},
        },
        shot_folder=shot,
    )
    assert validated is not None, (
        "the instructed action must change the next outcome; a recorded gap that the "
        "validator cannot see leaves the refusal that prescribed it firing forever"
    )


def test_a_recorded_gap_plus_full_contract_cover_names_the_removal_not_the_escalation(
    tmp_path: Path,
) -> None:
    """HIR-0222: a refusal must not prescribe an action already taken.

    room_1046_opening layer 2 looped on R20 seventeen times. Bind contracts, and the
    gap refuses them and asks for a decision. Bind the decision as well, and — because
    the contracts still cover every declared domain — the decision has nothing to pay,
    and the old message sent the session to `escalate_vocabulary_gap`, which it had
    already called and which changes nothing. Neither refusal named the one action that
    resolves it: remove the contract bindings the gap says cannot measure the statement.
    """
    root = _fixture_root(tmp_path)
    _record_gap(root, "R-form")

    binding = dict(_form_payload()["requirement_bindings"][0])
    binding["decision"] = {
        "statement": FORM_STATEMENT,
        "decision_strength": "approved_start",
    }

    with pytest.raises(ValueError) as refused:
        _validated_form(root, binding)
    message = str(refused.value)

    # It names the gap it already has, and does not ask for another.
    assert "recorded vocabulary gap(s) ['VG-001']" in message
    assert "escalate_vocabulary_gap" not in message
    assert "Do not escalate again" in message
    # It names the action that actually resolves the state.
    assert "remove them from this requirement's contract_ids" in message
    # And it shows the bindings that are blocking the decision.
    assert "already covered by contract bindings" in message


def test_one_predicate_decides_whether_a_decision_pays_a_domain() -> None:
    """HIR-0223: the local validator and the terminal gate ask the same question.

    They asked it independently. Materialization validation widened the payable domains
    by a recorded gap; the terminal gate refused every structural domain unconditionally
    and referenced gaps nowhere. Each was individually correct, and a requirement could
    satisfy the validator and then be refused by the gate for reaching exactly the state
    the validator had prescribed.
    """
    from vfx_harness.domain.vocabulary_gaps import (
        QUALITATIVE_DOMAINS,
        decision_may_pay_domain,
    )

    # A qualitative domain never needs a gap.
    for domain in QUALITATIVE_DOMAINS:
        assert decision_may_pay_domain(domain, gap_ids=())
    # A structural domain needs one, and is paid once it has one.
    for domain in ("scene", "projected_composition", "temporal"):
        assert not decision_may_pay_domain(domain, gap_ids=())
        assert decision_may_pay_domain(domain, gap_ids=("VG-001",))

    # Both boundaries call it rather than restating it.
    import inspect as inspect_module

    from vfx_harness.evaluation.plan_gate import meta
    from vfx_harness.orchestration.jit_materialization import validate_requirements

    for module in (meta, validate_requirements):
        source = inspect_module.getsource(module)
        assert "decision_may_pay_domain" in source, module.__name__
        assert '{"image", "human"}' not in source, (
            f"{module.__name__} restates the domain set instead of sharing it"
        )


def test_a_gap_backed_decision_survives_the_terminal_gate(tmp_path: Path) -> None:
    """The assertion that spans both boundaries, which neither alone provides.

    room_1046_opening reached this state on its own: decision-only bindings for three
    structural requirements, `VALIDATION PASSED` from the local validator, then six
    blocking `requirement-domain-binding` findings from the terminal gate — "provisional
    decision cannot pay structural domain 'projected_composition'". The fix is only
    proven by asserting the same authority clears both.
    """
    root = _fixture_root(tmp_path)
    _record_gap(root, "R-form")

    validated = _validated_form(
        root,
        {
            "requirement_id": "R-form",
            "decision": {"statement": FORM_STATEMENT, "decision_strength": "approved_start"},
        },
    )
    assert validated is not None

    # The gate's own predicate must agree for every domain the requirement declares.
    from vfx_harness.domain.vocabulary_gaps import (
        decision_may_pay_domain,
        recorded_vocabulary_gap_ids,
    )

    gaps = recorded_vocabulary_gap_ids(root)
    assert gaps.get("R-form") == ("VG-001",)
    for domain in ("scene", "projected_composition", "temporal"):
        assert decision_may_pay_domain(domain, gap_ids=gaps["R-form"]), domain
