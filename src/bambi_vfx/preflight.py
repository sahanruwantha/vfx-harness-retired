"""Check the things that make a run pointless BEFORE it spends anything.

    python -m bambi_vfx.preflight            # check and report
    python -m bambi_vfx.preflight --strict   # exit 1 if anything is wrong

WHY THIS EXISTS. A key was added to `.env` as `CLAUDE_API_KEY` — a name nothing reads.
The SDK reads `ANTHROPIC_API_KEY`, so the new key was silently ignored, the stale
`CLAUDE_CODE_OAUTH_TOKEN` was used instead, and that subscription had hit its monthly
spend limit. The observable result was a session that reported `subtype=success` with
`cost=$0.0000` and one turn whose entire output was the sentence "You've hit your monthly
spend limit". Nothing in the pipeline treated that as a failure.

That is the worst shape a failure can take here: a build layer would "succeed" having
built nothing, get critiqued anyway, score 2.0, and record a verdict that describes the
billing state of an account rather than anything about the shot. Ten seconds of checking
env var NAMES prevents it.

MEASURED, not assumed (one live call each, both credentials present in the environment):
  ANTHROPIC_API_KEY + CLAUDE_CODE_OAUTH_TOKEN → the API KEY is used
    (model claude-opus-5[1m], cost $0.1461, 3 turns, tools called normally)
  CLAUDE_CODE_OAUTH_TOKEN alone, over its spend limit → model claude-sonnet-5,
    cost $0.0000, 1 turn, text "You've hit your monthly spend limit", subtype success
"""

from __future__ import annotations

import os
import shutil

from .config import load_environment

# What the Agent SDK / Claude Code CLI actually reads, in the precedence measured above.
_READ = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

# Names that look exactly like the real thing and are read by NOTHING. Each is a real
# mistake someone has made or would plausibly make; `CLAUDE_API_KEY` is the one that
# actually happened here.
_DECOYS = {
    "CLAUDE_API_KEY": "ANTHROPIC_API_KEY",
    "ANTHROPIC_KEY": "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN": "ANTHROPIC_API_KEY",
    "CLAUDE_TOKEN": "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_OAUTH_TOKEN": "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_API_KEY": "ANTHROPIC_API_KEY",
}

# A key's prefix says which KIND of credential it is, so a value pasted into the wrong
# variable is catchable without ever sending it anywhere.
_PREFIX = {"sk-ant-api": "API key", "sk-ant-oat": "OAuth token"}


def _kind(val: str) -> str:
    for p, k in _PREFIX.items():
        if val.startswith(p):
            return k
    return "unrecognised prefix"


def auth() -> dict:
    """Which credential will be used, and what is misconfigured."""
    present = {k: os.environ[k] for k in _READ if os.environ.get(k)}
    decoys = {k: os.environ[k] for k in _DECOYS if os.environ.get(k)}
    problems, notes = [], []

    for name, val in decoys.items():
        correct = _DECOYS[name]
        already = correct in present
        problems.append(
            f"{name} is set ({_kind(val)}, {len(val)} chars) but NOTHING READS IT. "
            f"The variable the SDK reads is {correct}."
            + (f" {correct} is already set, so this one is merely dead weight — delete it."
               if already else
               f" {correct} is NOT set, so this credential is being IGNORED and the run "
               f"will use {'the ' + next(iter(present)) if present else 'no credential at all'}."))

    for name, val in present.items():
        want = "API key" if name == "ANTHROPIC_API_KEY" else "OAuth token"
        got = _kind(val)
        if got != want:
            problems.append(f"{name} holds a {got}, but that variable expects a {want} — "
                            f"the two are not interchangeable.")

    using = None
    if "ANTHROPIC_API_KEY" in present:
        using = "ANTHROPIC_API_KEY"
        if "CLAUDE_CODE_OAUTH_TOKEN" in present:
            notes.append("both credentials are set; the API key takes precedence "
                         "(measured), so billing goes to API credits, not the "
                         "subscription.")
    elif "CLAUDE_CODE_OAUTH_TOKEN" in present:
        using = "CLAUDE_CODE_OAUTH_TOKEN"
        notes.append("using the subscription token. A subscription that hits its monthly "
                     "spend limit fails as a ZERO-COST 'success' — see empty_success().")
    else:
        logged_in = bool(shutil.which("claude"))
        problems.append(
            "no credential in the environment: neither ANTHROPIC_API_KEY nor "
            "CLAUDE_CODE_OAUTH_TOKEN is set."
            + (" A `claude` CLI is installed, so an interactive login may still work, "
               "but nothing here can confirm it." if logged_in else ""))

    return {"ok": not problems, "using": using, "problems": problems, "notes": notes,
            "present": sorted(present), "decoys": sorted(decoys)}


def check() -> dict:
    a = auth()
    return {"ok": a["ok"], "auth": a}


def report(d: dict) -> str:
    a = d["auth"]
    L = ["── preflight ──"]
    L.append(f"   auth: {'using ' + a['using'] if a['using'] else 'NO CREDENTIAL'}")
    for n in a["notes"]:
        L.append(f"     · {n}")
    for p in a["problems"]:
        L.append(f"   ✗ {p}")
    if d["ok"]:
        L.append("   ✓ nothing to fix")
    return "\n".join(L)


def warn_if_broken() -> bool:
    """Print problems if there are any. Returns True when everything is fine.

    Called at the top of the stages that spend money. Deliberately does NOT raise: a
    false positive here must never be able to block a run, and the decoy list is a
    heuristic. It only has to be LOUD.
    """
    from .log import log
    d = check()
    if d["ok"]:
        return True
    log("── PREFLIGHT PROBLEMS ──")
    for p in d["auth"]["problems"]:
        log(f"✗ {p}", 1)
    log("  fix these first: a misconfigured credential does not fail loudly, it fails "
        "as a zero-cost 'success' that the pipeline then critiques as if it were work.", 1)
    return False


def empty_success(info: dict, tool_calls: int) -> str | None:
    """Did a 'successful' response actually do nothing? Returns why, or None.

    The observed shape, from a subscription over its spend limit: subtype `success`,
    `cost_usd` 0.0, one turn, no tool calls, and a single sentence of assistant text
    explaining the limit. Every field the pipeline checks said the build was fine.

    The test is cost AND tools, not either alone. Cost can legitimately be absent on some
    paths (it is Optional on error branches), and a cheap turn is not by itself suspicious
    — but a builder that spent nothing and touched no tool has not built anything, and
    that is true regardless of why. Scoring the scene after this buys a critic call to
    discover that an empty scene looks empty.
    """
    if info.get("subtype") not in ("success", None, "unknown"):
        return None                      # a real error path reports itself elsewhere
    if tool_calls > 0:
        return None
    if (info.get("cost") or 0.0) > 0.0:
        return None
    return (f"the session reported '{info.get('subtype')}' after "
            f"{info.get('turns', 0)} turn(s) having spent $0.00 and called ZERO tools. "
            f"That is not a build — it is the shape an auth or spend-limit failure takes "
            f"(the limit message arrives as ordinary assistant text and the result still "
            f"says success). Run `python -m bambi_vfx.preflight`.")


def main(argv: list[str] | None = None) -> int:
    import argparse
    load_environment()
    ap = argparse.ArgumentParser(prog="bambi_vfx.preflight")
    ap.add_argument("--strict", action="store_true", help="exit 1 if anything is wrong")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = check()
    if a.json:
        import json
        print(json.dumps(d, indent=2))
    else:
        print(report(d))
    return 1 if (a.strict and not d["ok"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
