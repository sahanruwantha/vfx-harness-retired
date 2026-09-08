"""Application configuration and deterministic ``.env`` loading.

Configuration enters the application through this module.  Library imports must not
mutate ``os.environ``; command entry points call :func:`load_environment` explicitly.
Real process environment values always win over values in a dotenv file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from flynn_agents_sdk.deepseek import VISION_MODEL

ENV_FILE_VARIABLE = "VFXH_ENV_FILE"
CREDENTIAL_VARIABLE = "VFXH_CREDENTIAL"
API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
OAUTH_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
_CREDENTIAL_PREFERENCES = ("oauth", "api_key")
PACKAGE_ROOT = Path(__file__).resolve().parent
# ``PACKAGE_ROOT`` is ``<checkout>/src/vfx_harness/infrastructure`` in a source
# checkout.  ``parents[1]`` is therefore the ``src`` directory, not the checkout.
# Keeping this as an explicit constant (rather than searching cwd/parents) preserves the
# deterministic resolution contract while allowing the repository-root ``.env``, evals,
# shots, and packaged knowledge paths to resolve where the project says they live.
PROJECT_ROOT = PACKAGE_ROOT.parents[2]
DEFAULT_EXECUTION_MODEL = "claude-sonnet-5"
DEFAULT_CRITIC_MODEL = "claude-opus-5"


def environment_file(path: str | Path | None = None) -> Path | None:
    """Resolve the dotenv file without searching arbitrary parent directories.

    Resolution is intentionally predictable: an explicit argument, then
    ``VFXH_ENV_FILE``, then the checkout-root ``.env``.  Installed packages have no
    implicit dotenv file; callers can opt in with ``VFXH_ENV_FILE``.
    """
    configured = path or os.environ.get(ENV_FILE_VARIABLE)
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"{ENV_FILE_VARIABLE} points to no file: {candidate}")
        return candidate

    candidate = PROJECT_ROOT / ".env"
    checkout = (PROJECT_ROOT / "pyproject.toml").is_file()
    return candidate if checkout and candidate.is_file() else None


def dotenv_resolution_note() -> str:
    """Say which dotenv file this process resolved, and how to point it elsewhere.

    A credential set in a checkout's ``.env`` is invisible to a process whose code is
    imported from another checkout (a pinned worktree), because resolution follows the
    code, not the working directory. Telling an operator to "correct the named credential
    variables" then sends them to edit a file that is already correct (HIR-0198).
    """
    explicit = os.environ.get(ENV_FILE_VARIABLE)
    if explicit:
        return f"{ENV_FILE_VARIABLE}={explicit} is the dotenv file this process reads."
    candidate = PROJECT_ROOT / ".env"
    if (PROJECT_ROOT / "pyproject.toml").is_file() and candidate.is_file():
        return f"This process reads {candidate}; the variables must be set there."
    return (
        f"No dotenv file was resolved: this process imports its code from {PROJECT_ROOT}, "
        f"which has no readable .env, so set {ENV_FILE_VARIABLE} to the file that holds "
        "the credentials (for example the primary checkout's .env)."
    )


def credential_preference() -> str:
    """Which Claude credential the harness should hand the Agent SDK."""
    value = os.environ.get(CREDENTIAL_VARIABLE)
    if value is None:
        return "oauth"
    normalized = value.strip().lower()
    if normalized not in _CREDENTIAL_PREFERENCES:
        raise ValueError(
            f"{CREDENTIAL_VARIABLE} must be one of: {', '.join(_CREDENTIAL_PREFERENCES)}"
        )
    return normalized


def _apply_credential_preference() -> None:
    """Select the credential the Agent SDK will see when both are configured.

    The SDK itself prefers ``ANTHROPIC_API_KEY`` over ``CLAUDE_CODE_OAUTH_TOKEN`` (measured
    in application/preflight.py).  The harness default is the subscription token, so when
    both are present the API key is withheld from the process environment unless
    ``VFXH_CREDENTIAL=api_key`` explicitly selects it.  This is cross-variable selection
    policy, not value precedence: a real-environment API key is also withheld when the
    preference says oauth.  With only one credential configured, nothing is removed.
    """
    if credential_preference() != "oauth":
        return
    if os.environ.get(OAUTH_TOKEN_VARIABLE) and os.environ.get(API_KEY_VARIABLE):
        del os.environ[API_KEY_VARIABLE]


def load_environment(path: str | Path | None = None) -> Path | None:
    """Load project configuration once, preserving real environment variables."""
    candidate = environment_file(path)
    if candidate is not None:
        load_dotenv(candidate, override=False)
    _apply_credential_preference()
    return candidate


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: 1/0, true/false, yes/no, on/off")


def _text(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _optional_float(name: str, *, minimum: float = 0.0) -> float | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= minimum:
        raise ValueError(f"{name} must be > {minimum}")
    return parsed


def _int(name: str, default: int, *, minimum: int = 1) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    """Typed, non-secret runtime settings.

    Credentials stay in ``os.environ`` for the SDKs that consume them directly and are
    deliberately excluded from this object to reduce accidental logging.
    """

    blender_bin: str = "blender"
    asset_image_backend: str = "codex"
    higgsfield_bin: str | None = None
    transcript_enabled: bool = True
    distill_inline: bool = False
    execution_model: str = DEFAULT_EXECUTION_MODEL
    planner_model: str = VISION_MODEL
    unit_plan_seconds: int = 600
    unit_plan_output_tokens: int = 32768
    global_planner_model: str = VISION_MODEL
    global_plan_seconds: int = 600
    global_plan_output_tokens: int = 32768
    materialization_model: str = VISION_MODEL
    materialization_seconds: int = 600
    materialization_output_tokens: int = 32768
    builder_model: str = DEFAULT_EXECUTION_MODEL
    script_model: str = DEFAULT_EXECUTION_MODEL
    reviewer_model: str = VISION_MODEL
    asset_model: str = DEFAULT_EXECUTION_MODEL
    distiller_model: str = DEFAULT_EXECUTION_MODEL
    critic_model: str = DEFAULT_CRITIC_MODEL
    # Hard ceiling for one global plan session (draft, verify, or repair). A runaway
    # pass ends at this boundary instead of grinding against unresolvable findings;
    # `vfx plan --max-turns` still overrides per invocation.
    plan_max_turns: int = 12
    plan_verify_max_turns: int = 6
    # Fail closed when an SDK response stream stops producing events.  A turn and
    # spend cap cannot bound a transport/model session that never emits a terminal
    # ResultMessage, so this is deliberately an event-idle deadline rather than a
    # total response deadline.
    model_event_idle_seconds: int = 360
    # Caps for the receipt-backed run controller (ADR-0010). Dispatch stops on identity
    # first; these bound spend when identity alone would not.  ``run_max_usd`` is the
    # run's model spend ceiling read from its cost log; ``None`` leaves only the count caps.
    run_max_dispatches: int = 6
    run_max_replans_per_layer: int = 2
    run_max_usd: float | None = None

    @classmethod
    def from_environment(cls, *, load_dotenv_file: bool = True) -> Settings:
        if load_dotenv_file:
            load_environment()
        execution_model = _text("VFXH_EXECUTION_MODEL", DEFAULT_EXECUTION_MODEL)
        return cls(
            blender_bin=os.environ.get("BLENDER_BIN", "blender"),
            asset_image_backend=os.environ.get("ASSET_IMAGE_BACKEND", "codex").lower(),
            higgsfield_bin=os.environ.get("HIGGSFIELD_BIN") or None,
            transcript_enabled=not _flag("VFXH_NO_TRANSCRIPT"),
            distill_inline=_flag("VFXH_DISTILL_INLINE"),
            execution_model=execution_model,
            planner_model=_text("VFXH_PLANNER_MODEL", _text("DEEPSEEK_MODEL", VISION_MODEL)),
            unit_plan_seconds=_int("VFXH_UNIT_PLAN_SECONDS", 600),
            unit_plan_output_tokens=_int("VFXH_UNIT_PLAN_OUTPUT_TOKENS", 32768),
            global_planner_model=_text("VFXH_GLOBAL_PLANNER_MODEL", _text("DEEPSEEK_MODEL", VISION_MODEL)),
            global_plan_seconds=_int("VFXH_GLOBAL_PLAN_SECONDS", 600),
            global_plan_output_tokens=_int("VFXH_GLOBAL_PLAN_OUTPUT_TOKENS", 32768),
            materialization_model=_text("VFXH_MATERIALIZATION_MODEL", _text("DEEPSEEK_MODEL", VISION_MODEL)),
            materialization_seconds=_int("VFXH_MATERIALIZATION_SECONDS", 600),
            materialization_output_tokens=_int("VFXH_MATERIALIZATION_OUTPUT_TOKENS", 32768),
            builder_model=_text("VFXH_BUILDER_MODEL", execution_model),
            script_model=_text("VFXH_SCRIPT_MODEL", execution_model),
            reviewer_model=_text("VFXH_REVIEWER_MODEL", _text("DEEPSEEK_MODEL", VISION_MODEL)),
            asset_model=_text("VFXH_ASSET_MODEL", execution_model),
            distiller_model=_text("VFXH_DISTILLER_MODEL", execution_model),
            critic_model=_text("VFXH_CRITIC_MODEL", DEFAULT_CRITIC_MODEL),
            plan_max_turns=_int("VFXH_PLAN_MAX_TURNS", 12),
            plan_verify_max_turns=_int("VFXH_PLAN_VERIFY_MAX_TURNS", 6),
            model_event_idle_seconds=_int("VFXH_MODEL_EVENT_IDLE_SECONDS", 360),
            run_max_dispatches=_int("VFXH_RUN_MAX_DISPATCHES", 6),
            run_max_replans_per_layer=_int("VFXH_RUN_MAX_REPLANS_PER_LAYER", 2),
            run_max_usd=_optional_float("VFXH_RUN_MAX_USD"),
        )
