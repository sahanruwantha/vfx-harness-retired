"""A real confined Blender worker renders on the host GPU when the host has one (HIR-0194)."""

from __future__ import annotations

import ctypes.util
import shutil

import pytest

from vfx_harness.application import preflight
from vfx_harness.blender import filesystem_confinement

pytestmark = pytest.mark.integration


def test_confined_worker_reports_the_host_gpu_not_software_gl() -> None:
    resolved, problems = preflight._resolve_blender(preflight.os.environ.get("BLENDER_BIN") or "blender")
    if resolved is None or problems:
        pytest.skip(f"no confinable Blender on this host: {problems}")
    if shutil.which("bwrap") is None or ctypes.util.find_library("seccomp") is None:
        pytest.skip("bubblewrap or libseccomp unavailable")
    host_nodes = filesystem_confinement.host_gpu_device_nodes()
    if not host_nodes:
        pytest.skip("host exposes no GPU device nodes; software rendering is the honest answer")

    section = preflight._probe_blender_confinement(resolved)

    assert section["problems"] == [], section["problems"]
    gpu = section["worker_gpu"]
    assert isinstance(gpu, dict) and "error" not in gpu, gpu
    assert gpu["device_type"] != "SOFTWARE", gpu
    assert [str(node) for node in host_nodes] == section["host_gpu_device_nodes"]
