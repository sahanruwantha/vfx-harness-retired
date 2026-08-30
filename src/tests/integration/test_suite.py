"""pytest entry points for the checks that must hold on every commit.

`pyproject.toml` has configured pytest since the project started, and pytest collected
ZERO tests and exited 0 — the real suite is `src/tests/integration/test_harness.py`, a print-based script
with 261 assertions and no `test_*` functions for pytest to find. A CI job running `pytest`
would therefore have gone green without executing a single check, which is worse than
having no CI at all: it reports a guarantee it never verified.

So this module is deliberately thin. It does not re-implement the suite; it runs it as a
subprocess (the harness carries module-level state and reports through stdout, so process
isolation is the honest way to invoke it) and fails on a non-zero exit.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([*argv], cwd=ROOT, capture_output=True, text=True)


def test_deterministic_harness_passes():
    """The 261-check suite: no Blender, no network, no model."""
    p = _run(sys.executable, "-m", "tests.integration.test_harness")
    if p.returncode != 0:
        # The harness prints its own failure list; surface it instead of a bare exit code.
        pytest.fail(f"tests.integration.test_harness exited {p.returncode}\n"
                    f"{p.stdout[-4000:]}\n{p.stderr[-2000:]}")


def test_no_undefined_names():
    """F821 — the rule that would have caught the bug that killed every layer build.

    `build_unit()` called `reset_tool_use()` and `tool_use_summary()` and `build_agent`
    imported neither. Importing the module still worked, because an unbound global is
    resolved only when its line executes, so nothing failed until a real build did. This
    is a separate test from the lint job on purpose: it is a correctness check, and it
    should be the first thing to go red rather than one line in a style report.
    """
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    if not Path(ruff).exists():
        pytest.skip("ruff not installed — run `pip install -e '.[dev]'`")
    p = _run(ruff, "check", "--select", "F821", "--output-format", "concise", ".")
    assert p.returncode == 0, f"undefined names found:\n{p.stdout}"


def test_lint_clean():
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    if not Path(ruff).exists():
        pytest.skip("ruff not installed — run `pip install -e '.[dev]'`")
    p = _run(ruff, "check", "--output-format", "concise", ".")
    assert p.returncode == 0, f"lint findings:\n{p.stdout}"
