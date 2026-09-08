"""Stage 3 — the build + critic loop for one PLAN LAYER."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    TextBlock,
)

from vfx_harness.agents.builder.models import MAX_BUDGET_USD, MAX_CONTINUES, BuildTruncated
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.state import _APPROACH, _ERRORS
from vfx_harness.application.preflight import model_phase_failure
from vfx_harness.observability import costlog, transcript
from vfx_harness.observability.log import (
    TOOL_USE,
    _result_text,
    log,
    log_message,
)
from vfx_harness.observability.runlog import bump

if TYPE_CHECKING:
    from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard


def _collect_approach(message) -> None:
    if _APPROACH.get("text") or not isinstance(message, AssistantMessage):
        return
    for b in message.content:
        if not isinstance(b, TextBlock):
            continue
        for line in b.text.splitlines():
            if line.strip().upper().startswith("APPROACH:"):
                _APPROACH["text"] = line.split(":", 1)[1].strip()[:400]
                log(f"approach: {_APPROACH['text'][:120]}", 1)
                return


def _collect_errors(message) -> None:
    """Reuse log's extractor: a tool result's content may be a str, a list of dicts, or a
    list of BLOCK OBJECTS. My first version only handled the first two, so object-shaped
    results extracted to "" and were dropped — it collected 1 of 2 errors on BR layer S."""
    for b in getattr(message, "content", []) or []:
        if not getattr(b, "is_error", False):
            continue
        txt = _result_text(b)
        head = txt.strip().splitlines()[0][:200] if txt.strip() else ""
        if head and head not in _ERRORS:
            _ERRORS.append(head)


async def _drain_once(client: ClaudeSDKClient, verbose: bool) -> dict:
    """Consume one builder response; report HOW it ended.

    The SDK signals a truncated loop with subtype='error_max_turns' on the final
    ResultMessage. Discarding it (as this used to) means a build that was cut off
    mid-scene is indistinguishable from one that finished — BR layer G was critiqued,
    scored and recorded 'failed' while half-built. Always look at the subtype.
    """
    info = {
        "subtype": "unknown",
        "turns": 0,
        "cost": 0.0,
        "session_id": None,
        "tokens": {},
        "is_error": False,
        "api_error_status": None,
    }
    idle_seconds = builder_package().Settings.from_environment(load_dotenv_file=False).model_event_idle_seconds
    response = client.receive_response().__aiter__()
    messages_seen = 0
    last_message_type = "response_start"
    while True:
        try:
            with anyio.fail_after(idle_seconds):
                message = await anext(response)
        except StopAsyncIteration:
            break
        except TimeoutError as exc:
            transcript.event(
                "model_event_idle_timeout",
                idle_seconds=idle_seconds,
                messages_seen=messages_seen,
                last_message_type=last_message_type,
            )
            raise BuildTruncated(
                f"builder emitted no SDK event for {idle_seconds}s after "
                f"{last_message_type}; the model session is indeterminate — retry the "
                "unit through the audited retry transition (resume only when its ledger "
                "names an existing checkpoint and journal)",
                terminal_cause="model_session_idle_timeout",
            ) from exc
        messages_seen += 1
        last_message_type = type(message).__name__
        if verbose:
            log_message(message)
        else:
            # Quiet means no console noise, not no durable evidence.  Previously every
            # non-result message vanished from quiet transcripts, including the SDK's
            # compact_boundary notification.
            transcript.message(message)
            if isinstance(message, builder_package().ResultMessage):
                # Accounting is correctness data, not console decoration. Quiet runs
                # still record their sessions even though they skip the pretty-printer.
                costlog.record(message)
        if type(message).__name__ == "SystemMessage" and getattr(message, "subtype", None) == "compact_boundary":
            bump("compaction_completed")
            if not verbose:
                log("↻ context compaction completed; continuing from the SDK summary")
        _collect_errors(message)
        _collect_approach(message)
        if isinstance(message, builder_package().ResultMessage):
            # Token economics lived ONLY in the SDK's session JSONL, outside the repo, so
            # "which layer burned the budget, and was the stable prefix actually cached?"
            # could not be answered from anything the pipeline writes. A collapsing
            # cache-hit rate is the early warning that the cached prefix has been broken.
            u = getattr(message, "usage", None)
            info = {
                "session_id": getattr(message, "session_id", None),
                "subtype": getattr(message, "subtype", "unknown"),
                "turns": getattr(message, "num_turns", 0) or 0,
                # total_cost_usd is cumulative for the session and Optional on error paths
                "cost": getattr(message, "total_cost_usd", None) or 0.0,
                # Provider truth outranks the SDK subtype. A monthly spend limit was
                # observed as subtype=success, is_error=true, api_error_status=429.
                "is_error": bool(getattr(message, "is_error", False)),
                "api_error_status": getattr(message, "api_error_status", None),
                "tokens": {
                    k: (u.get(k) or 0)
                    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                }
                if isinstance(u, dict)
                else {},
            }
    return info


async def _drain(
    client: ClaudeSDKClient,
    verbose: bool,
    *,
    continues: int = MAX_CONTINUES,
    attempt_guard: UnitAttemptGuard,
) -> dict:
    """Drain a builder response, nudging it onward if it hit the turn cap.

    A streaming-input session SURVIVES error_max_turns — the builder is still there
    with its full context, and each new message starts a fresh turn budget. So an
    exhausted cap is a pause, not a death: tell it how much it has spent and let it
    finish. (Single-shot query() raises instead; that is why this only works here.)
    """
    info = await _drain_once(client, verbose)
    attempt_guard.check(f"consume unit builder response ({info['subtype']})")
    for i in range(continues):
        if info["subtype"] != "error_max_turns":
            break
        log(f"⏸ builder hit the turn cap ({info['turns']} turns, ${info['cost']:.2f}) — continuing {i + 1}/{continues}")
        continuation_tools_before = sum(TOOL_USE.values())
        continuation_prior_cost = float(info.get("cost") or 0.0)
        attempt_guard.check(f"query unit builder continuation {i + 1}")
        await client.query(
            f"You have hit a turn checkpoint: {info['turns']} turns and "
            f"${info['cost']:.2f} spent on this layer so far, out of a ${MAX_BUDGET_USD:.0f} "
            f"budget. You have NOT been reset — the scene and your context are intact. "
            f"Continue from exactly where you stopped, but start converging: finish the "
            f"work in progress and prefer landing the layer over further refinement."
        )
        info = await _drain_once(client, verbose)
        attempt_guard.check(
            f"consume unit builder continuation {i + 1} ({info['subtype']})"
        )
        continuation_why = model_phase_failure(
            info,
            sum(TOOL_USE.values()) - continuation_tools_before,
            prior_cost=continuation_prior_cost,
        )
        if continuation_why:
            transcript.event(
                "empty_success",
                phase="turn_continuation",
                why=continuation_why,
                **info,
            )
            raise BuildTruncated(
                f"builder continuation: {continuation_why}",
                terminal_cause="model_session_failure",
            )
    if info["subtype"] == "error_max_turns":
        log(f"! builder still truncated after {continues} continuations ({info['turns']} turns, ${info['cost']:.2f})")
    # Context usage is optional telemetry. The SDK control request can itself wait 60s
    # after a provider result already says HTTP 429/is_error. Return the authoritative
    # terminal facts immediately; the caller will publish the typed phase failure.
    if bool(info.get("is_error")) or info.get("api_error_status") not in (None, "", 0):
        return info
    try:
        usage = await client.get_context_usage()
        compact = {
            "total_tokens": usage.get("totalTokens"),
            "max_tokens": usage.get("maxTokens"),
            "raw_max_tokens": usage.get("rawMaxTokens"),
            "percentage": usage.get("percentage"),
            "auto_compact": usage.get("isAutoCompactEnabled"),
            "auto_compact_threshold": usage.get("autoCompactThreshold"),
            "memory_files": [
                {k: row.get(k) for k in ("path", "type", "tokens") if k in row}
                for row in (usage.get("memoryFiles") or [])
            ],
        }
        transcript.event("context_usage", **compact)
        if float(compact.get("percentage") or 0) >= 70:
            log(
                f"⚠ context {compact['percentage']:.1f}% full "
                f"({compact['total_tokens']}/{compact['max_tokens']} tokens)"
            )
    except Exception as exc:
        # Context telemetry is diagnostic and must never make a completed build fail.
        transcript.event("context_usage_unavailable", error=str(exc)[:160])
    return info


def _extract_json(text: str) -> dict:
    """Pull the last JSON object out of the critic's reply (fenced or bare)."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = fenced or re.findall(r"(\{.*\})", text, re.DOTALL)
    for chunk in reversed(candidates):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise ValueError("critic returned no parseable JSON scorecard")
