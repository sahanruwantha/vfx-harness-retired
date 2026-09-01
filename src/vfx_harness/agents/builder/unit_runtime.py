"""Small attempt-bound setup and canonical script publication helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.agents.builder.attempt_guard import UnitAttemptGuard
from vfx_harness.agents.builder.authority import AuthorityBoundLedger
from vfx_harness.agents.builder.candidate_script import (
    require_exact_candidate_script_path,
)
from vfx_harness.agents.builder.models import _RESET
from vfx_harness.agents.builder.pkg import builder_package
from vfx_harness.agents.builder.prior import _run_artifact_script
from vfx_harness.agents.builder.revalidate import _retry_warm_start
from vfx_harness.blender import session as blender_session
from vfx_harness.blender.session import BlenderError
from vfx_harness.domain.brief import Shot
from vfx_harness.domain.work_units import canonical_unit_script_path
from vfx_harness.observability.log import log
from vfx_harness.orchestration import generate_construction
from vfx_harness.orchestration.authority_selection import ResolvedSelectedAuthority
from vfx_harness.orchestration.authority_selection_transaction import (
    durably_ensure_real_directory,
)
from vfx_harness.orchestration.ledger import Milestone
from vfx_harness.orchestration.unit_state import unit_digest


@dataclass(frozen=True)
class UnitRuntimeStart:
    ledger: AuthorityBoundLedger
    previous_status: str
    retry_script: Path
    unit_hash: str
    warm_start_candidate: bool
    started_at: float


def start_unit_runtime(
    shot: Shot,
    milestone: Milestone,
    script_rel: str,
    active_unit,
    selected_authority: ResolvedSelectedAuthority,
    attempt_guard: UnitAttemptGuard,
) -> UnitRuntimeStart:
    """Initialize the attempt-bound ledger and exact-generation warm-start decision."""

    attempt_guard.require_unit_boundary(
        milestone,
        active_unit,
        script_rel=script_rel,
    )
    ledger = AuthorityBoundLedger(
        shot,
        selected_authority,
        execution_guard=attempt_guard,
    )
    previous_slot = dict(ledger._slot(milestone))
    previous_status = str(previous_slot.get("status") or "")
    retry_script = shot.folder / script_rel
    current_unit_hash = unit_digest(active_unit)
    warm_start_candidate = _retry_warm_start(
        previous_status,
        retry_script,
        previous_artifact_unit_hash=str(
            previous_slot.get("artifact_unit_hash") or ""
        ),
        current_unit_hash=current_unit_hash,
    )
    ledger._slot(milestone)["script"] = script_rel
    ledger._slot(milestone)["unit_hash"] = current_unit_hash
    ledger.begin(milestone)
    return UnitRuntimeStart(
        ledger=ledger,
        previous_status=previous_status,
        retry_script=retry_script,
        unit_hash=current_unit_hash,
        warm_start_candidate=warm_start_candidate,
        started_at=time.monotonic(),
    )


def publish_candidate_script(
    shot_folder: str | Path,
    candidate_path: str | Path,
    script_rel: str,
    attempt_guard: UnitAttemptGuard,
) -> str:
    """Stage candidate bytes unlocked, then bind them under the exact attempt."""

    root = Path(shot_folder).resolve()
    candidate = require_exact_candidate_script_path(
        root,
        candidate_path,
        attempt_guard,
    )
    relative = Path(script_rel)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != str(script_rel)
    ):
        raise ValueError(
            f"canonical unit script must be a canonical shot-relative path: {script_rel!r}"
        )
    expected_script = canonical_unit_script_path(
        attempt_guard.layer_id,
        attempt_guard.unit.id,
    )
    if relative.as_posix() != expected_script:
        raise ValueError(
            "canonical unit script does not match the exact attempt identity: "
            f"expected {expected_script}, found {relative.as_posix()}"
        )
    destination = root / relative
    attempt_guard.check("start canonical unit script publication staging")
    durably_ensure_real_directory(root, relative.parent)
    prepared = blender_session.prepare_durable_parent_publish(
        candidate,
        destination,
        source_root=candidate.parent,
    )
    try:
        return attempt_guard.publish(
            "publish canonical unit replay script",
            lambda: blender_session.commit_durable_parent_publish(prepared),
        )
    finally:
        blender_session.discard_prepared_parent_publish(prepared)


def apply_retry_warm_start(
    shot: Shot,
    session,
    prior_paths: list[Path],
    priors,
    promoted,
    *,
    enabled: bool,
    retry_script: Path,
    previous_status: str,
    script_rel: str,
):
    """Replay an exact-generation failed artifact or restore clean priors loudly."""

    if not enabled:
        return False, priors
    try:
        _run_artifact_script(session, retry_script)
    except BlenderError as exc:
        log(
            f"retry warm start rejected ({str(exc)[:120]}); restoring clean prior layers",
            1,
        )
        session.run(_RESET)
        session.run(builder_package()._preamble(shot))
        restored = builder_package()._run_prior_paths(session, prior_paths)
        generate_construction.pin_construction_import(session, promoted)
        return False, restored
    log(
        f"retry warm start: replayed prior {previous_status} artifact {script_rel}; "
        "builder will repair this scene instead of rebuilding it",
        1,
    )
    return True, priors
