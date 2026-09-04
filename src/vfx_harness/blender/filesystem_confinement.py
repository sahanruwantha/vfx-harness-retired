"""Descriptor-pinned filesystem capabilities for Blender worker processes."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vfx_harness.infrastructure.trusted_files import (
    PinnedTrustedDirectory,
    PinnedTrustedFile,
    TrustedDirectoryBinding,
    TrustedFileBinding,
    TrustedFileError,
    open_pinned_trusted_directory,
    open_pinned_trusted_file,
)

FILESYSTEM_SANDBOX = "bwrap"
HARNESS_RUNTIME_ROOT = Path(__file__).parent.parent
# GPU device nodes the worker must see to render on hardware. ``--dev /dev`` mounts a
# minimal devtmpfs with none of them, so a confined Blender silently falls back to
# llvmpipe software OpenGL: a 1080p volumetric EEVEE frame that takes 0.9 s on the host
# GPU took 21 s inside the sandbox (HIR-0194). Nodes are bound read-write only when they
# exist; a host without a GPU keeps the same confinement and reports software rendering.
GPU_DEVICE_NODES = (
    Path("/dev/dri"),
    Path("/dev/nvidiactl"),
    Path("/dev/nvidia-uvm"),
    Path("/dev/nvidia-uvm-tools"),
    Path("/dev/nvidia-modeset"),
    Path("/dev/nvidia-caps"),
)
_GPU_DEVICE_INDEX = re.compile(r"^nvidia[0-9]+$")


def host_gpu_device_nodes() -> tuple[Path, ...]:
    """Every GPU device node present on the host, in a stable order."""

    present = [node for node in GPU_DEVICE_NODES if node.exists()]
    dev = Path("/dev")
    if dev.is_dir():
        present.extend(
            sorted(child for child in dev.iterdir() if _GPU_DEVICE_INDEX.match(child.name))
        )
    return tuple(present)


def gpu_device_binds() -> tuple[str, ...]:
    """``--dev-bind`` arguments for every present GPU device node."""

    argv: list[str] = []
    for node in host_gpu_device_nodes():
        argv.extend(("--dev-bind", str(node), str(node)))
    return tuple(argv)


SYSTEM_RUNTIME_ROOTS = (
    Path("/etc"),
    Path("/opt"),
    Path("/snap"),
    Path("/sys"),
    Path("/usr"),
)
DECLARED_SHOT_READ_ROOTS = (
    Path("assets"),
    Path("build/construction"),
    Path("refs"),
)


class BlenderError(RuntimeError):
    """A Blender process or its mandatory confinement boundary failed."""


@dataclass(frozen=True, slots=True)
class PreparedWorkerCommand:
    """One worker command whose inherited descriptors stay live in this context."""

    argv: tuple[str, ...]
    pass_fds: tuple[int, ...]


def open_real_directory(path: Path, where: str) -> int:
    """Open every path component without following symlinks and return the leaf FD."""

    absolute = path.expanduser().absolute()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, flags)
        for part in absolute.parts[1:]:
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise BlenderError(f"{where} must be a real directory path: {absolute}") from exc
    if descriptor is None:
        raise BlenderError(f"{where} must be a real directory path: {absolute}")
    return descriptor


def confined_worker_argv(
    blender_argv: list[str],
    *,
    writable_roots: tuple[Path, ...],
    writable_root_descriptors: tuple[int, ...] = (),
    readable_roots: tuple[Path, ...] = (),
    readable_root_descriptors: tuple[int, ...] = (),
    readable_files: tuple[Path, ...] = (),
    readable_file_descriptors: tuple[int, ...] = (),
    runtime_root_descriptor: int | None = None,
    authority_root: Path | None = None,
    current_run_root: Path | None = None,
) -> list[str]:
    """Build a worker command with an explicit filesystem capability view.

    Shot-bound workers do not inherit a read-only host root. They see only system
    runtime roots, the harness runtime, declared shot inputs, an optional immutable
    replay snapshot, and the exact active worker scratch.
    """

    sandbox = shutil.which(FILESYSTEM_SANDBOX)
    if sandbox is None:
        raise BlenderError(
            "Blender worker requires bubblewrap (`bwrap`) for read-only authority "
            "confinement; install it and rerun strict preflight"
        )
    if writable_root_descriptors and len(writable_root_descriptors) != len(
        writable_roots
    ):
        raise BlenderError(
            "Blender confinement requires one descriptor for every writable root"
        )
    if readable_root_descriptors and len(readable_root_descriptors) != len(
        readable_roots
    ):
        raise BlenderError(
            "Blender confinement requires one descriptor for every readable root"
        )
    if readable_file_descriptors and len(readable_file_descriptors) != len(
        readable_files
    ):
        raise BlenderError(
            "Blender confinement requires one descriptor for every readable file"
        )

    authority = (
        authority_root.expanduser().resolve(strict=True)
        if authority_root is not None
        else None
    )
    run_root = (
        current_run_root.expanduser().resolve(strict=True)
        if current_run_root is not None
        else None
    )
    if authority is not None and run_root is None:
        raise BlenderError(
            "shot-bound Blender confinement requires the exact current run root"
        )
    if authority is not None and runtime_root_descriptor is None:
        raise BlenderError(
            "shot-bound Blender confinement requires a descriptor-pinned harness runtime"
        )
    if authority is None and current_run_root is not None:
        raise BlenderError("Blender current run root requires shot authority")

    roots_list: list[Path] = []
    for index, supplied in enumerate((*writable_roots, *readable_roots)):
        writable_root = index < len(writable_roots)
        root_kind = "writable" if writable_root else "readable"
        raw = supplied.expanduser().absolute()
        if not raw.is_dir():
            raise BlenderError(f"Blender {root_kind} root is not a directory: {raw}")
        current = Path(raw.anchor)
        for part in raw.parts[1:]:
            current /= part
            if current.is_symlink():
                raise BlenderError(
                    f"Blender {root_kind} root contains symlink component: {current}"
                )
        resolved = raw.resolve(strict=True)
        if authority is not None:
            assert run_root is not None
            try:
                run_relative = run_root.relative_to(authority)
                authority_relative = resolved.relative_to(authority)
            except ValueError as exc:
                raise BlenderError(
                    f"shot-bound Blender {root_kind} root escapes shot authority: {resolved}"
                ) from exc
            if len(run_relative.parts) != 2 or run_relative.parts[0] != "runs":
                raise BlenderError(
                    f"shot-bound Blender current run root is invalid: {run_root}"
                )
            try:
                current_run_relative = resolved.relative_to(run_root)
            except ValueError:
                current_run_relative = None
            if writable_root and (
                current_run_relative is None
                or not current_run_relative.parts
                or current_run_relative.parts[0] != "scratch"
            ):
                raise BlenderError(
                    "shot-bound Blender writable root must be under the exact run's "
                    f"scratch tree, found {authority_relative}"
                )
            if not writable_root:
                allowed_shot_root = authority_relative in DECLARED_SHOT_READ_ROOTS
                allowed_replay_root = (
                    current_run_relative is not None
                    and len(current_run_relative.parts) >= 2
                    and current_run_relative.parts[0] == "scratch"
                    and current_run_relative.parts[1] != "blender"
                )
                if not (allowed_shot_root or allowed_replay_root):
                    raise BlenderError(
                        "shot-bound Blender readable root is not a declared shot input "
                        f"or current-run replay snapshot: {authority_relative}"
                    )
        roots_list.append(resolved)
    writable = tuple(roots_list[: len(writable_roots)])
    readable = tuple(roots_list[len(writable_roots) :])
    if set(writable) & set(readable):
        raise BlenderError("Blender confinement root cannot be both readable and writable")
    if authority is not None and not writable_root_descriptors:
        raise BlenderError(
            "shot-bound Blender confinement requires descriptor-pinned writable roots"
        )
    if authority is not None and readable and not readable_root_descriptors:
        raise BlenderError(
            "shot-bound Blender confinement requires descriptor-pinned readable roots"
        )
    files: list[Path] = []
    for supplied in readable_files:
        raw = supplied.expanduser().absolute()
        if not raw.is_file() or raw.is_symlink():
            raise BlenderError(f"Blender readable file is not a real file: {raw}")
        current = Path(raw.anchor)
        for part in raw.parts[1:-1]:
            current /= part
            if current.is_symlink():
                raise BlenderError(
                    f"Blender readable file contains symlink component: {current}"
                )
        resolved = raw.resolve(strict=True)
        if not any(root == resolved or root in resolved.parents for root in readable):
            raise BlenderError(
                "Blender readable file must be a member of a declared readable root: "
                f"{resolved}"
            )
        files.append(resolved)
    readable_members = tuple(files)
    if authority is not None and readable_members and not readable_file_descriptors:
        raise BlenderError(
            "shot-bound Blender confinement requires descriptor-pinned readable files"
        )
    argv = [
        sandbox,
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
    ]
    if authority is not None:
        for runtime_root in SYSTEM_RUNTIME_ROOTS:
            if runtime_root.is_dir() and not runtime_root.is_symlink():
                argv.extend(("--ro-bind", str(runtime_root), str(runtime_root)))
        for link in (Path("/bin"), Path("/lib"), Path("/lib64"), Path("/sbin")):
            if link.is_symlink():
                argv.extend(("--symlink", os.readlink(link), str(link)))
        argv.extend(("--dev", "/dev", *gpu_device_binds(), "--proc", "/proc", "--tmpfs", "/tmp"))

        destinations = (HARNESS_RUNTIME_ROOT, authority, *readable, *writable)
        created: set[Path] = set()
        for destination in destinations:
            current = Path(destination.anchor)
            for part in destination.parts[1:]:
                current /= part
                if current not in created:
                    argv.extend(("--dir", str(current)))
                    created.add(current)
        argv.extend(("--tmpfs", str(authority)))
        created.clear()
        for destination in (*readable, *writable):
            relative = destination.relative_to(authority)
            current = authority
            for part in relative.parts:
                current /= part
                if current not in created:
                    argv.extend(("--dir", str(current)))
                    created.add(current)
        assert runtime_root_descriptor is not None
        argv.extend(
            (
                "--ro-bind-fd",
                str(runtime_root_descriptor),
                str(HARNESS_RUNTIME_ROOT),
            )
        )
        for descriptor, root in zip(
            readable_root_descriptors,
            readable,
            strict=True,
        ):
            argv.extend(("--ro-bind-fd", str(descriptor), str(root)))
        for descriptor, member in zip(
            readable_file_descriptors,
            readable_members,
            strict=True,
        ):
            argv.extend(("--ro-bind-fd", str(descriptor), str(member)))
        for descriptor, root in zip(
            writable_root_descriptors,
            writable,
            strict=True,
        ):
            argv.extend(("--bind-fd", str(descriptor), str(root)))
        argv.extend(
            (
                "--remount-ro",
                str(authority),
                "--clearenv",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "PATH",
                "/usr/local/bin:/usr/bin:/bin",
                "--setenv",
                "PYTHONPATH",
                str(HARNESS_RUNTIME_ROOT.parent),
                "--setenv",
                "LANG",
                "C.UTF-8",
                "--chdir",
                str(authority),
            )
        )
    else:
        argv.extend(
            (
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                *gpu_device_binds(),
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
            )
        )
        for index, root in enumerate(readable):
            if readable_root_descriptors:
                argv.extend(
                    ("--ro-bind-fd", str(readable_root_descriptors[index]), str(root))
                )
            else:
                argv.extend(("--ro-bind", str(root), str(root)))
        for index, member in enumerate(readable_members):
            if readable_file_descriptors:
                argv.extend(
                    (
                        "--ro-bind-fd",
                        str(readable_file_descriptors[index]),
                        str(member),
                    )
                )
            else:
                argv.extend(("--ro-bind", str(member), str(member)))
        for index, root in enumerate(writable):
            if writable_root_descriptors:
                argv.extend(
                    ("--bind-fd", str(writable_root_descriptors[index]), str(root))
                )
            else:
                argv.extend(("--bind", str(root), str(root)))
    return [*argv, "--", *blender_argv]


@contextmanager
def prepared_worker_command(
    blender_argv: list[str],
    *,
    writable_roots: tuple[Path, ...],
    readable_roots: tuple[Path, ...] = (),
    readable_root_bindings: tuple[TrustedDirectoryBinding, ...] = (),
    readable_file_bindings: tuple[TrustedFileBinding, ...] = (),
    authority_root: Path | None = None,
    current_run_root: Path | None = None,
) -> Iterator[PreparedWorkerCommand]:
    """Pin every source root until the child inherits its complete command."""

    authority = (
        authority_root.expanduser().absolute()
        if authority_root is not None
        else None
    )
    selected_readable = readable_roots
    if authority is not None and not selected_readable:
        selected_readable = tuple(
            path
            for relative in DECLARED_SHOT_READ_ROOTS
            if (path := authority / relative).is_dir()
        )
    if readable_root_bindings and len(readable_root_bindings) != len(
        selected_readable
    ):
        raise BlenderError(
            "Blender confinement requires one trusted binding for every bound "
            "readable root"
        )
    descriptors: list[int] = []
    pinned_readable: list[PinnedTrustedDirectory] = []
    pinned_files: list[PinnedTrustedFile] = []
    try:
        writable_descriptors = tuple(
            open_real_directory(path, "Blender writable root")
            for path in writable_roots
        )
        descriptors.extend(writable_descriptors)
        if readable_root_bindings:
            for path, expected in zip(
                selected_readable,
                readable_root_bindings,
                strict=True,
            ):
                if path.expanduser().absolute() != expected.path:
                    raise BlenderError(
                        "Blender readable root disagrees with its trusted binding: "
                        f"root={path.expanduser().absolute()}, binding={expected.path}"
                    )
                try:
                    pinned = open_pinned_trusted_directory(
                        expected.root,
                        expected.path,
                        "Blender declared readable root",
                    )
                    if pinned.binding != expected:
                        pinned.close()
                        raise BlenderError(
                            "Blender readable root changed before worker launch: "
                            f"{expected.path}"
                        )
                except TrustedFileError as exc:
                    raise BlenderError(str(exc)) from exc
                pinned_readable.append(pinned)
            readable_descriptors = tuple(
                pinned.descriptor for pinned in pinned_readable
            )
        else:
            readable_descriptors = tuple(
                open_real_directory(path, "Blender declared readable root")
                for path in selected_readable
            )
            descriptors.extend(readable_descriptors)
        for expected in readable_file_bindings:
            if not any(
                root.expanduser().absolute() == expected.path
                or root.expanduser().absolute() in expected.path.parents
                for root in selected_readable
            ):
                raise BlenderError(
                    "Blender readable member is outside every declared readable root: "
                    f"{expected.path}"
                )
            try:
                pinned_file = open_pinned_trusted_file(
                    expected.root,
                    expected.path,
                    "Blender declared readable member",
                    require_nonempty=False,
                )
                if pinned_file.binding != expected:
                    pinned_file.close()
                    raise BlenderError(
                        "Blender readable member changed before worker launch: "
                        f"{expected.path}"
                    )
                pinned_file.require_current()
            except TrustedFileError as exc:
                raise BlenderError(str(exc)) from exc
            pinned_files.append(pinned_file)
        readable_file_descriptors = tuple(
            pinned.descriptor for pinned in pinned_files
        )
        runtime_descriptor = (
            open_real_directory(HARNESS_RUNTIME_ROOT, "Blender harness runtime root")
            if authority is not None
            else None
        )
        if runtime_descriptor is not None:
            descriptors.append(runtime_descriptor)
        argv = confined_worker_argv(
            blender_argv,
            writable_roots=writable_roots,
            writable_root_descriptors=writable_descriptors,
            readable_roots=selected_readable,
            readable_root_descriptors=readable_descriptors,
            readable_files=tuple(binding.path for binding in readable_file_bindings),
            readable_file_descriptors=readable_file_descriptors,
            runtime_root_descriptor=runtime_descriptor,
            authority_root=authority,
            current_run_root=current_run_root,
        )
        for pinned_file in pinned_files:
            try:
                pinned_file.require_current()
            except TrustedFileError as exc:
                raise BlenderError(str(exc)) from exc
        inherited = (
            *writable_descriptors,
            *readable_descriptors,
            *readable_file_descriptors,
            *((runtime_descriptor,) if runtime_descriptor is not None else ()),
        )
        yield PreparedWorkerCommand(tuple(argv), inherited)
    finally:
        for pinned in pinned_files:
            pinned.close()
        for pinned in pinned_readable:
            pinned.close()
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
