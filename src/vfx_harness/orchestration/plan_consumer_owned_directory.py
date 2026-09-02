"""Kernel-proven creation of owned plan-consumer directories.

``mkdirat`` does not return a descriptor.  A later open cannot, by itself, prove
that it opened the inode created by the caller.  This Linux boundary marks the
already-held parent with fanotify ``FAN_REPORT_TARGET_FID`` before ``mkdirat``.
The resulting ``FAN_CREATE`` carries the new directory's opaque kernel file
handle; adoption succeeds only when that handle exactly matches
``name_to_handle_at(AT_EMPTY_PATH | AT_HANDLE_FID)`` on the restrictively opened
child descriptor.  A name swap therefore cannot turn a foreign inode into owned
state, even when it lands between creation and capture.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import select
import stat
import struct
import sys
import time
from dataclasses import dataclass

from vfx_harness.observability.prepared_publication_descriptors import (
    block_deferred_signals,
)
from vfx_harness.observability.run_owner_fork_guard import ForkProtectedAcquisition
from vfx_harness.orchestration.plan_consumer_view_cleanup import (
    move_owned_directory_noreplace,
)
from vfx_harness.orchestration.plan_consumer_view_descriptors import (
    PlanConsumerDirectoryIdentity,
    PlanConsumerViewMutationConflict,
)

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)

_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_MAGICLINKS = 0x02
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08
_OPENAT2_RESOLVE = (
    _RESOLVE_NO_XDEV
    | _RESOLVE_NO_MAGICLINKS
    | _RESOLVE_NO_SYMLINKS
    | _RESOLVE_BENEATH
)
_SYS_OPENAT2 = 437

_FAN_CLOEXEC = 0x00000001
_FAN_NONBLOCK = 0x00000002
_FAN_REPORT_FID = 0x00000200
_FAN_REPORT_DIR_FID = 0x00000400
_FAN_REPORT_NAME = 0x00000800
_FAN_REPORT_TARGET_FID = 0x00001000
_FAN_REPORT_DFID_NAME_TARGET = (
    _FAN_REPORT_FID
    | _FAN_REPORT_DIR_FID
    | _FAN_REPORT_NAME
    | _FAN_REPORT_TARGET_FID
)
_FAN_CREATE = 0x00000100
_FAN_ONDIR = 0x40000000
_FAN_Q_OVERFLOW = 0x00004000
_FAN_MARK_ADD = 0x00000001
_FAN_MARK_ONLYDIR = 0x00000008
_FAN_EVENT_INFO_TYPE_FID = 1
_FAN_EVENT_INFO_TYPE_DFID_NAME = 2
_FANOTIFY_METADATA_VERSION = 3
_FAN_NOFD = -1

_AT_EMPTY_PATH = 0x1000
_AT_HANDLE_FID = 0x0200
_MAX_HANDLE_BYTES = 128

_EVENT_METADATA = struct.Struct("=IBBHQii")
_INFO_HEADER = struct.Struct("=BBH")
_FILE_HANDLE_HEADER = struct.Struct("=Ii")

_LIBC = ctypes.CDLL(None, use_errno=True)
_SYSCALL = getattr(_LIBC, "syscall", None)
_FANOTIFY_INIT = getattr(_LIBC, "fanotify_init", None)
_FANOTIFY_MARK = getattr(_LIBC, "fanotify_mark", None)
_NAME_TO_HANDLE_AT = getattr(_LIBC, "name_to_handle_at", None)
if _SYSCALL is not None:
    _SYSCALL.restype = ctypes.c_long
if _FANOTIFY_INIT is not None:
    _FANOTIFY_INIT.argtypes = (ctypes.c_uint, ctypes.c_uint)
    _FANOTIFY_INIT.restype = ctypes.c_int
if _FANOTIFY_MARK is not None:
    _FANOTIFY_MARK.argtypes = (
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_ulonglong,
        ctypes.c_int,
        ctypes.c_char_p,
    )
    _FANOTIFY_MARK.restype = ctypes.c_int


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


class _FileHandle(ctypes.Structure):
    _fields_ = (
        ("handle_bytes", ctypes.c_uint),
        ("handle_type", ctypes.c_int),
        ("handle", ctypes.c_ubyte * _MAX_HANDLE_BYTES),
    )


@dataclass(frozen=True, slots=True)
class _OpaqueFileHandle:
    handle_type: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class _CreateEvent:
    name: str
    parent: _OpaqueFileHandle
    target: _OpaqueFileHandle


def _exact_child_name(name: str, where: str) -> None:
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name:
        raise PlanConsumerViewMutationConflict(f"{where} requires one exact child name")


def _fanotify_descriptor() -> int:
    if _FANOTIFY_INIT is None or _FANOTIFY_MARK is None:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer owned-directory creation requires Linux fanotify target-FID reporting"
        )
    descriptor = _FANOTIFY_INIT(
        _FAN_CLOEXEC | _FAN_NONBLOCK | _FAN_REPORT_DFID_NAME_TARGET,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    if descriptor >= 0:
        os.set_inheritable(descriptor, False)
        return descriptor
    observed_errno = ctypes.get_errno()
    raise PlanConsumerViewMutationConflict(
        "plan-consumer owned-directory creation could not initialize unprivileged "
        "fanotify target-FID reporting"
    ) from OSError(observed_errno, os.strerror(observed_errno))


def _watch_parent(
    parent_descriptor: int,
    acquisition: ForkProtectedAcquisition,
) -> int:
    watch = acquisition.open_descriptor(_fanotify_descriptor)
    assert _FANOTIFY_MARK is not None
    result = _FANOTIFY_MARK(
        watch,
        _FAN_MARK_ADD | _FAN_MARK_ONLYDIR,
        _FAN_CREATE | _FAN_ONDIR,
        parent_descriptor,
        None,
    )
    if result == 0:
        return watch
    observed_errno = ctypes.get_errno()
    acquisition.retire(watch)
    raise PlanConsumerViewMutationConflict(
        "plan-consumer owned-directory creation could not mark the exact held parent "
        "for target-FID events"
    ) from OSError(observed_errno, os.strerror(observed_errno))


def open_beneath_directory(parent_descriptor: int, name: str) -> int:
    """Open one exact real child without links, magic links, or mount crossing."""

    _exact_child_name(name, "plan-consumer directory traversal")
    if _SYSCALL is None:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer directory traversal requires Linux openat2"
        )
    how = _OpenHow(
        flags=_DIRECTORY_FLAGS,
        mode=0,
        resolve=_OPENAT2_RESOLVE,
    )
    ctypes.set_errno(0)
    result = _SYSCALL(
        ctypes.c_long(_SYS_OPENAT2),
        ctypes.c_int(parent_descriptor),
        ctypes.c_char_p(os.fsencode(name)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if result >= 0:
        return int(result)
    observed_errno = ctypes.get_errno()
    if observed_errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer directory traversal requires openat2 with "
            "RESOLVE_BENEATH, RESOLVE_NO_SYMLINKS, and RESOLVE_NO_XDEV"
        ) from OSError(observed_errno, os.strerror(observed_errno))
    raise OSError(observed_errno, os.strerror(observed_errno), name)


def _descriptor_handle(descriptor: int) -> _OpaqueFileHandle:
    if _NAME_TO_HANDLE_AT is None:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer owned-directory creation requires name_to_handle_at"
        )
    handle = _FileHandle()
    handle.handle_bytes = _MAX_HANDLE_BYTES
    mount_id = ctypes.c_int()
    result = _NAME_TO_HANDLE_AT(
        descriptor,
        ctypes.c_char_p(b""),
        ctypes.byref(handle),
        ctypes.byref(mount_id),
        _AT_EMPTY_PATH | _AT_HANDLE_FID,
    )
    if result != 0:
        observed_errno = ctypes.get_errno()
        raise PlanConsumerViewMutationConflict(
            "plan-consumer owned-directory descriptor has no comparable kernel file handle"
        ) from OSError(observed_errno, os.strerror(observed_errno))
    if handle.handle_bytes > _MAX_HANDLE_BYTES:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer owned-directory kernel file handle exceeds the closed bound"
        )
    return _OpaqueFileHandle(
        handle_type=handle.handle_type,
        payload=bytes(handle.handle[: handle.handle_bytes]),
    )


def _parse_fid_record(
    payload: bytes,
    offset: int,
    length: int,
) -> tuple[bytes, _OpaqueFileHandle, int]:
    handle_offset = offset + _INFO_HEADER.size + 8
    if length < _INFO_HEADER.size + 8 + _FILE_HANDLE_HEADER.size:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer fanotify FID record is truncated"
        )
    handle_bytes, handle_type = _FILE_HANDLE_HEADER.unpack_from(payload, handle_offset)
    handle_start = handle_offset + _FILE_HANDLE_HEADER.size
    handle_end = handle_start + handle_bytes
    record_end = offset + length
    if handle_bytes > _MAX_HANDLE_BYTES or handle_end > record_end:
        raise PlanConsumerViewMutationConflict(
            "plan-consumer fanotify FID payload exceeds its closed record"
        )
    return (
        payload[offset + _INFO_HEADER.size : handle_offset],
        _OpaqueFileHandle(handle_type, payload[handle_start:handle_end]),
        handle_end,
    )


def _parse_events(payload: bytes) -> tuple[_CreateEvent, ...]:
    events: list[_CreateEvent] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < _EVENT_METADATA.size:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer fanotify event metadata is truncated"
            )
        event_len, version, _reserved, metadata_len, mask, event_fd, pid = (
            _EVENT_METADATA.unpack_from(payload, offset)
        )
        if (
            version != _FANOTIFY_METADATA_VERSION
            or metadata_len < _EVENT_METADATA.size
            or event_len < metadata_len
            or offset + event_len > len(payload)
            or event_fd != _FAN_NOFD
            or pid != os.getpid()
        ):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer fanotify event has an unsupported closed schema"
            )
        if mask & _FAN_Q_OVERFLOW:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer fanotify queue overflowed before inode proof"
            )
        if mask != _FAN_CREATE | _FAN_ONDIR:
            raise PlanConsumerViewMutationConflict(
                "plan-consumer fanotify event has unexpected mask bits"
            )
        info_offset = offset + metadata_len
        name: str | None = None
        parent: _OpaqueFileHandle | None = None
        parent_fsid: bytes | None = None
        target: _OpaqueFileHandle | None = None
        target_fsid: bytes | None = None
        parent_records = 0
        target_records = 0
        while info_offset < offset + event_len:
            if offset + event_len - info_offset < _INFO_HEADER.size:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer fanotify info header is truncated"
                )
            info_type, _pad, info_len = _INFO_HEADER.unpack_from(payload, info_offset)
            if info_len < _INFO_HEADER.size or info_offset + info_len > offset + event_len:
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer fanotify info record has an invalid length"
            )
            if info_type in {_FAN_EVENT_INFO_TYPE_FID, _FAN_EVENT_INFO_TYPE_DFID_NAME}:
                fsid, handle, handle_end = _parse_fid_record(
                    payload,
                    info_offset,
                    info_len,
                )
                if info_type == _FAN_EVENT_INFO_TYPE_FID:
                    target_records += 1
                    target_fsid = fsid
                    target = handle
                else:
                    parent_records += 1
                    parent_fsid = fsid
                    parent = handle
                    name_payload = payload[handle_end : info_offset + info_len]
                    try:
                        terminator = name_payload.index(0)
                    except ValueError as exc:
                        raise PlanConsumerViewMutationConflict(
                            "plan-consumer fanotify DFID_NAME has no NUL terminator"
                        ) from exc
                    if any(name_payload[terminator:]):
                        raise PlanConsumerViewMutationConflict(
                            "plan-consumer fanotify DFID_NAME has nonzero alignment bytes"
                        )
                    try:
                        name = name_payload[:terminator].decode(
                            sys.getfilesystemencoding(),
                            errors="strict",
                        )
                    except UnicodeDecodeError as exc:
                        raise PlanConsumerViewMutationConflict(
                            "plan-consumer fanotify DFID_NAME is not a valid filesystem name"
                        ) from exc
            else:
                raise PlanConsumerViewMutationConflict(
                    f"plan-consumer fanotify event has unknown info type {info_type}"
                )
            info_offset += info_len
        if mask & _FAN_CREATE and mask & _FAN_ONDIR:
            if (
                name is None
                or parent is None
                or target is None
                or parent_records != 1
                or target_records != 1
                or parent_fsid != target_fsid
            ):
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer directory-create event lacks one exact parent/name/target FID"
                )
            events.append(_CreateEvent(name=name, parent=parent, target=target))
        offset += event_len
    return tuple(events)


def _create_events_through_target(
    descriptor: int,
    name: str,
) -> tuple[_CreateEvent, ...]:
    poller = select.poll()
    poller.register(descriptor, select.POLLIN | select.POLLERR | select.POLLHUP)
    deadline = time.monotonic() + 5.0
    observed: list[_CreateEvent] = []
    while True:
        while True:
            try:
                payload = os.read(descriptor, 64 * 1024)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    break
                raise PlanConsumerViewMutationConflict(
                    "plan-consumer fanotify inode proof could not be read"
                ) from exc
            if not payload:
                break
            observed.extend(_parse_events(payload))
        if any(event.name == name for event in observed):
            return tuple(observed)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not poller.poll(max(1, int(remaining * 1000))):
            raise PlanConsumerViewMutationConflict(
                "plan-consumer owned-directory target-FID event was not observed"
            )


def create_and_capture_empty_directory(
    parent_descriptor: int,
    name: str,
    acquisition: ForkProtectedAcquisition,
    *,
    where: str,
) -> tuple[int, PlanConsumerDirectoryIdentity]:
    """Create a child and adopt only the inode named by its kernel create event."""

    _exact_child_name(name, where)
    parent_before = PlanConsumerDirectoryIdentity.capture(parent_descriptor)
    watch = _watch_parent(parent_descriptor, acquisition)
    child_descriptor: int | None = None
    open_error: OSError | None = None
    try:
        with block_deferred_signals():
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            try:
                child_descriptor = acquisition.open_descriptor(
                    lambda: open_beneath_directory(parent_descriptor, name)
                )
                os.set_inheritable(child_descriptor, False)
            except OSError as exc:
                open_error = exc
            observed_events = _create_events_through_target(watch, name)
    except FileExistsError:
        raise
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"{where} could not create and capture its exact directory inode"
        ) from exc
    if open_error is not None or child_descriptor is None:
        raise PlanConsumerViewMutationConflict(
            f"{where} directory was substituted or removed before descriptor capture"
        ) from open_error

    exact_events = tuple(event for event in observed_events if event.name == name)
    parent_handle = _descriptor_handle(parent_descriptor)
    descriptor_handle = _descriptor_handle(child_descriptor)
    if (
        len(exact_events) != 1
        or exact_events[0].parent != parent_handle
        or exact_events[0].target != descriptor_handle
    ):
        raise PlanConsumerViewMutationConflict(
            f"{where} opened an inode different from the kernel-observed mkdir target; "
            "no observed generation was adopted"
        )
    try:
        held = os.fstat(child_descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        children = os.listdir(child_descriptor)
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"{where} directory could not be post-proven through its held descriptor"
        ) from exc
    held_identity = PlanConsumerDirectoryIdentity(
        held.st_dev,
        held.st_ino,
        stat.S_IFMT(held.st_mode),
    )
    named_identity = PlanConsumerDirectoryIdentity(
        named.st_dev,
        named.st_ino,
        stat.S_IFMT(named.st_mode),
    )
    if (
        not stat.S_ISDIR(held.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or named_identity != held_identity
        or children
        or PlanConsumerDirectoryIdentity.capture(parent_descriptor) != parent_before
    ):
        raise PlanConsumerViewMutationConflict(
            f"{where} did not retain the exact empty directory observed by the kernel"
        )
    acquisition.retire(watch)
    return child_descriptor, held_identity


def create_and_publish_empty_directory(
    parent_descriptor: int,
    name: str,
    acquisition: ForkProtectedAcquisition,
    *,
    where: str,
) -> tuple[int, PlanConsumerDirectoryIdentity]:
    """Publish a target-FID-proven random staging inode at an absent child name."""

    _exact_child_name(name, where)
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"{where} destination could not be inspected"
        ) from exc
    else:
        raise FileExistsError(errno.EEXIST, f"{where} destination exists", name)

    for _attempt in range(32):
        staging_name = f".plan-consumer-directory.tmp-{secrets.token_hex(16)}"
        try:
            descriptor, identity = create_and_capture_empty_directory(
                parent_descriptor,
                staging_name,
                acquisition,
                where=f"{where} private staging",
            )
        except FileExistsError:
            continue
        break
    else:  # pragma: no cover - cryptographic collision bound
        raise PlanConsumerViewMutationConflict(
            f"{where} exhausted unique private staging names"
        )

    with block_deferred_signals():
        os.fsync(descriptor)
        move_owned_directory_noreplace(
            parent_descriptor,
            staging_name,
            name,
            identity,
            where=f"{where} no-replace publication",
        )
    try:
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        held = os.fstat(descriptor)
    except OSError as exc:
        raise PlanConsumerViewMutationConflict(
            f"{where} could not post-prove its published directory inode"
        ) from exc
    if _identity(named) != identity or _identity(held) != identity:
        raise PlanConsumerViewMutationConflict(
            f"{where} published name does not retain its exact staged inode"
        )
    return descriptor, identity


def _identity(observed: os.stat_result) -> PlanConsumerDirectoryIdentity:
    return PlanConsumerDirectoryIdentity(
        observed.st_dev,
        observed.st_ino,
        stat.S_IFMT(observed.st_mode),
    )


__all__: list[str] = []
