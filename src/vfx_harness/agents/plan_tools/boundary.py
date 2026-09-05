"""One exception policy for every plan tool, replacing ten hand-picked tuples.

A tool handler catches what it can express as a refusal. Anything that escapes it is,
by definition, something that handler did not expect — a defect, not a finding. The SDK
turns such an escape into an ordinary tool error, so it reaches the model as prose with
no JSON pointer, indistinguishable from a validation refusal it could act on. Sessions
retried them: twelve consecutive `finalize_materialization` calls in one shot over ~360
seconds, five in another at roughly $1.70 each, and a whole run against an organisation
entitlement error that no retry could ever clear.

Both models eventually diagnosed it correctly and stopped — one after deliberately
corrupting its own candidate to prove the failure preceded content evaluation. That is
good behaviour defeated by an untyped result, and it should not be the session's job.

Two mechanisms, neither of them wording:

* **An escape is typed as a defect.** No handler decides which exception types count;
  escaping is the signal. The result names the tool, the exception type and that it is
  not addressable by the session.
* **A repeat does not execute.** The second identical defect from one tool in one
  session returns without calling the handler at all. The bound is the harness's, not
  the model's judgement, so cost stops whether or not the session reasons well.

Refusals are untouched: a handler that returns ``is_error`` prose keeps returning it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

DEFECT_RULE = (
    "This is a harness defect, not a validation finding: it carries no JSON pointer and "
    "no addressable row, so no edit to the candidate can change it. Do not retry this "
    "tool. Report the tool name, the exception type and what you were attempting, and "
    "stop."
)


def _signature(tool_name: str, exc: BaseException) -> str:
    return f"{tool_name}:{type(exc).__name__}:{str(exc)[:200]}"


def guard_tool_boundary(
    tool: Any,
    *,
    on_defect: Callable[[str, BaseException, int], None] | None = None,
) -> Any:
    """Return ``tool`` with its handler wrapped in the shared boundary policy."""

    handler = tool.handler
    seen: dict[str, int] = {}

    def _result(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    async def guarded(args):
        repeated = [key for key in seen if key.startswith(f"{tool.name}:")]
        if repeated:
            # The bound is mechanical: a second call cannot reach the handler, so the
            # cost of a deterministic defect is one attempt plus one refusal, whatever
            # the session concludes.
            return _result(
                f"{tool.name} already failed with an unrecoverable harness defect in "
                f"this session ({repeated[0].split(':', 2)[1]}), and was not called "
                f"again. {DEFECT_RULE}"
            )
        try:
            return await handler(args)
        except Exception as exc:
            key = _signature(tool.name, exc)
            seen[key] = seen.get(key, 0) + 1
            if on_defect is not None:
                on_defect(tool.name, exc, seen[key])
            return _result(
                f"{tool.name} raised {type(exc).__name__}: {exc}. {DEFECT_RULE}"
            )

    return dataclasses.replace(tool, handler=guarded)
