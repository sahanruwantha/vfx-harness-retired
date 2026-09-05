"""The terminal materialization action, driven through the tool the model calls.

Two shots lost complete candidates to a `TypeError` inside `finalize_materialization`:
the handler omitted `shot_folder`, a `None` default let the omission through the
signature, and it died four frames deep in `Path(None)`. The suite had 2724 passing
tests and none of them imported this handler, so nothing exercised the terminal action
through the boundary a materialization session actually uses.

Structural tests catch the specific shape (HIR-0218's architecture pair asserts every
call site passes the argument). They cannot catch the next failure inside this path.
What distinguishes a working finalize from a crashed one, from the session's point of
view, is whether the result is *typed*: either it publishes, or it returns findings the
model can act on. An unstructured exception string is neither, and it is what made both
sessions retry against something deterministic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from tests.unit.test_plan_records import (
    _add_deferred_layer,
    _candidate,
    _jit_payload,
    publish_current,
)
from vfx_harness.agents.plan_tools.materialize_mcp import register_materialize_tools
from vfx_harness.observability import run_artifacts


def _finalize_tool(shot: Path, candidate: Path, layout):
    """Build the handler with the exact eight keys the factory closes over."""
    namespace = SimpleNamespace(
        materialization_revision_token=None,
        materialization_axis_ids=("polish",),
        materialization_layer_id="2",
        materialization_allowed_provides=None,
        materialization_requirement_statements={},
    )
    tools = register_materialize_tools(
        shot_folder=shot,
        layout=layout,
        ns=namespace,
        overlay_root=None,
        candidate_materialization=str(candidate),
        materialization_write_lock=anyio.Lock(),
        _resolve=lambda value: Path(value),
        _keep=lambda *a, **k: None,
    )
    for tool in tools:
        name = getattr(tool, "name", "") or getattr(getattr(tool, "handler", None), "__name__", "")
        if "finalize" in str(name):
            return tool
    raise AssertionError("finalize_materialization tool not registered")


def _call(tool, args):
    handler = getattr(tool, "handler", tool)
    return asyncio.run(handler(args))


def _text_of(result) -> str:
    if isinstance(result, dict):
        content = result.get("content") or []
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    return str(result)


def test_finalize_materialization_returns_a_typed_result_not_a_crash(tmp_path: Path) -> None:
    """HIR-0218/0220: the terminal action answers in the vocabulary the session reads.

    The assertion is deliberately not 'it passes' — a fixture candidate may legitimately
    carry findings. It is that the answer is *typed*: JSON-pointer findings or a
    publication, never a bare exception. `expected str, bytes or os.PathLike object,
    not NoneType` is the exact string two sessions retried against, twelve and five
    times, because it carried no pointer to act on.
    """
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "mcp-finalize")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    candidate = _jit_payload(tmp_path, bundle.content_hash)

    tool = _finalize_tool(tmp_path, candidate, layout)
    text = _text_of(_call(tool, {}))

    # The load-bearing half: a raw interpreter error is not an answer a session can use.
    for untyped in ("NoneType", "os.PathLike", "Traceback", "TypeError", "AttributeError"):
        assert untyped not in text, text
    # And it answers in the harness's own vocabulary — a publication, an addressed
    # finding, or a named gate refusal. This fixture legitimately stops at the last:
    # layer 2 cannot materialize before layer 1 holds a terminal finalization receipt,
    # which is itself evidence the handler reached real validation rather than dying
    # before it.
    typed = (
        "VALIDATION", "/layer/", "/scene_contracts/", "/requirement_bindings",
        "attest", "gate failed", "cannot materialize", "receipt",
    )
    assert any(marker in text for marker in typed), text


def test_finalize_materialization_reaches_content_validation(tmp_path: Path) -> None:
    """A defect planted in the candidate must come back as an addressed finding.

    hansa's driver diagnosed the outage by deliberately corrupting `activates_at` and
    observing the identical crash, proving the failure preceded content evaluation.
    This asserts the opposite property directly: a content defect is seen and reported
    against the pointer that carries it.
    """
    _candidate(tmp_path)
    _add_deferred_layer(tmp_path)
    layout = run_artifacts.create(tmp_path, "mcp-finalize-content")
    bundle = publish_current(tmp_path, layout, outcome="clean_with_deferred")
    candidate = _jit_payload(tmp_path, bundle.content_hash)

    document = json.loads(candidate.read_text(encoding="utf-8"))
    rows = document.get("scene_contracts") or []
    if not rows:
        pytest.skip("fixture candidate carries no scene contract to corrupt")
    rows[0]["activates_at"] = "999"
    candidate.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")

    text = _text_of(_call(_finalize_tool(tmp_path, candidate, layout), {}))

    assert "NoneType" not in text, text
    assert "999" in text or "activates_at" in text, text
