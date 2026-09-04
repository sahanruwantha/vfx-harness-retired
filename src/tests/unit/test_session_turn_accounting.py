"""The harness counts the turns it budgets, and says so beside the CLI's own counter.

hansa run 20260904T143358Z-238376 reported `turns=14` against `--max-turns 12` and
terminated `success`; caesar reported 11 against the same cap. The budget was denominated in
a unit the harness never observed, so no operator could tell whether it bound anything
(HIR-0199).
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from vfx_harness.agents.sdk_options import sdk_options
from vfx_harness.observability import log, session_turns

SRC = Path(__file__).resolve().parents[2] / "vfx_harness"


def test_options_construction_declares_its_budget() -> None:
    session_turns.begin(None)
    options = sdk_options(max_turns=18, model="claude-sonnet-5")

    assert options.max_turns == 18
    assert session_turns.accounting() == {"observed": 0, "budget": 18}

    # Options without a turn budget declare none rather than inheriting the last one.
    sdk_options(model="claude-sonnet-5")
    assert session_turns.accounting()["budget"] is None


def test_observed_turns_count_assistant_messages_and_reset_per_session() -> None:
    session_turns.begin(4)
    for _ in range(3):
        session_turns.observe_turn()
    assert session_turns.accounting() == {"observed": 3, "budget": 4}

    session_turns.reset_observed()
    assert session_turns.accounting() == {"observed": 0, "budget": 4}, (
        "a new session under the same client keeps its declared budget"
    )

    for value in (0, -1, True, None, "8"):
        session_turns.begin(value)
        assert session_turns.accounting()["budget"] is None, value


def test_the_result_line_reports_observed_over_budget_and_labels_the_cli_counter(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from vfx_harness.observability import costlog

    monkeypatch.setattr(log.transcript, "message", lambda _m: None)
    monkeypatch.setattr(costlog, "record", lambda _m: None)
    session_turns.begin(18)
    for _ in range(9):
        session_turns.observe_turn()

    result = SimpleNamespace(
        subtype="success", num_turns=14, duration_ms=360567, total_cost_usd=0.6705
    )
    monkeypatch.setattr(log, "ResultMessage", type(result))
    monkeypatch.setattr(log, "AssistantMessage", type("Other", (), {}))
    log.log_message(result)

    line = next(text for text in capsys.readouterr().out.splitlines() if "done:" in text)
    assert "turns=9/18" in line, line
    assert "cli_num_turns=14" in line, "the CLI counter stays, labelled as its own"


def test_no_module_builds_sdk_options_with_a_turn_budget_directly() -> None:
    """A second constructor would silently drop the budget declaration again."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "sdk_options.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "ClaudeAgentOptions":
                continue
            if any(keyword.arg == "max_turns" for keyword in node.keywords):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")

    assert offenders == [], (
        "construct SDK options through agents.sdk_options.sdk_options so the turn budget "
        f"is declared to the harness counter (HIR-0199): {offenders}"
    )
