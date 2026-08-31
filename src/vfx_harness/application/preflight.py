"""Check the things that make a run pointless BEFORE it spends anything.

    python -m vfx_harness.application.preflight            # check and report
    python -m vfx_harness.application.preflight --strict   # exit 1 if anything is wrong

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

Because the raw SDK precedence prefers the API key, the harness applies its own selection
in load_environment(): the subscription token is the default, and ANTHROPIC_API_KEY is
withheld from the process environment when both are configured. VFXH_CREDENTIAL=api_key
restores the API key explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

from vfx_harness.domain.environment_results import EnvironmentCheck, EnvironmentResult
from vfx_harness.domain.stop_envelope_primitives import canonical_digest
from vfx_harness.domain.stop_envelopes import StopCause, StopEnvelope, StopIdentity
from vfx_harness.domain.stop_transaction_state import (
    EnvironmentResultAssertion,
    StopEvidenceRef,
)
from vfx_harness.domain.stop_transactions import (
    EnvironmentReverified,
    RecoverEnvironmentTarget,
    StopAction,
)
from vfx_harness.infrastructure.config import Settings, credential_preference, load_environment
from vfx_harness.observability.log import log

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
            if credential_preference() == "api_key":
                notes.append("both credentials are set; VFXH_CREDENTIAL=api_key selects "
                             "the API key, so billing goes to API credits, not the "
                             "subscription.")
            else:
                problems.append(
                    "both credentials are visible although the preference is oauth — "
                    "load_environment() has not applied credential selection on this "
                    "path, so the SDK would use the API key. Call load_environment() "
                    "before spending.")
    elif "CLAUDE_CODE_OAUTH_TOKEN" in present:
        using = "CLAUDE_CODE_OAUTH_TOKEN"
        notes.append("using the subscription token (harness default; set "
                     "VFXH_CREDENTIAL=api_key to bill API credits instead). A "
                     "subscription that hits its monthly spend limit fails as a "
                     "ZERO-COST 'success' — see empty_success().")
    else:
        logged_in = bool(shutil.which("claude"))
        problems.append(
            "no credential in the environment: neither ANTHROPIC_API_KEY nor "
            "CLAUDE_CODE_OAUTH_TOKEN is set."
            + (" A `claude` CLI is installed, so an interactive login may still work, "
               "but nothing here can confirm it." if logged_in else ""))

    return {"ok": not problems, "using": using, "problems": problems, "notes": notes,
            "present": sorted(present), "decoys": sorted(decoys)}


def _command_path(command: str) -> str | None:
    candidate = Path(command).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = candidate.resolve()
        return str(resolved) if resolved.is_file() and os.access(resolved, os.X_OK) else None
    return shutil.which(command)


def _safe_auth() -> dict:
    try:
        return auth()
    except ValueError as exc:
        return {
            "ok": False,
            "using": None,
            "problems": [str(exc)],
            "notes": [],
            "present": sorted(name for name in _READ if os.environ.get(name)),
            "decoys": sorted(name for name in _DECOYS if os.environ.get(name)),
        }


def check(blender: str | None = None) -> dict:
    a = _safe_auth()
    configuration = {"ok": True, "problems": []}
    configured_blender = blender
    try:
        settings = Settings.from_environment(load_dotenv_file=False)
        configured_blender = configured_blender or settings.blender_bin
    except (OSError, ValueError) as exc:
        configuration = {"ok": False, "problems": [str(exc)]}
    configured_blender = configured_blender or "blender"
    resolved_blender = _command_path(configured_blender)
    blender_result = {
        "ok": resolved_blender is not None,
        "requested": configured_blender,
        "resolved": resolved_blender,
        "problems": (
            []
            if resolved_blender is not None
            else [f"Blender executable {configured_blender!r} is not available or executable."]
        ),
    }
    return {
        "ok": bool(a["ok"] and configuration["ok"] and blender_result["ok"]),
        "auth": a,
        "configuration": configuration,
        "blender": blender_result,
    }


def probe(blender: str | None = None) -> dict:
    """Load configuration and return every strict preflight failure as typed-safe data."""
    try:
        load_environment()
    except (OSError, ValueError) as exc:
        configuration = {"ok": False, "problems": [str(exc)]}
        a = _safe_auth()
        requested = blender or os.environ.get("BLENDER_BIN") or "blender"
        resolved = _command_path(requested)
        blender_result = {
            "ok": resolved is not None,
            "requested": requested,
            "resolved": resolved,
            "problems": (
                []
                if resolved is not None
                else [f"Blender executable {requested!r} is not available or executable."]
            ),
        }
        return {
            "ok": False,
            "auth": a,
            "configuration": configuration,
            "blender": blender_result,
        }
    return check(blender)


def environment_result(value: dict) -> EnvironmentResult:
    """Compile secret-free preflight facts into the standalone strict result schema."""
    auth_result = value.get("auth") if isinstance(value, dict) else None
    configuration = value.get("configuration") if isinstance(value, dict) else None
    blender = value.get("blender") if isinstance(value, dict) else None
    if not all(isinstance(row, dict) for row in (auth_result, configuration, blender)):
        raise ValueError("preflight result requires auth, configuration, and Blender observations")
    assert isinstance(auth_result, dict)
    assert isinstance(configuration, dict)
    assert isinstance(blender, dict)
    safe_observation = {
        "schema": "vfx-harness.auth-observation/v1",
        "using": auth_result.get("using"),
        "present": list(auth_result.get("present") or []),
        "decoys": list(auth_result.get("decoys") or []),
        "problems": list(auth_result.get("problems") or []),
        "notes": list(auth_result.get("notes") or []),
    }
    auth_passed = bool(auth_result.get("ok"))
    problems = safe_observation["problems"]
    auth_found = (
        f"Credential selection is valid and uses {safe_observation['using']}."
        if auth_passed
        else f"Credential configuration has {len(problems)} blocking problem(s): "
        + "; ".join(str(problem) for problem in problems)
    )
    configuration_safe = {
        "schema": "vfx-harness.configuration-observation/v1",
        "problems": list(configuration.get("problems") or []),
    }
    configuration_passed = bool(configuration.get("ok"))
    configuration_found = (
        "Runtime configuration parsed successfully."
        if configuration_passed
        else "Runtime configuration is invalid: "
        + "; ".join(str(problem) for problem in configuration_safe["problems"])
    )
    blender_safe = {
        "schema": "vfx-harness.blender-observation/v1",
        "requested": blender.get("requested"),
        "resolved": blender.get("resolved"),
        "problems": list(blender.get("problems") or []),
    }
    blender_passed = bool(blender.get("ok"))
    blender_found = (
        f"Blender resolves to {blender_safe['resolved']}."
        if blender_passed
        else "; ".join(str(problem) for problem in blender_safe["problems"])
    )
    return EnvironmentResult(
        probe_id="preflight",
        checks=(
            EnvironmentCheck(
                check_id="credential_configuration",
                passed=auth_passed,
                observed_digest=canonical_digest(safe_observation),
                expected="Exactly one valid selected credential is visible to the model runtime.",
                found=auth_found,
                next_action=(
                    "No environment recovery is required."
                    if auth_passed
                    else "Correct the named credential variables, then run strict preflight again."
                ),
            ),
            EnvironmentCheck(
                check_id="runtime_configuration",
                passed=configuration_passed,
                observed_digest=canonical_digest(configuration_safe),
                expected="Runtime configuration parses under the current strict settings schema.",
                found=configuration_found,
                next_action=(
                    "No configuration recovery is required."
                    if configuration_passed
                    else "Correct the named configuration value, then run strict preflight again."
                ),
            ),
            EnvironmentCheck(
                check_id="blender_executable",
                passed=blender_passed,
                observed_digest=canonical_digest(blender_safe),
                expected="The configured Blender executable resolves to an executable file.",
                found=blender_found,
                next_action=(
                    "No Blender recovery is required."
                    if blender_passed
                    else "Install Blender or select a valid executable, then run strict preflight again."
                ),
            ),
        ),
    )


def environment_stop(layout, result: EnvironmentResult) -> StopEnvelope:
    """Turn a failed run-scoped preflight into recovery-only terminal authority."""
    if not isinstance(result, EnvironmentResult) or result.ok:
        raise ValueError("environment_stop requires a failed EnvironmentResult")
    failed = tuple(check for check in result.checks if not check.passed)
    document = result.as_dict()
    evidence_path = layout.write_report("preflight-environment-result", document)
    try:
        observed = EnvironmentResult.from_dict(
            json.loads(evidence_path.read_text(encoding="utf-8")),
            "run preflight environment result",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("run-scoped preflight evidence failed typed read-back") from exc
    if observed != result:
        raise RuntimeError("run-scoped preflight evidence changed during publication")
    evidence = StopEvidenceRef(
        kind="environment_result",
        locator=evidence_path.relative_to(layout.shot).as_posix(),
        sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        record_schema=EnvironmentResult.SCHEMA,
        record_digest=result.digest,
    )
    assert result.probe_spec is not None
    probe_spec_digest = result.probe_spec.digest
    environment = EnvironmentResultAssertion(
        probe_id=result.probe_id,
        probe_spec_digest=probe_spec_digest,
        result_digest=result.digest,
    )
    failed_check_ids = tuple(check.check_id for check in failed)
    target = RecoverEnvironmentTarget(
        environment=environment,
        failed_check_ids=failed_check_ids,
        recovery_adapter_id="external_operator",
        evidence=(evidence,),
    )
    action = StopAction(
        target=target,
        postcondition=EnvironmentReverified(
            probe_id=result.probe_id,
            probe_spec_digest=probe_spec_digest,
            before_result_digest=result.digest,
            failed_check_ids=failed_check_ids,
        ),
    )
    return StopEnvelope(
        stage="infrastructure",
        stop_class="infrastructure_failure",
        identity=StopIdentity(
            run_id=layout.run_id,
            bundle_digest=None,
            view_digest=None,
            layer_id=None,
            unit_id=None,
            unit_plan_digest=None,
            unit_digest=None,
            candidate_digest=None,
            checkpoint_digest=None,
            settings_digest=None,
            debt_state_digest=None,
        ),
        cause=StopCause(
            invariant_id="strict_preflight_failed",
            finding_ids=tuple(check.check_id for check in failed),
            owner_scope_ids=("environment",),
            normalized_facts_digest=result.environment_digest,
        ),
        attempt_evidence_digest=result.digest,
        classification_evidence_digest=result.environment_digest,
        artifact_state_digest=result.environment_digest,
        authoritative_before_digest=result.environment_digest,
        actions=(action,),
        evidence_refs=(evidence,),
        budget_key="environment-recovery",
        expected="Strict preflight passes before any paid execution begins.",
        found="; ".join(check.found for check in failed),
        next_action="Recover the environment and re-run strict preflight.",
    )


def report(d: dict) -> str:
    a = d["auth"]
    L = ["── preflight ──"]
    L.append(f"   auth: {'using ' + a['using'] if a['using'] else 'NO CREDENTIAL'}")
    for n in a["notes"]:
        L.append(f"     · {n}")
    for p in a["problems"]:
        L.append(f"   ✗ {p}")
    configuration = d.get("configuration") or {"ok": False, "problems": ["not checked"]}
    L.append(f"   config: {'valid' if configuration['ok'] else 'INVALID'}")
    for problem in configuration["problems"]:
        L.append(f"   ✗ {problem}")
    blender = d.get("blender") or {
        "ok": False,
        "requested": None,
        "resolved": None,
        "problems": ["not checked"],
    }
    L.append(
        f"   blender: {blender['resolved'] if blender['ok'] else blender['requested'] or 'UNSET'}"
    )
    for problem in blender["problems"]:
        L.append(f"   ✗ {problem}")
    if d["ok"]:
        L.append("   ✓ nothing to fix")
    return "\n".join(L)


def warn_if_broken() -> bool:
    """Print problems if there are any. Returns True when everything is fine.

    Called at the top of the stages that spend money. Deliberately does NOT raise: a
    false positive here must never be able to block a run, and the decoy list is a
    heuristic. It only has to be LOUD.
    """
    d = check()
    if d["ok"]:
        return True
    log("── PREFLIGHT PROBLEMS ──")
    for p in [
        *d["auth"]["problems"],
        *d["configuration"]["problems"],
        *d["blender"]["problems"],
    ]:
        log(f"✗ {p}", 1)
    log("  fix these first: a misconfigured credential does not fail loudly, it fails "
        "as a zero-cost 'success' that the pipeline then critiques as if it were work.", 1)
    return False


def empty_success(
    info: dict, tool_calls: int, *, prior_cost: float = 0.0
) -> str | None:
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
    spent = max(0.0, float(info.get("cost") or 0.0) - float(prior_cost or 0.0))
    if spent > 0.0:
        return None
    return (f"the session reported '{info.get('subtype')}' after "
            f"{info.get('turns', 0)} turn(s) having spent $0.00 in this phase and "
            f"called ZERO tools. "
            f"That is not a build — it is the shape an auth or spend-limit failure takes "
            f"(the limit message arrives as ordinary assistant text and the result still "
            f"says success). Run `python -m vfx_harness.application.preflight`.")


def model_phase_failure(
    info: dict, tool_calls: int, *, prior_cost: float = 0.0
) -> str | None:
    """Return a provider/zero-work failure hidden behind a success subtype.

    Provider error facts outrank the SDK subtype even when useful work preceded the
    terminal error: the phase did not reach its own completion boundary.
    """
    subtype = str(info.get("subtype") or "unknown")
    api_status = info.get("api_error_status")
    if subtype in {"success", "unknown", "None"} and (
        bool(info.get("is_error")) or api_status not in (None, "", 0)
    ):
        suffix = f" (HTTP {api_status})" if api_status not in (None, "", 0) else ""
        return (
            f"the provider marked this terminal result as an error{suffix} despite "
            f"SDK subtype '{subtype}'. The phase is incomplete and downstream judgment "
            "must not consume it."
        )
    return empty_success(info, tool_calls, prior_cost=prior_cost)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vfx_harness.application.preflight")
    ap.add_argument("--strict", action="store_true", help="exit 1 if anything is wrong")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--output", type=Path, help="write the typed environment result to this path")
    a = ap.parse_args(argv)
    d = probe()
    typed = environment_result(d)
    serialized = json.dumps(typed.as_dict(), indent=2, sort_keys=True) + "\n"
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = a.output.with_name(a.output.name + f".tmp.{os.getpid()}")
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, a.output)
    if a.json or a.strict:
        print(serialized, end="")
    else:
        print(report(d))
    return 1 if (a.strict and not typed.ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
