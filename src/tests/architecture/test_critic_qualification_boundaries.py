"""Qualification value parsing cannot acquire filesystem or execution authority."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "vfx_harness"


@pytest.mark.parametrize("path", ["domain/critic_qualification.py", "domain/work_units/claims.py"])
def test_qualification_domain_has_no_io_or_runtime_dependencies(path):
    tree = ast.parse((ROOT / path).read_text())
    allowed_standard = {"__future__", "dataclasses", "typing", "json", "hashlib", "math", "re"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "pathlib":
                assert {alias.name for alias in node.names} <= {"PurePosixPath"}
            else:
                assert module in allowed_standard or module.startswith("vfx_harness.domain."), module
        elif isinstance(node, ast.Import):
            assert all(alias.name in allowed_standard for alias in node.names)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "eval", "exec", "__import__"}
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"open", "read_bytes", "read_text", "write_bytes", "write_text"}
