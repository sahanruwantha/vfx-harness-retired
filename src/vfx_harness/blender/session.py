"""Client for the warm Blender worker — launch once, call many times."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import queue
import secrets
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.blender.filesystem_confinement import (
    BlenderError,
    open_real_directory,
    prepared_worker_command,
)
from vfx_harness.blender.observation_environment import validate_observation_request
from vfx_harness.blender.resolution import (
    resolve_blender,
)
from vfx_harness.infrastructure.trusted_files import (
    PinnedTrustedFile,
    TrustedDirectoryBinding,
    TrustedFileBinding,
    TrustedFileError,
    open_pinned_trusted_file,
)
from vfx_harness.observability import run_artifacts

SENT = "@@VFXH@@"
_WORKER = Path(__file__).with_name("worker.py")


@dataclass(frozen=True, slots=True)
class PreparedParentPublication:
    """Parent-owned immutable temp ready for one metadata-only commit."""

    temporary: Path
    destination: Path
    sha256: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    descriptor: int
    directory_descriptor: int
    temporary_name: str
    destination_name: str
    directory_device: int
    directory_inode: int
    source: PinnedTrustedFile
    source_binding: TrustedFileBinding


def _publication_identity(observed: os.stat_result) -> tuple[int, ...]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _expected_publication_identity(
    prepared: PreparedParentPublication,
) -> tuple[int, ...]:
    return (
        prepared.device,
        prepared.inode,
        prepared.size,
        prepared.modified_ns,
        prepared.changed_ns,
    )


def _publication_entry_names_prepared_inode(
    prepared: PreparedParentPublication,
) -> bool:
    """Whether the live temp name still resolves to the descriptor-held inode."""

    try:
        observed = os.stat(
            prepared.temporary_name,
            dir_fd=prepared.directory_descriptor,
            follow_symlinks=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return stat.S_ISREG(observed.st_mode) and (observed.st_dev, observed.st_ino) == (
        prepared.device,
        prepared.inode,
    )


def _unlink_prepared_entry_if_owned(prepared: PreparedParentPublication) -> bool:
    """Remove only the directory entry that still names the prepared inode."""

    if not _publication_entry_names_prepared_inode(prepared):
        return False
    try:
        os.unlink(
            prepared.temporary_name,
            dir_fd=prepared.directory_descriptor,
        )
    except FileNotFoundError:
        return False
    return True


def _prepare_durable_parent_publish(
    source: Path,
    destination: Path,
    *,
    source_root: Path,
) -> PreparedParentPublication:
    """Copy/hash/fsync outside state locks into an unreferenced parent temp."""

    directory_descriptor = open_real_directory(
        destination.parent,
        "worker publication destination parent",
    )
    directory_stat = os.fstat(directory_descriptor)
    try:
        pinned_source = open_pinned_trusted_file(
            source_root,
            source,
            "worker publication source",
        )
    except TrustedFileError as exc:
        os.close(directory_descriptor)
        raise BlenderError(str(exc)) from exc
    temporary_name = f".{destination.name}.prepared.{secrets.token_hex(12)}"
    try:
        temporary_descriptor = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
    except BaseException:
        pinned_source.close()
        os.close(directory_descriptor)
        raise
    temporary = destination.parent / temporary_name
    try:
        expected_digest = pinned_source.copy_and_hash_to(temporary_descriptor)
        os.fsync(temporary_descriptor)
        os.lseek(temporary_descriptor, 0, os.SEEK_SET)
        observed_digest = hashlib.sha256()
        with os.fdopen(temporary_descriptor, "rb", closefd=False) as prepared_handle:
            while chunk := prepared_handle.read(1024 * 1024):
                observed_digest.update(chunk)
        if observed_digest.hexdigest() != expected_digest:
            raise BlenderError(f"worker publication prepared copy changed: {temporary}")
        read_descriptor = os.open(
            temporary_name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        observed = os.fstat(read_descriptor)
        return PreparedParentPublication(
            temporary=temporary,
            destination=destination,
            sha256=expected_digest,
            device=observed.st_dev,
            inode=observed.st_ino,
            size=observed.st_size,
            modified_ns=observed.st_mtime_ns,
            changed_ns=observed.st_ctime_ns,
            descriptor=read_descriptor,
            directory_descriptor=directory_descriptor,
            temporary_name=temporary_name,
            destination_name=destination.name,
            directory_device=directory_stat.st_dev,
            directory_inode=directory_stat.st_ino,
            source=pinned_source,
            source_binding=pinned_source.binding,
        )
    except TrustedFileError as exc:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        pinned_source.close()
        os.close(directory_descriptor)
        raise BlenderError(str(exc)) from exc
    except BaseException:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        pinned_source.close()
        os.close(directory_descriptor)
        raise


def _commit_durable_parent_publish(prepared: PreparedParentPublication) -> str:
    """Atomically expose a prepared inode; no content I/O occurs in this commit."""

    installed = False
    try:
        try:
            prepared.source.require_current()
        except TrustedFileError as exc:
            raise BlenderError(str(exc)) from exc
        try:
            observed = os.fstat(prepared.descriptor)
        except OSError as exc:
            raise BlenderError(
                f"prepared worker publication disappeared: {prepared.temporary}"
            ) from exc
        if (
            not stat.S_ISREG(observed.st_mode)
            or _publication_identity(observed) != _expected_publication_identity(prepared)
        ):
            raise BlenderError(
                f"prepared worker publication identity changed: {prepared.temporary}"
            )
        current_directory = open_real_directory(
            prepared.destination.parent,
            "worker publication destination parent",
        )
        try:
            current_stat = os.fstat(current_directory)
        finally:
            os.close(current_directory)
        if (current_stat.st_dev, current_stat.st_ino) != (
            prepared.directory_device,
            prepared.directory_inode,
        ):
            raise BlenderError(
                f"worker publication destination directory changed: {prepared.destination.parent}"
            )
        if not _publication_entry_names_prepared_inode(prepared):
            raise BlenderError(
                "prepared worker publication directory entry changed: "
                f"{prepared.temporary}"
            )
        os.replace(
            prepared.temporary_name,
            prepared.destination_name,
            src_dir_fd=prepared.directory_descriptor,
            dst_dir_fd=prepared.directory_descriptor,
        )
        installed = True
        current_directory = open_real_directory(
            prepared.destination.parent,
            "worker publication destination parent",
        )
        try:
            current_stat = os.fstat(current_directory)
        finally:
            os.close(current_directory)
        if (current_stat.st_dev, current_stat.st_ino) != (
            prepared.directory_device,
            prepared.directory_inode,
        ):
            os.unlink(
                prepared.destination_name,
                dir_fd=prepared.directory_descriptor,
            )
            os.fsync(prepared.directory_descriptor)
            raise BlenderError(
                "worker publication destination directory changed during commit: "
                f"{prepared.destination.parent}"
            )
        os.fsync(prepared.directory_descriptor)
    finally:
        if not installed:
            _unlink_prepared_entry_if_owned(prepared)
            with contextlib.suppress(OSError):
                os.fsync(prepared.directory_descriptor)
        prepared.source.close()
        os.close(prepared.descriptor)
        os.close(prepared.directory_descriptor)
    return prepared.sha256


def _discard_prepared_parent_publish(prepared: PreparedParentPublication) -> None:
    """Remove an uncommitted temp only while it retains the prepared identity."""

    try:
        try:
            observed = os.fstat(prepared.descriptor)
        except OSError:
            return
        if stat.S_ISREG(observed.st_mode) and (observed.st_dev, observed.st_ino) == (
            prepared.device,
            prepared.inode,
        ):
            _unlink_prepared_entry_if_owned(prepared)
    finally:
        prepared.source.close()
        with contextlib.suppress(OSError):
            os.close(prepared.descriptor)
        with contextlib.suppress(OSError):
            os.close(prepared.directory_descriptor)


def prepare_durable_parent_publish(
    source: Path,
    destination: Path,
    *,
    source_root: Path,
) -> PreparedParentPublication:
    """Public parent-owned staging boundary for a later metadata-only commit."""

    return _prepare_durable_parent_publish(
        source,
        destination,
        source_root=source_root,
    )


def commit_durable_parent_publish(prepared: PreparedParentPublication) -> str:
    """Public metadata-only commit for parent-owned prepared bytes."""

    return _commit_durable_parent_publish(prepared)


def discard_prepared_parent_publish(prepared: PreparedParentPublication) -> None:
    """Public cleanup for a prepared publication that did not commit."""

    _discard_prepared_parent_publish(prepared)


class BlenderSession:
    """A long-lived headless Blender process holding one scene in memory.

    Snap-Blender note: artifacts must live under $HOME (snap confinement can't see
    /tmp), so the default artifacts dir is created next to the repo, not in /tmp.
    """

    def __init__(
        self,
        blender: str = "blender",
        artifacts_dir: str | Path | None = None,
        blend_file: str | Path | None = None,
        boot_timeout: float = 60.0,
        assets_dir: str | Path | None = None,
        cwd: str | Path | None = None,
        readable_roots: tuple[str | Path, ...] = (),
        readable_root_bindings: tuple[TrustedDirectoryBinding, ...] = (),
        readable_file_bindings: tuple[TrustedFileBinding, ...] = (),
    ):
        self.blender = blender
        self.blend_file = str(blend_file) if blend_file else None
        self.boot_timeout = boot_timeout
        self.assets_dir = str(assets_dir) if assets_dir else None
        self.readable_roots = tuple(Path(path) for path in readable_roots)
        self.readable_root_bindings = readable_root_bindings
        self.readable_file_bindings = readable_file_bindings
        # the worker must run FROM the shot folder: agents are configured with
        # cwd=shot.folder, so relative paths inside run_bpy ("refs/…", "build/…") have
        # to resolve the same way — otherwise the builder reads the repo root and
        # concludes its own references are missing.
        self.cwd = str(cwd) if cwd else None
        # Renders are throwaway; SNAPSHOTS are not — resume needs to find them again.
        # These used to be mkdtemp(dir=$HOME) per session and were never cleaned: 107
        # dirs and 1.8 GB accumulated in one day, and the .blend checkpoints inside them
        # were unreachable, which is why a failed layer could only be rebuilt from zero.
        self._ephemeral = artifacts_dir is None and cwd is None
        if artifacts_dir is None:
            if cwd:
                layout = run_artifacts.ensure(cwd, command="blender-session")
                artifacts_dir = layout.scratch / "blender"
            else:
                artifacts_dir = Path(tempfile.mkdtemp(prefix=".vfxh-render-", dir=Path.home()))
        self.artifacts = Path(artifacts_dir)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        layout = run_artifacts.ensure(cwd, command="blender-session") if cwd else None
        self._run_root = layout.root if layout else None
        self.snapshots = layout.checkpoints / "blender" if layout else self.artifacts
        self.snapshots.mkdir(parents=True, exist_ok=True)
        # Journals are checkpoint evidence beside the .blend snapshots: the finalizer
        # distils the accepted prefix from them. The session mints their one destination
        # so publication containment and the finalizer can never name different roots
        # (run 20260902T165518Z-004470 refused every journal against the snapshot root).
        self.journals = (
            layout.checkpoints / "journals" if layout else self.artifacts / "journals"
        )
        self.journals.mkdir(parents=True, exist_ok=True)
        self._worker_publications = self.artifacts / "parent-publications"
        self._worker_publications.mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen | None = None
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._ids = itertools.count(1)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> BlenderSession:
        self.blender = resolve_blender(self.blender, shot_bound=self.cwd is not None)
        blender_argv = [self.blender, "--background", "--factory-startup"]
        if self.blend_file:
            blender_argv.append(self.blend_file)
        blender_argv += [
            "--python",
            str(_WORKER),
            "--",
            "--artifacts",
            str(self.artifacts),
        ]
        if self.assets_dir:
            blender_argv += ["--assets", self.assets_dir]
        authority = Path(self.cwd).expanduser().absolute() if self.cwd else None
        with prepared_worker_command(
            blender_argv,
            writable_roots=(self.artifacts,),
            readable_roots=self.readable_roots,
            readable_root_bindings=self.readable_root_bindings,
            readable_file_bindings=self.readable_file_bindings,
            authority_root=authority,
            current_run_root=self._run_root,
        ) as command:
            self.proc = subprocess.Popen(
                command.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                pass_fds=command.pass_fds,
            )
        self._stdout_queue = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            msg = self._read(timeout=max(0.0, deadline - time.monotonic()))
            if msg is None:
                break
            if msg.get("event") == "ready":
                return self
        stderr = self._stop_and_stderr()
        raise BlenderError("Blender worker did not become ready" + (f": {stderr}" if stderr else ""))

    def _stop_and_stderr(self) -> str:
        if not self.proc:
            return ""
        if self.proc.poll() is None:
            self.proc.kill()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=5)
        if not self.proc.stderr:
            return ""
        with contextlib.suppress(Exception):
            return self.proc.stderr.read()[-1200:].strip()
        return ""

    def _sweep(self, keep: int = 40) -> None:
        """Renders pile up fast; keep the newest and bin the rest. Snapshots are kept —
        they are the resume checkpoints."""
        try:
            pngs = sorted(self.artifacts.glob("*.png"), key=lambda p: p.stat().st_mtime)
            for p in pngs[:-keep]:
                p.unlink(missing_ok=True)
        except OSError as e:
            # Housekeeping, so never fatal — but a sweep that keeps failing means renders
            # accumulate unbounded, and this pipeline has already leaked 1.8 GB into $HOME
            # once by nobody noticing exactly this.
            print(f"! artifact sweep failed ({e}) — renders may accumulate in {self.artifacts}", flush=True)

    def close(self) -> None:
        self._sweep()
        if self.proc and self.proc.poll() is None:
            try:
                self._write({"id": next(self._ids), "cmd": "shutdown"})
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # -- io ------------------------------------------------------------------
    def _pump_stdout(self) -> None:
        """Drain stdout continuously so boot timeouts work with buffered text streams."""
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._stdout_queue.put(line)
        self._stdout_queue.put(None)

    def _write(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _read(self, timeout: float | None = None) -> dict | None:
        assert self.proc and self.proc.stdout
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                line = self._stdout_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            line = line.rstrip("\n")
            if line.startswith(SENT) and line.endswith(SENT) and len(line) > 2 * len(SENT):
                return json.loads(line[len(SENT) : -len(SENT)])

    def call(self, cmd: str, **args) -> dict:
        if not self.proc or self.proc.poll() is not None:
            raise BlenderError("Blender worker is not running")
        rid = next(self._ids)
        self._write({"id": rid, "cmd": cmd, "args": args})
        while True:
            msg = self._read()
            if msg is None:
                stderr = self._stop_and_stderr()
                raise BlenderError(f"worker died during {cmd!r}" + (f": {stderr}" if stderr else ""))
            if msg.get("id") != rid:
                continue  # skip stray events
            if not msg.get("ok"):
                raise BlenderError(msg.get("error", "unknown") + "\n" + msg.get("trace", ""))
            return msg.get("result", {})

    # -- typed convenience wrappers -----------------------------------------
    def ping(self) -> dict:
        return self.call("ping")

    def pin_construction_replay(self, payload: dict | None) -> dict:
        """Load one verified GLB snapshot into worker memory, or clear the pin."""

        return self.call("pin_construction_replay", payload=payload)

    def run(
        self,
        code: str,
        *,
        journal: bool = True,
        transactional: bool = False,
        execution_policy: str = "live",
    ) -> dict:
        """Execute Blender Python; read-only probes can opt out of the replay journal."""
        if execution_policy not in {"live", "artifact"}:
            raise ValueError(f"unknown Blender execution policy {execution_policy!r}")
        return self.call(
            "run",
            code=code,
            journal=journal,
            transactional=transactional,
            execution_policy=execution_policy,
        )

    def inspect(self, section: str = "all") -> str:
        return self.call("inspect", section=section)["text"]

    def canonical_observation_environment(
        self,
        *,
        frame: int,
        subject_roles: tuple[str, ...] | list[str],
        observation_medium: str,
        carrier_families: tuple[str, ...] | list[str],
    ) -> dict:
        """Read one canonical, hashable observation environment without rendering.

        The worker re-evaluates the requested frame and fails closed if there is no
        active camera or a requested semantic subject has no rendered host.
        """
        selected_frame, roles, medium, families = validate_observation_request(
            frame, subject_roles, observation_medium, carrier_families
        )
        return self.call(
            "observation_environment",
            frame=selected_frame,
            subject_roles=list(roles),
            observation_medium=medium,
            carrier_families=list(families),
        )

    def keyframes(self, obj: str) -> str:
        return self.call("keyframes", object=obj)["text"]

    def render(self, frame: int, mode: str = "eevee", scale: float = 0.5, **kw) -> str:
        return self.call("render", frame=frame, mode=mode, scale=scale, **kw)["image_path"]

    def render_full(self, frame: int, mode: str = "eevee", scale: float = 0.5, **kw) -> dict:
        """`render` returns only the path; the Phase-2 modes also return a caption and
        the settings they used, and a caption that never reaches the reader is the
        measured failure mode of every diagnostic render mode."""
        return self.call("render", frame=frame, mode=mode, scale=scale, **kw)

    def check(self, kind: str, **kw) -> dict:
        """Phase 1 — a judgment-free scene check. `kind='self_test'` runs the fixtures."""
        return self.call("check", kind=kind, **kw)

    def diff(self, a: str, b: str, dest: str | None = None) -> dict:
        """|A − B| for two PNGs already rendered.

        Client-side on purpose: Blender's bundled Python has no Pillow, and subtracting
        two files that are already on disk never needed a scene."""
        # tools imports BlenderSession; delay the reverse edge until the method runs.
        from .tools import subtract_png  # noqa: PLC0415

        return subtract_png(a, b, dest or str(self.artifacts / "diff.png"))

    def stage_snapshot(self, tag: str) -> dict:
        """Write a non-authoritative checkpoint candidate into worker scratch."""

        scratch = self._worker_publications / "snapshots"
        scratch.mkdir(parents=True, exist_ok=True)
        return self.call("snapshot", tag=tag, dir=str(scratch))

    def publish_snapshot(self, result: dict) -> dict:
        """Prepare and commit one snapshot for callers without attempt authority."""

        prepared = self.prepare_snapshot_publication(result)
        try:
            return self.commit_snapshot_publication(prepared)
        except BaseException:
            self.discard_snapshot_publication(prepared)
            raise

    def prepare_snapshot_publication(
        self,
        result: dict,
    ) -> tuple[dict, PreparedParentPublication]:
        """Copy a worker snapshot into an unreferenced parent-owned temp."""

        result = dict(result)
        scratch = self._worker_publications / "snapshots"
        source = Path(str(result["blend"]))
        try:
            source.resolve(strict=True).relative_to(scratch.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise BlenderError(
                f"worker snapshot escaped publication scratch: {source}"
            ) from exc
        destination = self.snapshots / source.name
        prepared = _prepare_durable_parent_publish(
            source,
            destination,
            source_root=scratch,
        )
        result["sha256"] = prepared.sha256
        result["blend"] = str(destination)
        return result, prepared

    def commit_snapshot_publication(
        self,
        prepared: tuple[dict, PreparedParentPublication],
    ) -> dict:
        """Expose a prepared snapshot with one metadata-only durable commit."""

        result, publication = prepared
        _commit_durable_parent_publish(publication)
        return dict(result)

    def discard_snapshot_publication(
        self,
        prepared: tuple[dict, PreparedParentPublication],
    ) -> None:
        """Discard an uncommitted parent snapshot temp."""

        _discard_prepared_parent_publish(prepared[1])

    def snapshot(self, tag: str) -> dict:
        """Stage then parent-publish a checkpoint for non-attempt callers."""

        return self.publish_snapshot(self.stage_snapshot(tag))

    def stage_journal(
        self,
        path: str | None = None,
        clear: bool = False,
        limit: int | None = None,
        start: int = 0,
    ) -> tuple[dict, Path | None]:
        """Dump the journal into worker scratch without checkpoint publication.

        `start` excludes inherited reset/prior replay; `limit` truncates to a snapshot's
        `journal_index`. The finalizer therefore sees only the active unit calls behind
        the restored checkpoint."""
        worker_path = None
        destination = None
        if path is not None:
            destination = Path(path).absolute()
            try:
                destination.relative_to(self.journals.absolute())
            except ValueError as exc:
                raise BlenderError(
                    f"journal publication must stay under {self.journals}, found "
                    f"{destination}; take the destination from journal_destination()"
                ) from exc
            scratch = self._worker_publications / "journals"
            scratch.mkdir(parents=True, exist_ok=True)
            worker_path = scratch / destination.name
        result = self.call(
            "journal",
            path=str(worker_path) if worker_path is not None else None,
            clear=clear,
            limit=limit,
            start=start,
        )
        return result, destination

    def publish_journal(
        self,
        staged: tuple[dict, Path | None],
    ) -> dict:
        """Prepare and commit one journal for callers without attempt authority."""

        prepared = self.prepare_journal_publication(staged)
        try:
            return self.commit_journal_publication(prepared)
        except BaseException:
            self.discard_journal_publication(prepared)
            raise

    def prepare_journal_publication(
        self,
        staged: tuple[dict, Path | None],
    ) -> tuple[dict, PreparedParentPublication | None]:
        """Copy a worker journal into an unreferenced parent-owned temp."""

        result, destination = staged
        result = dict(result)
        publication = None
        if destination is not None:
            worker_path = self._worker_publications / "journals" / destination.name
            publication = _prepare_durable_parent_publish(
                worker_path,
                destination,
                source_root=self._worker_publications / "journals",
            )
            result["sha256"] = publication.sha256
            result["path"] = str(destination)
        return result, publication

    def commit_journal_publication(
        self,
        prepared: tuple[dict, PreparedParentPublication | None],
    ) -> dict:
        """Expose a prepared journal with one metadata-only durable commit."""

        result, publication = prepared
        if publication is not None:
            _commit_durable_parent_publish(publication)
        return dict(result)

    def discard_journal_publication(
        self,
        prepared: tuple[dict, PreparedParentPublication | None],
    ) -> None:
        """Discard an uncommitted parent journal temp."""

        publication = prepared[1]
        if publication is not None:
            _discard_prepared_parent_publish(publication)

    def journal_destination(self, name: str) -> Path:
        """Mint the one checkpoint-owned publication path for a journal file."""

        if not name or name != Path(name).name or name in {".", ".."}:
            raise BlenderError(
                f"journal name must be a plain file name, found {name!r}"
            )
        return self.journals / name

    def journal(
        self,
        path: str | None = None,
        clear: bool = False,
        limit: int | None = None,
        start: int = 0,
    ) -> dict:
        """Stage then parent-publish a journal for non-attempt callers."""

        if not hasattr(self, "_worker_publications"):
            return self.call(
                "journal",
                path=path,
                clear=clear,
                limit=limit,
                start=start,
            )
        return self.publish_journal(
            self.stage_journal(path=path, clear=clear, limit=limit, start=start)
        )

    def replay(self, start: int = 0) -> dict:
        """Re-exec journalled run_bpy calls from `start` (use after restore)."""
        return self.call("replay", start=start)

    def restore(self, blend: str) -> dict:
        """Restore a .blend checkpoint (exact scene state at snapshot time).

        Parent-published checkpoints live under ``checkpoints/blender``, a root the
        confined worker cannot see: the first best-round restore under confinement died
        with ENOENT inside the sandbox (run 20260902T165518Z-004470, 2.exterior_massing).
        The parent re-stages the exact published bytes into the worker's publication
        scratch, verifies them, and hands the worker that visible path (HIR-0174).
        """
        published = Path(blend).absolute()
        if not hasattr(self, "_worker_publications"):
            return self.call("restore", blend=str(published))
        scratch = self._worker_publications / "snapshots"
        try:
            published.relative_to(self.snapshots.absolute())
        except ValueError:
            try:
                published.relative_to(scratch.absolute())
            except ValueError as exc:
                raise BlenderError(
                    f"restore accepts a published checkpoint under {self.snapshots} or "
                    f"its staged copy under {scratch}, found {published}"
                ) from exc
            staged = published
            if not staged.is_file():
                raise BlenderError(f"staged checkpoint is absent: {staged}") from None
        else:
            if not published.is_file():
                raise BlenderError(f"published checkpoint is absent: {published}")
            payload = published.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            scratch.mkdir(parents=True, exist_ok=True)
            staged = scratch / published.name
            if not (
                staged.is_file()
                and hashlib.sha256(staged.read_bytes()).hexdigest() == digest
            ):
                temporary = staged.with_name(f".{staged.name}.restore.{os.getpid()}")
                temporary.write_bytes(payload)
                os.replace(temporary, staged)
        result = self.call("restore", blend=str(staged))
        result["checkpoint"] = str(published)
        return result
