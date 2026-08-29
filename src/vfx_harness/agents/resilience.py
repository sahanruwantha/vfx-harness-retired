"""Retrying an agent session — and, more importantly, knowing when NOT to.

A plan pass costs ~$9 and ~25 minutes. Two consecutive repair rounds were lost to this:

    Exception: Claude Code returned an error result: success
    cost=$0.0007 · 0 tool calls · target artifact byte-identical before and after

The exception says "success", the cost says nothing happened, and the actual cause was in
the session's own assistant text, one line down:

    API Error: Repeated 529 Overloaded errors. The API is at capacity.

So the failure a caller sees carries none of the information needed to decide what to do
about it. Worse, it is indistinguishable at that layer from the failure this repo already
documents — a subscription over its spend limit also returns `subtype=success` at
near-zero cost. One of those is worth retrying in twenty seconds and the other will fail
identically forever, and retrying it ten times is how a bad credential turns into a
half-hour of silence.

Hence classification before backoff. A retry loop that cannot tell "come back later" from
"this will never work" is not resilience, it is a slower way to fail.

    TRANSIENT  529 / 503 / 429 / overloaded / capacity / timeout — back off and retry
    TERMINAL   401 / auth / invalid key / credit balance / spend limit — raise immediately,
               naming what to fix
    UNKNOWN    retried ONCE, then raised. An unrecognised error is more likely a real bug
               than a blip, and hammering it hides the stack trace that would fix it.

Success is judged by the POST-CONDITION the caller names (for a plan pass: did its target get
written), never by the absence of an exception — because the exception said "success".
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

import anyio

from vfx_harness.observability.log import log

# Matched against the exception text AND the session's own assistant output, because the
# useful string lives in the latter: the SDK reported `error result: success` while the
# transcript said "Repeated 529 Overloaded errors".
_TRANSIENT = re.compile(
    r"\b(429|500|502|503|504|529)\b|overload|at capacity|rate.?limit|too many requests|"
    r"timed? ?out|timeout|temporarily|try again|connection (reset|error|aborted)|"
    r"server error", re.IGNORECASE)
_TERMINAL = re.compile(
    r"\b(401|403)\b|invalid[ _-]?(x-)?api[ _-]?key|authentication|unauthorized|"
    r"credit balance|insufficient (credit|funds|quota)|spend limit|billing|"
    r"usage limits?|specified api usage|regain access|permission denied|not authorized",
    re.IGNORECASE,
)
# Both shapes the SDK produces: the ResultMessage subtype (`error_max_turns`) and the
# ProcessError prose ("Reached maximum number of turns (12)"), which run 22d8ab proved
# arrives as a raised exception whose text is all the classifier gets — the collected
# session signal is discarded on the exception path.
_MAX_TURNS = re.compile(
    r"error_max_turns|max(?:imum)?(?:[ _-]| number of )?turns", re.IGNORECASE
)
_USAGE_LIMIT = re.compile(
    r"credit balance|insufficient (?:credit|funds|quota)|spend limit|billing|"
    r"usage limits?|specified api usage|regain access",
    re.IGNORECASE,
)

_BASE_DELAY = 20.0      # 529s clear in tens of seconds, not milliseconds
_MAX_DELAY = 240.0


class AgentSessionFailure(RuntimeError):
    """Terminal model-session failure with a stable machine-readable cause."""

    def __init__(self, message: str, terminal_cause: str):
        self.terminal_cause = terminal_cause
        super().__init__(message)


def result_signal(message: object) -> str | None:
    """The SDK reports max-turn and error terminations only as `ResultMessage.subtype`,
    which text-block collection misses — the transcript records it, but `classify` never
    saw it, so a real exhaustion read as an unknown empty failure, burned one retry
    session, and was mislabeled `session_stalled`. Session collectors append this to the
    signal they hand `run_session`."""
    if type(message).__name__ != "ResultMessage":
        return None
    subtype = getattr(message, "subtype", None)
    is_error = bool(getattr(message, "is_error", False))
    api_status = getattr(message, "api_error_status", None)
    fields = [f"subtype={subtype}"] if subtype else []
    if is_error:
        fields.append("is_error=true")
    if api_status not in (None, "", 0):
        fields.append(f"api_error_status={api_status}")
    return f"[session result: {' '.join(fields)}]" if fields else None


def classify(text: str) -> str:
    """TERMINAL wins over TRANSIENT: an auth failure that mentions a timeout is still an
    auth failure, and retrying it burns the wall clock the user is waiting on."""
    if _TERMINAL.search(text or ""):
        return "terminal"
    if _TRANSIENT.search(text or ""):
        return "transient"
    return "unknown"


async def run_session(
    attempt_fn: Callable[[], Awaitable[str]],
    *,
    succeeded: Callable[[], bool],
    label: str = "session",
    attempts: int = 4,
    base_delay: float = _BASE_DELAY,
    accept_max_turns_if_succeeded: bool = False,
) -> None:
    """Run `attempt_fn` until `succeeded()` is true, backing off on transient failures.

    `attempt_fn` runs one session and returns whatever text it collected (assistant output),
    which is where the real error message lives. `succeeded` is the caller's post-condition
    — an exception-free run that produced nothing is a FAILURE here, which is precisely the
    case that cost two rounds. Turn exhaustion fails closed even when a generic
    post-condition holds: a written candidate is not a select (HIR-0027). One caller may
    opt into accepting exhaustion only when its post-condition is an explicit,
    revision-bound terminal attestation rather than candidate existence (HIR-0108).
    """
    last = ""
    for n in range(1, attempts + 1):
        err = ""
        try:
            said = await attempt_fn()
        except Exception as e:
            said, err = "", str(e)
        # Exhaustion is a failed transaction even when the caller’s post-condition
        # already holds (a candidate file exists). Checking succeeded() first let
        # remat5 (975cb6) return, then publish_materialization raise on the dirty
        # document — terminal_cause process_error, not max_turns_exhausted.
        blob = f"{err}\n{said}"
        if _MAX_TURNS.search(blob):
            if accept_max_turns_if_succeeded and succeeded():
                log(
                    f"{label}: final model turn carried a valid terminal attestation; "
                    "accepting the attested revision"
                )
                return
            last = (err or said or "produced no output and raised nothing").strip()[:300]
            raise AgentSessionFailure(
                f"{label} exhausted its model turn budget: {last}",
                "max_turns_exhausted",
            )
        if succeeded():
            if n > 1:
                log(f"{label}: succeeded on attempt {n}/{attempts}")
            return
        # No exception and no output is the shape that reports success having done nothing.
        kind = classify(blob)
        last = (err or said or "produced no output and raised nothing").strip()[:300]
        if kind == "terminal":
            log(f"! {label}: TERMINAL failure, not retrying — {last}")
            cause = "usage_limit" if _USAGE_LIMIT.search(blob) else "terminal_service_error"
            raise AgentSessionFailure(f"{label} failed terminally: {last}", cause)
        if n == attempts or (kind == "unknown" and n >= 2):
            break
        delay = min(base_delay * (2 ** (n - 1)), _MAX_DELAY)
        log(f"! {label}: {kind} failure on attempt {n}/{attempts}, retrying in "
            f"{delay:.0f}s — {last[:160]}")
        await anyio.sleep(delay)

    raise AgentSessionFailure(
        f"{label} failed after {attempts} attempt(s) and produced nothing. Last signal: "
        f"{last}" + ("\n(unrecognised error — retried once only, on the reasoning that an "
                     "unknown failure is more likely a bug than a blip)"
                     if classify(last) == "unknown" else ""),
        "session_stalled" if classify(last) == "unknown" else "service_unavailable",
    )
