"""A stop class is not a terminal cause (HIR-0227).

`StopEnvelope.stop_class` says who owns a stop; `terminal_cause` says why the run
ended. The two vocabularies are disjoint. Three sites assigned one to the other, and
the result reached disk: 59 of 115 run summaries in this repository's artifacts carried
a `terminal_cause` that could not be one -- 42 `harness_defect`, 17 `authority_defect`.

These tests parse rather than grep, because a textual sweep for `terminal_cause` matches
the line above an assignment as readily as the assignment itself.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

import pytest

from vfx_harness.observability import unclassified_authority

SRC = Path(__file__).resolve().parents[2] / "vfx_harness"

STOP_CLASSES = frozenset(
    {
        "local_implementation_miss",
        "authority_defect",
        "harness_defect",
        "infrastructure_failure",
        "human_decision_required",
    }
)


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _terminal_cause_assignments() -> list[tuple[Path, int, ast.AST]]:
    """Every value assigned to a `terminal_cause` name, key, or keyword argument."""
    found: list[tuple[Path, int, ast.AST]] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "terminal_cause":
                        found.append((path, getattr(value, "lineno", node.lineno), value))
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "terminal_cause":
                        found.append((path, getattr(kw.value, "lineno", node.lineno), kw.value))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "terminal_cause":
                        found.append((path, node.lineno, node.value))
    return found


def test_the_two_vocabularies_share_no_member() -> None:
    """If they overlapped, assigning one to the other could be accidentally correct."""
    assert not (unclassified_authority.TERMINAL_CAUSES & STOP_CLASSES)


def test_no_literal_terminal_cause_is_a_stop_class() -> None:
    """The exact defect: `"terminal_cause": envelope.stop_class` at three sites."""
    offenders: list[str] = []
    for path, line, value in _terminal_cause_assignments():
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value in STOP_CLASSES
        ):
            offenders.append(f"{path.name}:{line} literal {value.value!r} is a stop class")
        if isinstance(value, ast.Attribute) and value.attr == "stop_class":
            offenders.append(f"{path.name}:{line} assigns .stop_class to terminal_cause")
    assert not offenders, "terminal_cause given a stop class:\n" + "\n".join(offenders)


def test_every_literal_terminal_cause_is_in_the_closed_vocabulary() -> None:
    """A value this repository authors must be sayable; a typo must not degrade."""
    offenders: list[str] = []
    for path, line, value in _terminal_cause_assignments():
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value not in unclassified_authority.TERMINAL_CAUSES
        ):
            offenders.append(f"{path.name}:{line} {value.value!r}")
    assert not offenders, (
        "terminal_cause literals outside TERMINAL_CAUSES:\n"
        + "\n".join(offenders)
        + f"\naccepted: {sorted(unclassified_authority.TERMINAL_CAUSES)}"
    )


@pytest.mark.parametrize("stop_class", sorted(STOP_CLASSES))
def test_authoring_a_stop_class_as_a_cause_is_refused_and_says_why(stop_class: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        unclassified_authority.require_terminal_cause(stop_class, "example.terminal_cause")
    message = str(excinfo.value)
    assert "says who owns the stop" in message
    assert stop_class in message
    # The rejection names the accepted set, not only the offending token.
    assert "process_error" in message


def test_an_unknown_cause_is_refused_rather_than_degraded() -> None:
    """`closed_terminal_cause` stays lenient for observed metadata; authoring does not."""
    assert unclassified_authority.closed_terminal_cause("unaccepted_prior") == (
        "unclassified_terminal_cause"
    )
    with pytest.raises(ValueError, match="must be one of"):
        unclassified_authority.require_terminal_cause("unaccepted_prior", "x.terminal_cause")


def test_terminal_record_only_ever_derives_a_legal_cause() -> None:
    """The AST sweep sees assignments, not return values -- this covers the other path.

    `terminal_record` is what `run_owner_boundary` writes into `summary.json`, and two
    of its literals (`cancelled_without_intent`, and the publication-failure cause one
    frame above it) were outside the vocabulary until this test existed.
    """
    from vfx_harness.observability import run_artifacts

    class _Truncated(RuntimeError):
        terminal_cause = "model_session_idle_timeout"

    class _WithOutcome(RuntimeError):
        run_metadata: ClassVar[dict[str, str]] = {"outcome": "stalled"}

    cases: list[BaseException] = [
        KeyboardInterrupt(),
        SystemExit(3),
        RuntimeError("plain"),
        _Truncated("truncated"),
        _WithOutcome("stalled"),
        run_artifacts.RequestedExit(6, "UNACCEPTED PRIOR — refusing"),
    ]
    for exc in cases:
        _state, _code, cause, _detail = run_artifacts.terminal_record(exc)
        assert cause in unclassified_authority.TERMINAL_CAUSES, (
            f"{type(exc).__name__} derived {cause!r}, which is not a terminal cause"
        )

