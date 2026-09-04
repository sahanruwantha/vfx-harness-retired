"""The confined Blender worker renders on the host GPU, and preflight proves it (HIR-0194).

`--dev /dev` mounts a minimal devtmpfs with no GPU device node, so a confined Blender fell
back to llvmpipe software OpenGL on every render. The confinement now dev-binds the present
nodes, the worker ping reports the GPU platform, and strict preflight fails closed when the
host exposes a GPU but the worker reports software rendering.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from vfx_harness.application import preflight
from vfx_harness.blender import filesystem_confinement


def _present(monkeypatch: pytest.MonkeyPatch, nodes: set[str], indexed: tuple[str, ...] = ()) -> None:
    original_exists = Path.exists
    original_is_dir = Path.is_dir
    original_iterdir = Path.iterdir

    def exists(self: Path) -> bool:
        if str(self) in nodes:
            return True
        if str(self).startswith("/dev/"):
            return False
        return original_exists(self)

    def is_dir(self: Path) -> bool:
        if str(self) == "/dev":
            return True
        return original_is_dir(self)

    def iterdir(self: Path):
        if str(self) == "/dev":
            return iter(Path("/dev") / name for name in ("null", "tty", *indexed))
        return original_iterdir(self)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(Path, "iterdir", iterdir)


def test_gpu_device_binds_follow_the_host_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    _present(monkeypatch, {"/dev/dri", "/dev/nvidiactl", "/dev/nvidia-uvm"}, indexed=("nvidia1", "nvidia0"))

    nodes = filesystem_confinement.host_gpu_device_nodes()

    assert [str(node) for node in nodes] == [
        "/dev/dri",
        "/dev/nvidiactl",
        "/dev/nvidia-uvm",
        "/dev/nvidia0",
        "/dev/nvidia1",
    ]
    binds = filesystem_confinement.gpu_device_binds()
    assert binds[:3] == ("--dev-bind", "/dev/dri", "/dev/dri")
    assert binds.count("--dev-bind") == 5

    _present(monkeypatch, set())
    assert filesystem_confinement.host_gpu_device_nodes() == ()
    assert filesystem_confinement.gpu_device_binds() == ()


class _FakeSession:
    """The confinement smoke session, with a configurable ping."""

    ping_payload: ClassVar[dict] = {}
    booted: ClassVar[list] = []

    def __init__(self, *, blender: str, cwd: Path, boot_timeout: float) -> None:
        self.blender = blender
        self.artifacts = Path(cwd) / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.booted.append(self)

    def start(self) -> None:
        pass

    def ping(self) -> dict:
        return dict(self.ping_payload)

    def run(self, _code: str, *, journal: bool = False) -> dict:
        (self.artifacts / "capability-probe.txt").write_text("active", encoding="utf-8")
        return {
            "result": {
                "declared": "declared",
                "old_hidden": True,
                "sibling_hidden": True,
                "host_hidden": True,
                "environment_hidden": True,
            }
        }

    def check(self, *, kind: str) -> dict:
        return {"gate": {"ok": True}}

    def close(self) -> None:
        pass


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(preflight, "BlenderSession", _FakeSession)
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    monkeypatch.setattr(preflight.ctypes.util, "find_library", lambda name: "libseccomp.so.2")

    def run(*, ping: dict, host_nodes: tuple[str, ...]) -> dict:
        # The probe is memoized per process; each case is a fresh host.
        cache_clear = getattr(preflight._probe_blender_confinement, "cache_clear", None)
        if cache_clear is not None:
            cache_clear()
        _FakeSession.ping_payload = ping
        monkeypatch.setattr(
            preflight.filesystem_confinement,
            "host_gpu_device_nodes",
            lambda: tuple(Path(node) for node in host_nodes),
        )
        return preflight._probe_blender_confinement("/usr/bin/blender")

    return run


def test_preflight_fails_closed_on_software_rendering_when_the_host_has_a_gpu(probe) -> None:
    software = {"renderer": "llvmpipe (LLVM 20.1.2, 256 bits)", "backend": "OPENGL", "device_type": "SOFTWARE"}
    result = probe(
        ping={"blender": "5.2.1 LTS", "eevee": "BLENDER_EEVEE", "gpu": software},
        host_nodes=("/dev/dri", "/dev/nvidia0"),
    )

    assert result["ok"] is False
    [problem] = result["problems"]
    assert "software OpenGL" in problem and "llvmpipe" in problem and "/dev/nvidia0" in problem
    assert result["worker_gpu"] == software
    assert result["host_gpu_device_nodes"] == ["/dev/dri", "/dev/nvidia0"]

    # A worker that reports no GPU platform at all is the same failure, with the reason named.
    result = probe(
        ping={"blender": "5.2.1 LTS", "eevee": "BLENDER_EEVEE", "gpu": {"error": "EGL_BAD_MATCH"}},
        host_nodes=("/dev/dri",),
    )
    assert result["ok"] is False and "EGL_BAD_MATCH" in result["problems"][0]


def test_preflight_passes_on_hardware_or_on_a_host_without_a_gpu(probe) -> None:
    nvidia = {"renderer": "NVIDIA GeForce RTX 2050/PCIe/SSE2", "backend": "OPENGL", "device_type": "NVIDIA"}
    result = probe(
        ping={"blender": "5.2.1 LTS", "eevee": "BLENDER_EEVEE", "gpu": nvidia},
        host_nodes=("/dev/dri", "/dev/nvidia0"),
    )
    assert result["ok"] is True and result["worker_gpu"] == nvidia

    software = {"renderer": "llvmpipe", "backend": "OPENGL", "device_type": "SOFTWARE"}
    result = probe(ping={"blender": "5.2.1 LTS", "eevee": "BLENDER_EEVEE", "gpu": software}, host_nodes=())
    assert result["ok"] is True, "software rendering is the honest answer on a host without a GPU"
    assert result["host_gpu_device_nodes"] == []


def test_environment_result_carries_the_gpu_observation(probe) -> None:
    nvidia = {"renderer": "NVIDIA GeForce RTX 2050/PCIe/SSE2", "backend": "OPENGL", "device_type": "NVIDIA"}
    section = probe(ping={"blender": "5.2.1 LTS", "eevee": "BLENDER_EEVEE", "gpu": nvidia}, host_nodes=("/dev/dri",))
    raw = {
        "ok": True,
        "auth": {"ok": True, "using": "api", "problems": [], "notes": [], "present": [], "decoys": []},
        "configuration": {"ok": True, "problems": []},
        "blender": {"ok": True, "requested": "blender", "resolved": "/usr/bin/blender", "problems": []},
        "blender_confinement": section,
        "builder_execution_fence": {"ok": True, "mechanism": "sysv-sem-undo+descriptor-flock", "problems": []},
        "plan_consumer_directory": {"ok": True, "mechanism": "fanotify-target-fid+openat2", "problems": []},
    }

    result = preflight.environment_result(raw)

    confinement = next(check for check in result.checks if check.check_id == "blender_confinement")
    assert confinement.passed
    assert result.probe_spec is not None and result.probe_spec.probe_revision == 6
    # The observation digest binds the GPU platform: a worker that silently dropped to
    # software rendering would change the confinement's observed identity.
    software = {"renderer": "llvmpipe", "backend": "OPENGL", "device_type": "SOFTWARE"}
    software_section = dict(section, worker_gpu=software)
    other = preflight.environment_result(dict(raw, blender_confinement=software_section))
    other_confinement = next(check for check in other.checks if check.check_id == "blender_confinement")
    assert other_confinement.observed_digest != confinement.observed_digest
