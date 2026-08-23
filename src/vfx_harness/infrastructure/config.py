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
    planner_model: str = DEFAULT_EXECUTION_MODEL
    builder_model: str = DEFAULT_EXECUTION_MODEL
    script_model: str = DEFAULT_EXECUTION_MODEL
    reviewer_model: str = DEFAULT_EXECUTION_MODEL
    asset_model: str = DEFAULT_EXECUTION_MODEL
    distiller_model: str = DEFAULT_EXECUTION_MODEL
    critic_model: str = DEFAULT_CRITIC_MODEL
    # Hard ceiling for one global plan session (draft, verify, or repair). A runaway
    # pass ends at this boundary instead of grinding against unresolvable findings;
    # `vfx plan --max-turns` still overrides per invocation.
    plan_max_turns: int = 24
    plan_verify_max_turns: int = 12

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
            planner_model=_text("VFXH_PLANNER_MODEL", execution_model),
            builder_model=_text("VFXH_BUILDER_MODEL", execution_model),
            script_model=_text("VFXH_SCRIPT_MODEL", execution_model),
            reviewer_model=_text("VFXH_REVIEWER_MODEL", execution_model),
            asset_model=_text("VFXH_ASSET_MODEL", execution_model),
            distiller_model=_text("VFXH_DISTILLER_MODEL", execution_model),
            critic_model=_text("VFXH_CRITIC_MODEL", DEFAULT_CRITIC_MODEL),
            plan_max_turns=_int("VFXH_PLAN_MAX_TURNS", 24),
            plan_verify_max_turns=_int("VFXH_PLAN_VERIFY_MAX_TURNS", 12),
        )
