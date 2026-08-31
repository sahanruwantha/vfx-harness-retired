"""No optical signal is an unpaid provisional judgment, not falsification."""

from __future__ import annotations

from types import SimpleNamespace

import anyio
from PIL import Image

from vfx_harness.agents.builder import _judge_unit_or_layer


def test_provisional_composition_no_signal_stays_unpaid_without_critic(
    tmp_path, monkeypatch
) -> None:
    candidate = tmp_path / "renders" / "black.png"
    candidate.parent.mkdir()
    Image.new("RGB", (64, 36), (0, 0, 0)).save(candidate)

    paid_critic_calls = 0

    async def paid_critic(*_args, **_kwargs):
        nonlocal paid_critic_calls
        paid_critic_calls += 1
        raise AssertionError("a no-signal plate must not reach the paid critic")

    monkeypatch.setattr(
        "vfx_harness.agents.builder.critic._critique", paid_critic
    )

    binding_id = "requirement:R-reference-read:form"
    claim = SimpleNamespace(
        id=binding_id,
        required=True,
        moments=(40,),
        authority="qualified_qualitative_required",
        asserts="image",
        evidence=(SimpleNamespace(kind="qualification", id=binding_id),),
    )
    active_unit = SimpleNamespace(
        id="2._composition",
        look_capabilities=(),
        evaluation=SimpleNamespace(
            claims=(claim,),
            judges=(SimpleNamespace(frame=40, ref="refs/f040.png"),),
        ),
        provisional_requirement_ids=("R-reference-read",),
        mutates=SimpleNamespace(roles=("subject.form",)),
    )
    shot = SimpleNamespace(
        folder=tmp_path,
        frames=40,
        frontmatter={"type": "still"},
    )
    milestone = SimpleNamespace(frame=40, id="2", ref="refs/f040.png")

    async def run():
        return await _judge_unit_or_layer(
            shot,
            milestone,
            candidate.relative_to(tmp_path).as_posix(),
            [("form", "reference-specific form")],
            session=None,
            verbose=False,
            scope=None,
            evidence=[],
            active_unit=active_unit,
        )

    verdict = anyio.run(run)

    assert paid_critic_calls == 0
    assert verdict["pass"] is False
    assert verdict["decided_by"] == "no_optical_signal"
    assert verdict["contract_gap"] is False
    assert verdict["signal"]["has_signal"] is False
    assert "contract_gaps" not in verdict
