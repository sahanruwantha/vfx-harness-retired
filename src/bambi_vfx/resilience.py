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

from .log import log

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
    r"permission denied|not authorized", re.IGNORECASE)

_BASE_DELAY = 20.0      # 529s clear in tens of seconds, not milliseconds
_MAX_DELAY = 240.0


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
) -> None:
    """Run `attempt_fn` until `succeeded()` is true, backing off on transient failures.

    `attempt_fn` runs one session and returns whatever text it collected (assistant output),
    which is where the real error message lives. `succeeded` is the caller's post-condition
    — an exception-free run that produced nothing is a FAILURE here, which is precisely the
    case that cost two rounds.
    """
    last = ""
    for n in range(1, attempts + 1):
        err = ""
        try:
            said = await attempt_fn()
        except Exception as e:
            said, err = "", str(e)
        if succeeded():
            if n > 1:
                log(f"{label}: succeeded on attempt {n}/{attempts}")
            return
        # No exception and no output is the shape that reports success having done nothing.
        blob = f"{err}\n{said}"
        kind = classify(blob)
        last = (err or said or "produced no output and raised nothing").strip()[:300]
        if kind == "terminal":
            log(f"! {label}: TERMINAL failure, not retrying — {last}")
            raise RuntimeError(f"{label} failed terminally: {last}")
        if n == attempts or (kind == "unknown" and n >= 2):
            break
        delay = min(base_delay * (2 ** (n - 1)), _MAX_DELAY)
        log(f"! {label}: {kind} failure on attempt {n}/{attempts}, retrying in "
            f"{delay:.0f}s — {last[:160]}")
        await anyio.sleep(delay)

    raise RuntimeError(
        f"{label} failed after {attempts} attempt(s) and produced nothing. Last signal: "
        f"{last}" + ("\n(unrecognised error — retried once only, on the reasoning that an "
                     "unknown failure is more likely a bug than a blip)"
                     if classify(last) == "unknown" else ""))
