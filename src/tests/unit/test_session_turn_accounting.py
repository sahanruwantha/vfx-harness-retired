"""The budget is reported against the counter it actually bounds.

HIR-0199 read hansa run 20260904T143358Z-238376 -- `turns=14` against `--max-turns 12`,
terminated `success` -- as proof that the CLI's counter is not what the budget bounds, and
replaced it with a count of the assistant messages the harness sees. Across 168 sessions in
this repository's artifacts that inference is wrong: the CLI counter overshoots its cap by
at most 3, the replacement reached 2.03x its budget on a `success` session, and the one
session where the cap actually engaged reported `num_turns` 39 against a cap of 38 --
exactly `cap + 1`, with the replacement counter at 75.

So `num_turns` is the bounded quantity and is reported over the budget. The stream count
stays, because it is available live where `num_turns` arrives only with the result, but it
counts `AssistantMessage` values -- a median 1.55 per CLI turn -- and is named for that
(HIR-0228).

HIR-0199's other half stands and is what made this measurable: one constructor declares the
budget, pinned by the last test in this module.
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
    assert session_turns.accounting() == {"assistant_messages": 0, "budget": 18}

    # Options without a turn budget declare none rather than inheriting the last one.
    sdk_options(model="claude-sonnet-5")
    assert session_turns.accounting()["budget"] is None


def test_the_stream_counter_counts_assistant_messages_and_resets_per_session() -> None:
    """The old name for this test said `observed_turns_count_assistant_messages`.

    That name states the defect: it counted messages and reported them as turns. The
    quantity is unchanged; only its name and what it is compared against are (HIR-0228).
    """
    session_turns.begin(4)
    for _ in range(3):
        session_turns.observe_assistant_message()
    assert session_turns.accounting() == {"assistant_messages": 3, "budget": 4}

    session_turns.reset_observed()
    assert session_turns.accounting() == {"assistant_messages": 0, "budget": 4}, (
        "a new session under the same client keeps its declared budget"
    )

    for value in (0, -1, True, None, "8"):
        session_turns.begin(value)
        assert session_turns.accounting()["budget"] is None, value


def test_the_result_line_reports_the_bounded_counter_over_the_budget(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from vfx_harness.observability import costlog

    monkeypatch.setattr(log.transcript, "message", lambda _m: None)
    monkeypatch.setattr(costlog, "record", lambda _m: None)
    session_turns.begin(18)
    # Through the public path, not the counter's API: on the pre-fix tree this test must
    # fail on the reported line, not on a missing symbol.
    assistant = type("Assistant", (), {"content": ()})
    monkeypatch.setattr(log, "AssistantMessage", assistant)
    monkeypatch.setattr(log, "SystemMessage", type("Sys", (), {}))
    for _ in range(9):
        log.log_message(assistant())

    result = SimpleNamespace(
        subtype="success", num_turns=14, duration_ms=360567, total_cost_usd=0.6705
    )
    monkeypatch.setattr(log, "ResultMessage", type(result))
    monkeypatch.setattr(log, "AssistantMessage", type("Other", (), {}))
    log.log_message(result)

    line = next(text for text in capsys.readouterr().out.splitlines() if "done:" in text)
    # `num_turns` is what `--max-turns` bounds, so it is the numerator over the budget.
    # This line asserted `turns=9/18` and `cli_num_turns=14`, pairing the budget with a
    # counter it does not bound and relegating the one it does (HIR-0228).
    assert "turns=14/18" in line, line
    assert "assistant_messages=9" in line, "the stream count stays, named for what it counts"
    assert "turns=9/18" not in line, "the budget must not be paired with the message count"


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


@pytest.mark.parametrize(
    "messages, cli_turns, budget",
    [(9, 14, 18), (75, 39, 38), (76, 41, 38), (1, 1, 4), (120, 60, 96)],
)
def test_the_budget_denominator_is_always_the_cli_counter(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    messages: int,
    cli_turns: int,
    budget: int,
) -> None:
    """One quantity, one comparison, across the real range seen in the corpus.

    A line-level sweep cannot check this: the pre-fix expression built `turns=` and
    `/{budget}` on two separate source lines, so a textual scan for both on one line
    passed against the very code it was written to catch. The values here are real --
    (75, 39, 38) is the one session where the cap engaged, (76, 41, 38) the largest
    message-count overshoot at 2.03x budget (HIR-0228).
    """
    from vfx_harness.observability import costlog

    monkeypatch.setattr(log.transcript, "message", lambda _m: None)
    monkeypatch.setattr(costlog, "record", lambda _m: None)
    session_turns.begin(budget)
    assistant = type("Assistant", (), {"content": ()})
    monkeypatch.setattr(log, "AssistantMessage", assistant)
    monkeypatch.setattr(log, "SystemMessage", type("Sys", (), {}))
    for _ in range(messages):
        log.log_message(assistant())

    result = SimpleNamespace(
        subtype="success", num_turns=cli_turns, duration_ms=1000, total_cost_usd=0.1
    )
    monkeypatch.setattr(log, "ResultMessage", type(result))
    monkeypatch.setattr(log, "AssistantMessage", type("Other", (), {}))
    log.log_message(result)

    line = next(text for text in capsys.readouterr().out.splitlines() if "done:" in text)
    assert f"turns={cli_turns}/{budget}" in line, line
    assert f"assistant_messages={messages}" in line, line
    if messages != cli_turns:
        # Only meaningful where the two counters differ. They coincided in 2 of 168
        # corpus sessions, so the degenerate case is real and is not a violation.
        assert f"{messages}/{budget}" not in line, (
            "the message count must never appear over the budget: it is not what "
            f"max_turns bounds, and reached {76 / 38:.2f}x it on a successful session"
        )


def test_the_bounded_counter_is_the_one_handed_to_the_cli() -> None:
    """The plumbing, stated once: budget -> SDK -> `--max-turns` -> CLI -> `num_turns`.

    This is why `num_turns` is the bounded quantity, and it is checkable rather than
    assumed: the installed SDK forwards `max_turns` to the CLI as `--max-turns`, and the
    CLI reports its own counter back on the result message.
    """
    from claude_agent_sdk._internal.transport import subprocess_cli

    source = Path(subprocess_cli.__file__).read_text(encoding="utf-8")
    assert '"--max-turns"' in source, (
        "the SDK no longer forwards max_turns to the CLI; re-derive which counter the "
        "budget bounds before trusting the result line (HIR-0228)"
    )
