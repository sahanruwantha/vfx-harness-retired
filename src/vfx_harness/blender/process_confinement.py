"""Irreversible syscall confinement for the long-lived Blender worker."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno

_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO = 0x00050000
_SCMP_FLTATR_CTL_TSYNC = 4
_DENIED_SYSCALLS = (
    "accept",
    "accept4",
    "bind",
    "connect",
    "execve",
    "execveat",
    "listen",
    "recvfrom",
    "recvmmsg",
    "recvmsg",
    "sendmmsg",
    "sendmsg",
    "sendto",
    "socket",
    "socketpair",
)


class WorkerConfinementError(RuntimeError):
    """Required OS worker confinement could not be installed."""


def install_worker_seccomp() -> None:
    """Deny network creation/use and execution of any later process image."""

    library = ctypes.util.find_library("seccomp")
    if not library:
        raise WorkerConfinementError(
            "libseccomp is required for Blender worker network/process confinement"
        )
    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    seccomp.seccomp_attr_set.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int

    context = seccomp.seccomp_init(_SCMP_ACT_ALLOW)
    if not context:
        raise WorkerConfinementError("libseccomp could not allocate a filter context")
    try:
        sync_result = seccomp.seccomp_attr_set(
            context,
            _SCMP_FLTATR_CTL_TSYNC,
            1,
        )
        if sync_result != 0:
            raise WorkerConfinementError(
                "libseccomp could not enable thread-synchronized confinement: "
                f"result {sync_result}"
            )
        action = _SCMP_ACT_ERRNO | errno.EPERM
        for name in _DENIED_SYSCALLS:
            number = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0:
                continue
            result = seccomp.seccomp_rule_add(context, action, number, 0)
            if result != 0:
                raise WorkerConfinementError(
                    f"libseccomp could not deny syscall {name}: result {result}"
                )
        result = seccomp.seccomp_load(context)
        if result != 0:
            raise WorkerConfinementError(
                f"libseccomp could not load Blender worker filter: result {result}"
            )
    finally:
        seccomp.seccomp_release(context)
