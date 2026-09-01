from __future__ import annotations

import asyncio
import sys

import pytest

from vfx_harness.agents import builder, distill
from vfx_harness.agents.builder import axes


def test_legacy_distillation_consumer_refuses_before_reading_queue(tmp_path) -> None:
    queue = tmp_path / "runs" / "legacy" / "logs" / "distill_queue.jsonl"
    queue.parent.mkdir(parents=True)
    queue.write_text('{"untrusted": "legacy row"}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="unit-completion receipt"):
        asyncio.run(distill._run(tmp_path))

    assert queue.read_text(encoding="utf-8") == '{"untrusted": "legacy row"}\n'


def test_legacy_distillation_cli_refuses_deterministically(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["vfx-distill", str(tmp_path)])

    with pytest.raises(SystemExit, match="receipt-bound staged diff"):
        distill.main()


def test_builder_package_exposes_no_direct_distillation_model_or_writer() -> None:
    assert not hasattr(builder, "distill_recipe")
    assert not hasattr(axes, "distill_recipe")
    assert not hasattr(axes, "query")
