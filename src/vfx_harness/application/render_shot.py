"""Stage 4 — render the built shot to mp4.

Runs the selected layer delta scripts in the global DAG's stable topological order in
a warm session, renders the frame range in EEVEE, and encodes to mp4 with ffmpeg. Run as
a module so the `vfx_harness` package imports resolve:

    python -m vfx_harness.application.render_shot <shot-folder> [--upto 40] [--scale 1.0]
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.agents.acceptance_stop import require_current_accepted_outcome
from vfx_harness.agents.builder import _RESET, _preamble, _run_artifact_script
from vfx_harness.agents.builder.prior import PreparedArtifactReplayInput
from vfx_harness.application.final_render_snapshot import (
    FinalRenderSnapshot,
    FinalRenderSnapshotError,
    capture_final_render_snapshot,
    final_render_state_locks,
    require_snapshot_inputs_current,
)
from vfx_harness.blender.session import BlenderSession
from vfx_harness.domain.brief import Shot, load_shot
from vfx_harness.observability import run_artifacts
from vfx_harness.observability.log import log
from vfx_harness.orchestration import authority_selection, layer_publication
from vfx_harness.orchestration.authority_selection_heads import (
    AuthoritySelectionHeadError,
    read_authority_selection_heads,
)
from vfx_harness.orchestration.authority_selection_transaction import (
    AuthoritySelectionConflict,
    authority_selection_lock,
    require_matching_authority_selection_token,
)
from vfx_harness.orchestration.ledger import Ledger
from vfx_harness.orchestration.selected_layer_chain import selected_layer_chain
from vfx_harness.orchestration.unit_evaluation_receipts import ExecutedReplayInput


class IncompleteRender(RuntimeError):
    """The deliverable was asked for before the chain that produces it is accepted."""


class FinalRenderPublicationError(RuntimeError):
    """The tentative final-media publication could not commit or roll back exactly."""


@dataclass(frozen=True, slots=True)
class _MediaRevision:
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _OwnedMediaRevision:
    device: int
    inode: int
    content: _MediaRevision


def _prepared_snapshot_replay_input(
    snapshot: FinalRenderSnapshot,
    index: int,
) -> PreparedArtifactReplayInput:
    """Return the exact captured bytes and leaf lineage for one replay member."""

    row = snapshot.replay[index]
    trusted = row.snapshot.trusted
    digest = row.snapshot.sha256
    if trusted is None or digest is None:
        raise FinalRenderSnapshotError(
            f"immutable replay script {index} has no trusted member binding"
        )
    return PreparedArtifactReplayInput(
        executed=ExecutedReplayInput(
            script_path=row.snapshot.path.relative_to(snapshot.replay_root).as_posix(),
            script_sha256=digest,
            source_binding=trusted,
        ),
        source_path=row.snapshot.path,
        payload=row.payload,
    )


def _render_output_path(
    shot: Shot,
    *,
    upto: str | None,
    force: bool,
    out: str | Path | None,
) -> Path:
    if out is not None:
        return Path(out)
    if force or upto is not None:
        label = (
            "forced"
            if force and upto is None
            else "upto-" + re.sub(r"[^A-Za-z0-9._-]+", "-", str(upto)).strip("-")
        )
        return (
            run_artifacts.ensure(shot.folder).scratch
            / "previews"
            / f"{shot.id}_{label}.mp4"
        )
    return run_artifacts.deliverables_dir(shot.folder) / f"{shot.id}_full.mp4"


def _chain_scripts(
    shot: Shot,
    upto: str | None = None,
    *,
    force: bool = False,
    expected_bundle_digest: str | None = None,
    selected_authority: authority_selection.ResolvedSelectedAuthority | None = None,
) -> list[Path]:
    """The layer delta scripts to run, in order, taken from the LEDGER's manifest.

    This used to glob build/ and run whatever it found. A glob answers "what files are
    here", but the deliverable is defined by "what did the pipeline accept" — so a stray
    experiment or a half-written 09_*.py silently entered the mp4, and a real layer that
    was misnamed silently did not. The ledger is the record; the directory is a cache.
    """

    build_dir = shot.folder / "build"
    if not build_dir.is_dir():
        raise FileNotFoundError(f"no build/ in {shot.folder} — run the build stage first")

    current_selected = selected_authority
    if current_selected is None and not force:
        try:
            current_selected = authority_selection.resolve_selected_authority(
                shot.folder
            )
        except authority_selection.SelectedAuthorityResolutionError as exc:
            raise IncompleteRender(str(exc)) from exc
    layers = list(
        selected_layer_chain(
            shot,
            selected_authority=current_selected,
            expected_bundle_digest=expected_bundle_digest,
        )
    )
    if upto:
        matching = [
            index
            for index, layer in enumerate(layers)
            if Path(layer.script).name.startswith(upto)
            or Path(layer.script).stem == upto
            or str(layer.id) == upto
        ]
        if not matching:
            raise FileNotFoundError(f"no layer matching {upto!r} in the plan")
        layers = layers[: matching[-1] + 1]

    ledger = Ledger(shot, selected_authority=current_selected)
    scripts, problems = [], []
    for g in layers:
        p = shot.folder / g.script
        if not p.is_file():
            problems.append(f"layer {g.id}: {g.script} missing")
            continue
        st = ledger.status(g.as_milestone())
        if st != "passed":
            problems.append(f"layer {g.id} ({g.script}) is '{st}', not passed")
        if not force:
            assert current_selected is not None
            try:
                layer_publication.require_current_layer_publication(
                    shot.folder,
                    g,
                    current_selected,
                )
            except layer_publication.LayerPublicationConflict as exc:
                problems.append(
                    f"layer {g.id} receipt-backed publication is invalid: {exc}"
                )
        scripts.append(p)
    if problems and not force:
        raise IncompleteRender(
            "refusing to render an unaccepted chain — " + "; ".join(problems)
            + ". Finish those layers, or pass --force for a preview render.")
    if problems:
        log(f"! --force: rendering an unaccepted chain — {'; '.join(problems)}")
    if not scripts:
        raise FileNotFoundError(f"no layer scripts in {build_dir} — run the build stage first")

    # Say what is being LEFT OUT. Silent truncation reads as "we rendered everything".
    named = {p.name for p in scripts}
    strays = sorted(p.name for p in build_dir.glob("[0-9]*.py") if p.name not in named)
    if strays:
        log(f"! ignoring {len(strays)} script(s) in build/ that the plan does not list: "
            + ", ".join(strays))
    return scripts


def _staged_render_path(output: Path) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}.pending-",
        suffix=output.suffix or ".mp4",
        dir=output.parent,
    )
    os.close(descriptor)
    return Path(temporary_name)


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _digest_descriptor(descriptor: int) -> _MediaRevision:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return _MediaRevision(size=size, sha256=digest.hexdigest())


def _read_owned_media(
    path: Path,
    *,
    where: str,
    sync: bool = False,
) -> _OwnedMediaRevision:
    flags = (
        (os.O_RDWR if sync else os.O_RDONLY)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FinalRenderPublicationError(
            f"{where} must be a readable real regular file: {path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FinalRenderPublicationError(
                f"{where} must be a regular file: {path}"
            )
        if sync:
            os.fsync(descriptor)
        content = _digest_descriptor(descriptor)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or content.size != after.st_size:
            raise FinalRenderPublicationError(f"{where} changed while it was read")
        return _OwnedMediaRevision(
            device=after.st_dev,
            inode=after.st_ino,
            content=content,
        )
    finally:
        os.close(descriptor)


def _copy_output_predecessor(
    output: Path,
) -> tuple[Path | None, _MediaRevision | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source = os.open(output, flags)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise FinalRenderPublicationError(
            f"existing final render must be a readable real regular file: {output}"
        ) from exc

    backup_descriptor: int | None = None
    backup: Path | None = None
    try:
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode):
            raise FinalRenderPublicationError(
                f"existing final render must be a regular file: {output}"
            )
        backup_descriptor, backup_name = tempfile.mkstemp(
            prefix=f".{output.name}.predecessor-",
            suffix=".mp4",
            dir=output.parent,
        )
        backup = Path(backup_name)
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(source, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(backup_descriptor, view)
                view = view[written:]
        os.fsync(backup_descriptor)
        after = os.fstat(source)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or size != after.st_size:
            raise FinalRenderPublicationError(
                "existing final render changed while its exact predecessor was preserved"
            )
        revision = _MediaRevision(size=size, sha256=digest.hexdigest())
        os.close(backup_descriptor)
        backup_descriptor = None
        _fsync_parent(backup)
        return backup, revision
    except BaseException:
        if backup_descriptor is not None:
            os.close(backup_descriptor)
        if backup is not None:
            with suppress(FileNotFoundError):
                backup.unlink()
        raise
    finally:
        os.close(source)


def _require_owned_output(output: Path, expected: _OwnedMediaRevision) -> None:
    observed = _read_owned_media(output, where="tentative final render")
    if observed != expected:
        raise FinalRenderPublicationError(
            "final-render rollback refused because another writer replaced or changed "
            "the tentative output"
        )


def _restore_output_predecessor(
    output: Path,
    tentative: _OwnedMediaRevision,
    backup: Path | None,
    predecessor: _MediaRevision | None,
) -> None:
    _require_owned_output(output, tentative)
    if backup is None:
        if predecessor is not None:
            raise FinalRenderPublicationError(
                "final-render predecessor state is internally inconsistent"
            )
        output.unlink()
        _fsync_parent(output)
        if output.exists() or output.is_symlink():
            raise FinalRenderPublicationError(
                "final-render rollback did not restore output absence"
            )
        return
    if predecessor is None:
        raise FinalRenderPublicationError(
            "final-render predecessor state is internally inconsistent"
        )
    backup_revision = _read_owned_media(
        backup,
        where="preserved final-render predecessor",
    )
    if backup_revision.content != predecessor:
        raise FinalRenderPublicationError(
            "preserved final-render predecessor changed before rollback"
        )
    os.replace(backup, output)
    _fsync_parent(output)
    restored = _read_owned_media(output, where="restored final render")
    if restored.content != predecessor:
        raise FinalRenderPublicationError(
            "final-render rollback did not restore the exact predecessor bytes"
        )


def _publish_media_transaction(
    staged: Path,
    output: Path,
    *,
    postcondition: Callable[[], None],
) -> None:
    tentative = _read_owned_media(
        staged,
        where="completed staged final render",
        sync=True,
    )
    backup, predecessor = _copy_output_predecessor(output)
    published = False
    retain_backup = False
    try:
        os.replace(staged, output)
        published = True
        _fsync_parent(output)
        postcondition()
    except BaseException:
        if published:
            try:
                _restore_output_predecessor(
                    output,
                    tentative,
                    backup,
                    predecessor,
                )
                backup = None
            except BaseException as rollback_error:
                retain_backup = backup is not None and backup.exists()
                raise FinalRenderPublicationError(
                    "final-render postcondition failed and exact predecessor rollback failed; "
                    f"preserved predecessor={backup if retain_backup else None}"
                ) from rollback_error
        raise
    finally:
        if backup is not None and not retain_backup:
            with suppress(FileNotFoundError):
                backup.unlink()
    _fsync_parent(output)


@contextmanager
def _final_render_output_lock(output: Path) -> Iterator[None]:
    lock = output.with_name(f".{output.name}.publish.lock")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise FinalRenderPublicationError(
            f"final-render publication lock must be a real regular file: {lock}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise FinalRenderPublicationError(
                f"final-render publication lock must be a regular file: {lock}"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            os.fsync(descriptor)
            _fsync_parent(lock)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _require_final_render_authority_current(
    shot: Shot,
    snapshot: FinalRenderSnapshot,
    *,
    selected_authority: authority_selection.ResolvedSelectedAuthority | None = None,
) -> None:
    current_selected = selected_authority
    if current_selected is None:
        current_selected = authority_selection.resolve_selected_authority(shot.folder)
    if (
        current_selected.selection_token
        != snapshot.selected_authority.selection_token
        or current_selected.assertion != snapshot.selected_authority.assertion
    ):
        raise FinalRenderSnapshotError("selected authority changed during final render")
    current_outcome = require_current_accepted_outcome(shot, current_selected)
    if current_outcome.digest != snapshot.outcome_digest:
        raise FinalRenderSnapshotError("accepted outcome changed during final render")
    require_snapshot_inputs_current(shot, snapshot)


def _require_final_render_snapshot_current(
    shot: Shot,
    snapshot: FinalRenderSnapshot,
    current_selected: authority_selection.ResolvedSelectedAuthority,
) -> None:
    """Verify captured inputs under caller-held selection/state locks.

    The full accepted-outcome check re-enters the serialized selection lock through
    every layer publication guard.  Publication performs that full check immediately
    before acquiring the transaction locks, then uses this byte/state postcondition
    while the locks are held through tentative rename and rollback.
    """

    if (
        current_selected.selection_token
        != snapshot.selected_authority.selection_token
        or current_selected.assertion != snapshot.selected_authority.assertion
    ):
        raise FinalRenderSnapshotError("selected authority changed during final render")
    require_snapshot_inputs_current(shot, snapshot)


def _publish_final_render(
    shot: Shot,
    staged: Path,
    output: Path,
    snapshot: FinalRenderSnapshot,
) -> None:
    """Tentatively publish, postverify, and restore the predecessor on conflict."""

    try:
        # This full verifier replays the receipt/v3-outcome/ledger read boundary for
        # every selected layer after the expensive render, before any deliverable byte
        # can replace its predecessor.
        _require_final_render_authority_current(shot, snapshot)
        with _final_render_output_lock(output), authority_selection_lock(
            shot.folder,
            exclusive=False,
        ):
            observed = read_authority_selection_heads(shot.folder)
            require_matching_authority_selection_token(
                snapshot.selected_authority.selection_token,
                observed.token,
            )
            # The selection lock is already held.  Re-entering
            # ``resolve_selected_authority`` would open the same inode through a new
            # descriptor and deadlock on its serialized flock.  Resolve the exact
            # heads observed inside this transaction instead.
            current_selected = authority_selection.resolve_selected_authority_from_heads(
                shot.folder,
                observed,
            )
            with final_render_state_locks(shot, snapshot):
                _require_final_render_snapshot_current(
                    shot,
                    snapshot,
                    current_selected,
                )
                _publish_media_transaction(
                    staged,
                    output,
                    postcondition=lambda: _require_final_render_snapshot_current(
                        shot,
                        snapshot,
                        current_selected,
                    ),
                )
    except (
        AuthoritySelectionConflict,
        AuthoritySelectionHeadError,
        FinalRenderSnapshotError,
        FinalRenderPublicationError,
        authority_selection.SelectedAuthorityResolutionError,
        OSError,
        ValueError,
    ) as exc:
        raise IncompleteRender(
            "accepted authority or replay inputs changed during final render; "
            "refusing publication"
        ) from exc


def render_mp4(shot: Shot, upto: str | None = None, *, scale: float = 1.0,
               blender: str = "blender", out: str | Path | None = None,
               force: bool = False) -> Path:
    acceptance_outcome = None
    selected_authority = None
    if upto is None and not force:
        try:
            selected_authority = authority_selection.resolve_selected_authority(
                shot.folder
            )
            acceptance_outcome = require_current_accepted_outcome(
                shot,
                selected_authority,
            )
        except (authority_selection.SelectedAuthorityResolutionError, ValueError) as exc:
            raise IncompleteRender(str(exc)) from exc
    elif force:
        log("! --force: final acceptance is not being used as publication authority")
    else:
        log("! --upto: partial-chain output is a preview, not a deliverable")
    scripts = _chain_scripts(
        shot,
        upto,
        force=force,
        expected_bundle_digest=(
            acceptance_outcome.bundle_digest
            if acceptance_outcome is not None
            else None
        ),
        selected_authority=selected_authority,
    )
    out = _render_output_path(shot, upto=upto, force=force, out=out)
    out.parent.mkdir(parents=True, exist_ok=True)
    staged_out = _staged_render_path(out) if selected_authority is not None else out
    render_snapshot = None
    if selected_authority is not None:
        assert acceptance_outcome is not None
        try:
            render_snapshot = capture_final_render_snapshot(
                shot,
                selected_authority,
                acceptance_outcome,
                scripts,
                scratch=run_artifacts.ensure(shot.folder, command="render").scratch,
            )
        except (FinalRenderSnapshotError, ValueError) as exc:
            staged_out.unlink(missing_ok=True)
            raise IncompleteRender(str(exc)) from exc
        scripts = list(render_snapshot.replay_scripts)

    try:
        s = BlenderSession(
            blender=blender,
            blend_file=None,
            assets_dir=(
                render_snapshot.assets_dir
                if render_snapshot is not None
                else shot.folder / "assets"
            ),
            cwd=shot.folder,
            readable_roots=(
                (render_snapshot.replay_root,)
                if render_snapshot is not None
                else ()
            ),
            readable_root_bindings=(
                (render_snapshot.replay_root_binding,)
                if render_snapshot is not None
                else ()
            ),
            readable_file_bindings=(
                render_snapshot.worker_file_bindings
                if render_snapshot is not None
                else ()
            ),
        ).start()
        try:
            s.run(_RESET)
            s.run(_preamble(shot))
            if render_snapshot is not None:
                s.run(
                    "import os\n"
                    f"os.chdir({str(render_snapshot.replay_root)!r})\n"
                )
            for index, p in enumerate(scripts):
                log(f"running {p.name}")
                _run_artifact_script(
                    s,
                    p,
                    (
                        _prepared_snapshot_replay_input(render_snapshot, index)
                        if render_snapshot is not None
                        else None
                    ),
                )
            log(f"rendering {shot.frames} frames @ scale {scale}…")
            t0 = time.monotonic()
            for f in range(1, shot.frames + 1):
                s.render(frame=f, mode="eevee", scale=scale)
                if f % 8 == 0:
                    log(f"  {f}/{shot.frames} ({time.monotonic() - t0:.0f}s)")
            seq = str(s.artifacts / "eevee_f%04d.png")
            subprocess.run(
                ["ffmpeg", "-y", "-framerate", str(shot.fps), "-i", seq,
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                 str(staged_out)],
                check=True, capture_output=True, text=True,
            )
        finally:
            s.close()
        if render_snapshot is not None:
            _publish_final_render(shot, staged_out, out, render_snapshot)
    except BaseException:
        if staged_out != out:
            staged_out.unlink(missing_ok=True)
        raise
    log(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Render the shot to mp4 — default runs ALL layer scripts in order.")
    ap.add_argument("folder", help="shot folder (contains build/)")
    ap.add_argument("--upto", default=None,
                    help="stop after this layer script (e.g. 40 or 40_seam.py)")
    ap.add_argument("--scale", type=float, default=1.0, help="0..1 render resolution")
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--out", help="output mp4 path (default renders/<shot>_full.mp4)")
    ap.add_argument("--force", action="store_true",
                    help="render even if layers are missing or unaccepted (preview only)")
    args = ap.parse_args()
    shot = load_shot(args.folder)
    with run_artifacts.invocation(shot.folder, "render", shot_id=shot.id,
                                  parameters={"upto": args.upto, "scale": args.scale}):
        try:
            render_mp4(shot, args.upto, scale=args.scale, blender=args.blender,
                       out=args.out, force=args.force)
        except IncompleteRender as e:
            log(f"INCOMPLETE CHAIN — {e}")
            raise run_artifacts.RequestedExit(7, f"INCOMPLETE CHAIN — {e}") from None


if __name__ == "__main__":
    main()
