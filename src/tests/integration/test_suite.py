"""Repository-wide static checks that must hold on every commit.

These run Ruff as a subprocess from the repository root so a CI job cannot go green
without executing them.  The former print-based `test_harness` script that also lived
here depended on untracked local shots and was retired by decision; every behavioural
check now lives in the discoverable pytest suites under `src/tests/`.
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
