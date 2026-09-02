"""HIR-0172 interruption success has one future source-verifying issuer."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "src" / "vfx_harness"
DOMAIN_STATUS = PACKAGE / "domain" / "run_status.py"
DOMAIN_INTERRUPTION = PACKAGE / "domain" / "run_interruption_records.py"
DOMAIN_OWNER_LOSS = PACKAGE / "domain" / "run_owner_loss.py"
EVALUATOR = "evaluation/run_interruption.py"
FUTURE_TERMINALIZER = "observability/run_interruption_terminalizer.py"
FUTURE_OWNER_LOSS_CAPTURE = "observability/run_owner_loss_capture.py"


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(PACKAGE).as_posix()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((row.value for row in call.keywords if row.arg == name), None)


def _literal_interrupted(call: ast.Call) -> bool:
    state = _keyword(call, "state")
    return isinstance(state, ast.Constant) and state.value == "interrupted"


def test_evaluation_has_no_caller_selected_outcome_factory() -> None:
    evaluation = next(
        node
        for node in _tree(DOMAIN_STATUS).body
        if isinstance(node, ast.ClassDef) and node.name == "InterruptionReceiptEvaluation"
    )
    methods = {node.name for node in evaluation.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert "mint" not in methods
    assert "satisfied" not in methods
    assert "issue" not in methods


@pytest.mark.parametrize(
    ("domain_path", "class_name"),
    (
        (DOMAIN_INTERRUPTION, "RunInterruptionReceipt"),
        (DOMAIN_OWNER_LOSS, "RunOwnerLossObservation"),
    ),
)
def test_ephemeral_interruption_facts_have_no_public_issuance_factory(
    domain_path: Path,
    class_name: str,
) -> None:
    record = next(
        node for node in _tree(domain_path).body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    methods = {node.name for node in record.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert "mint" not in methods
    assert "issue" not in methods


@pytest.mark.parametrize(
    ("class_name", "allowed_path"),
    (
        ("RunInterruptionReceipt", FUTURE_TERMINALIZER),
        ("RunOwnerLossObservation", FUTURE_OWNER_LOSS_CAPTURE),
    ),
)
def test_ephemeral_interruption_facts_have_one_future_production_issuer(
    class_name: str,
    allowed_path: str,
) -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and _called_name(node) == class_name and relative != allowed_path:
                violations.append(f"{relative}:{node.lineno}")

    assert not violations, (
        f"{class_name} may be issued only by {allowed_path} while its live capability is held: " + ", ".join(violations)
    )


def test_only_future_evaluator_may_construct_an_evaluation_in_production() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and _called_name(node) == "InterruptionReceiptEvaluation"
                and relative != EVALUATOR
            ):
                violations.append(f"{relative}:{node.lineno}")

    assert not violations, (
        "interruption evaluations may be issued only by the independent evaluator after it "
        "source-verifies their complete archive closure: " + ", ".join(violations)
    )


def test_only_future_evaluator_may_parse_an_evaluation_in_production() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        if relative == EVALUATOR:
            continue
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "from_dict"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "InterruptionReceiptEvaluation"
            ):
                violations.append(f"{relative}:{node.lineno}")

    assert not violations, (
        "a structurally parsed interruption evaluation is not authoritative outside the "
        "independent evaluator that reopens its complete archive closure: " + ", ".join(violations)
    )


def test_only_future_terminalizer_may_construct_run_status_directly() -> None:
    violations: list[str] = []
    for path in _sources():
        relative = _relative(path)
        if relative == FUTURE_TERMINALIZER:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name == "RunStatusV2":
                violations.append(f"{relative}:{node.lineno}:constructor")
                continue
            if (
                _literal_interrupted(node)
                and name == "mint"
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "RunStatusV2"
            ):
                violations.append(f"{relative}:{node.lineno}:mint")

    assert not violations, (
        "direct run-status construction and interrupted status minting belong only "
        "to the future terminalizer after evaluator read-back: " + ", ".join(violations)
    )
