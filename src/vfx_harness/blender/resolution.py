"""Select a Blender executable by proving it inside the mandatory confinement.

A desktop launcher can answer ``--version`` on the host and still be unusable under
the worker's bubblewrap confinement: a snap shim needs snapd and capabilities the
sandbox withholds and only fails, slowly, at worker boot.  Resolution therefore runs
the probe through the worker's own prepared command and keeps every rejected
candidate's confined diagnostic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from vfx_harness.blender.filesystem_confinement import (
    SYSTEM_RUNTIME_ROOTS,
    BlenderError,
    prepared_worker_command,
)
from vfx_harness.observability import run_artifacts

# Fallback locations tried after the requested name, its PATH resolution, and BLENDER_BIN.
_FALLBACK_CANDIDATES = ("/snap/blender/current/blender", "/usr/bin/blender")
_PROBE_RUN_ID = "blender-probe"
_PROBE_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True, slots=True)
class BlenderCandidateRejection:
    """Why one Blender candidate was not selected, in the confinement's own words."""

    candidate: str
    reason: str


class BlenderResolutionError(BlenderError):
    """No candidate printed its Blender identity inside the mandatory confinement."""

    def __init__(
        self,
        requested: str,
        rejections: tuple[BlenderCandidateRejection, ...],
        *,
        shot_bound: bool,
    ) -> None:
        self.requested = requested
        self.rejections = rejections
        self.shot_bound = shot_bound
        if shot_bound:
            view = (
                "the shot-bound view: "
                + ", ".join(str(root) for root in SYSTEM_RUNTIME_ROOTS)
                + " plus the harness runtime"
            )
        else:
            view = "the read-only host root view"
        tried = "; ".join(f"{item.candidate}: {item.reason}" for item in rejections)
        super().__init__(
            "no Blender candidate executes inside the mandatory filesystem confinement "
            f"({view}); requested {requested!r}; rejected: {tried or 'no candidate'}. "
            "Next action: set BLENDER_BIN to a Blender executable that runs under those "
            "roots; desktop launchers that need snapd or extra capabilities cannot run "
            "there, so a packaged install must name its real binary."
        )


def _confined_version_probe(path: str, *, shot_bound: bool) -> str | None:
    """Run ``--version`` through the worker's own confinement; None means accepted.

    The probe uses :func:`prepared_worker_command` with a private throwaway layout, so
    it proves executability under exactly the roots, mounts, and environment a worker
    receives.  A rejection carries the confinement's own diagnostic.
    """

    with tempfile.TemporaryDirectory(prefix=".vfxh-blender-probe-") as temporary:
        root = Path(temporary)
        if shot_bound:
            layout = run_artifacts.RunLayout(
                shot=root,
                run_id=_PROBE_RUN_ID,
                root=root / "runs" / _PROBE_RUN_ID,
            )
            writable = layout.scratch / "blender"
            authority: Path | None = root
            run_root: Path | None = layout.root
        else:
            writable = root / "blender"
            authority = None
            run_root = None
        writable.mkdir(parents=True)
        try:
            with prepared_worker_command(
                [path, "--version"],
                writable_roots=(writable,),
                authority_root=authority,
                current_run_root=run_root,
            ) as command:
                probe = subprocess.run(
                    command.argv,
                    capture_output=True,
                    text=True,
                    timeout=_PROBE_TIMEOUT_SECONDS,
                    check=False,
                    pass_fds=command.pass_fds,
                )
        except subprocess.TimeoutExpired:
            return (
                f"did not answer --version within {int(_PROBE_TIMEOUT_SECONDS)}s "
                "inside the confinement"
            )
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"
    if probe.returncode == 0 and "Blender" in probe.stdout + probe.stderr:
        return None
    reason = (probe.stderr or probe.stdout or f"exit {probe.returncode}").strip()
    return reason[-240:]


@lru_cache(maxsize=16)
def resolve_blender(requested: str, *, shot_bound: bool = True) -> str:
    """Select the first candidate that prints its Blender identity inside the confinement.

    A desktop launcher can succeed on the host and still be unusable under the mandatory
    filesystem confinement: a snap shim needs snapd and capabilities the sandbox
    withholds, so it times out at worker boot with a diagnostic about snap profiles.
    Selection is therefore decided only by the confined probe, and every rejected
    candidate keeps the confinement's own reason.  ``shot_bound`` selects the strict
    shot-bound view a run uses; an ephemeral session may resolve against the read-only
    host root view instead.
    """

    candidates: list[str] = []
    for value in (
        requested,
        shutil.which(requested),
        os.environ.get("BLENDER_BIN"),
        *_FALLBACK_CANDIDATES,
    ):
        if value and value not in candidates:
            candidates.append(str(value))
    rejections: list[BlenderCandidateRejection] = []
    for candidate in candidates:
        path = shutil.which(candidate) or candidate
        if not Path(path).is_file():
            rejections.append(BlenderCandidateRejection(candidate, "not found"))
            continue
        reason = _confined_version_probe(path, shot_bound=shot_bound)
        if reason is None:
            # Do not resolve symlinks: multi-call launchers select the application
            # from argv[0], and the confined probe already proved this exact path.
            return str(path)
        rejections.append(BlenderCandidateRejection(str(path), reason))
    raise BlenderResolutionError(requested, tuple(rejections), shot_bound=shot_bound)


__all__ = [
    "BlenderCandidateRejection",
    "BlenderResolutionError",
    "resolve_blender",
]
