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

ENV_FILE_VARIABLE = "BVFX_ENV_FILE"
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_EXECUTION_MODEL = "claude-sonnet-5"
DEFAULT_CRITIC_MODEL = "claude-opus-5"


def environment_file(path: str | Path | None = None) -> Path | None:
    """Resolve the dotenv file without searching arbitrary parent directories.

    Resolution is intentionally predictable: an explicit argument, then
    ``BVFX_ENV_FILE``, then the checkout-root ``.env``.  Installed packages have no
    implicit dotenv file; callers can opt in with ``BVFX_ENV_FILE``.
    """
    configured = path or os.environ.get(ENV_FILE_VARIABLE)
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"{ENV_FILE_VARIABLE} points to no file: {candidate}")
        return candidate

    candidate = PROJECT_ROOT / ".env"
    return candidate if candidate.is_file() else None


def load_environment(path: str | Path | None = None) -> Path | None:
    """Load project configuration once, preserving real environment variables."""
    candidate = environment_file(path)
    if candidate is not None:
        load_dotenv(candidate, override=False)
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

    @classmethod
    def from_environment(cls, *, load_dotenv_file: bool = True) -> Settings:
        if load_dotenv_file:
            load_environment()
        execution_model = _text("BVFX_EXECUTION_MODEL", DEFAULT_EXECUTION_MODEL)
        return cls(
            blender_bin=os.environ.get("BLENDER_BIN", "blender"),
            asset_image_backend=os.environ.get("ASSET_IMAGE_BACKEND", "codex").lower(),
            higgsfield_bin=os.environ.get("HIGGSFIELD_BIN") or None,
            transcript_enabled=not _flag("BVFX_NO_TRANSCRIPT"),
            distill_inline=_flag("BVFX_DISTILL_INLINE"),
            execution_model=execution_model,
            planner_model=_text("BVFX_PLANNER_MODEL", execution_model),
            builder_model=_text("BVFX_BUILDER_MODEL", execution_model),
            script_model=_text("BVFX_SCRIPT_MODEL", execution_model),
            reviewer_model=_text("BVFX_REVIEWER_MODEL", execution_model),
            asset_model=_text("BVFX_ASSET_MODEL", execution_model),
            distiller_model=_text("BVFX_DISTILLER_MODEL", execution_model),
            critic_model=_text("BVFX_CRITIC_MODEL", DEFAULT_CRITIC_MODEL),
        )
