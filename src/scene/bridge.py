"""The client: launch a persistent headless Blender and drive it over the socket.

:class:`BlenderBridge` is what the rest of the project (and, later, the ``render_3d`` leaf) calls.
It spawns ``blender --background --python scene/server.py``, waits for the server's ready-file,
then keeps **one** socket open across every call — so scene state persists and Blender's startup
cost is paid once, not per action. Headless by design; no GUI is ever opened.

The process spawn is isolated in :meth:`_spawn` so the connect/handshake/call logic can be tested
against a fake server with no Blender, matching the project's "control plane is testable" rule.

All runtime paths (ready-file, render dir, log) live under ``$HOME`` because the Blender snap is
confined and cannot see the host's private ``/tmp``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from scene.protocol import recv_message, send_message

_DEFAULT_STARTUP_TIMEOUT = 90.0  # seconds; snap Blender cold-start can be slow
_CONNECT_TIMEOUT = 15.0
# Per-command ceiling on the persistent socket. Must be generous: a heavy build or a high-sample
# render legitimately takes far longer than the connect timeout — which must NOT leak onto the
# socket, or long commands raise TimeoutError mid-render.
_DEFAULT_OP_TIMEOUT = 900.0


class BridgeError(RuntimeError):
    """The bridge could not start, connect, or complete a call."""


def _default_base_dir() -> Path:
    """A per-run workspace under $HOME (snap Blender cannot reach the host /tmp)."""
    base = Path(os.environ.get("BAMBI_BLENDER_HOME", Path.home() / ".cache" / "bambi" / "blender"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _resolve_blender() -> str:
    binary = os.environ.get("BAMBI_BLENDER_BIN") or shutil.which("blender")
    if not binary:
        raise BridgeError("blender not found on PATH; install it or set BAMBI_BLENDER_BIN")
    return binary


class BlenderBridge:
    """A live, headless Blender the agent talks to. Use as a context manager."""

    def __init__(
        self,
        *,
        blender_bin: str | None = None,
        work_dir: str | Path | None = None,
        render_dir: str | Path | None = None,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
        op_timeout: float = _DEFAULT_OP_TIMEOUT,
    ) -> None:
        self._op_timeout = op_timeout
        self._blender_bin = blender_bin or _resolve_blender()
        self._work_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="run-", dir=_default_base_dir()))
        self._render_dir = Path(render_dir) if render_dir else self._work_dir / "renders"
        self._ready_file = self._work_dir / "ready.json"
        self._log_path = self._work_dir / "blender.log"
        self._server_path = Path(__file__).resolve().with_name("server.py")
        self.startup_timeout = startup_timeout
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._logf = None

    # --- lifecycle ------------------------------------------------------------------

    def start(self) -> BlenderBridge:
        if self._sock is not None:
            return self
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._render_dir.mkdir(parents=True, exist_ok=True)
        if self._ready_file.exists():
            self._ready_file.unlink()
        self._proc = self._spawn()
        port = self._await_ready()
        self._sock = self._connect(port)
        return self

    def _spawn(self) -> subprocess.Popen:
        cmd = [
            self._blender_bin,
            "--background",
            "--python",
            str(self._server_path),
            "--",
            "--ready-file",
            str(self._ready_file),
            "--render-dir",
            str(self._render_dir),
            "--port",
            "0",
        ]
        self._logf = open(self._log_path, "w")  # noqa: SIM115 — closed in close()
        return subprocess.Popen(cmd, stdout=self._logf, stderr=subprocess.STDOUT)

    def _await_ready(self) -> int:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise BridgeError(
                    f"blender exited during startup (code {self._proc.returncode}):\n{self._tail_log()}"
                )
            if self._ready_file.exists():
                try:
                    data = json.loads(self._ready_file.read_text())
                except (json.JSONDecodeError, ValueError):
                    time.sleep(0.05)
                    continue
                return int(data["port"])
            time.sleep(0.1)
        raise BridgeError(f"timed out waiting for blender bridge after {self.startup_timeout}s:\n{self._tail_log()}")

    def _connect(self, port: int) -> socket.socket:
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        last: OSError | None = None
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                sock.settimeout(self._op_timeout)  # don't let the 5s connect timeout govern commands
                return sock
            except OSError as exc:
                last = exc
                time.sleep(0.1)
        raise BridgeError(f"could not connect to blender bridge on :{port}: {last}")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self.call("shutdown")
            except (BridgeError, OSError):
                pass
            self._sock.close()
            self._sock = None
        if self._proc is not None:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        if self._logf is not None:
            self._logf.close()
            self._logf = None

    def __enter__(self) -> BlenderBridge:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- calls ----------------------------------------------------------------------

    def call(self, command: str, **params) -> object:
        """Send one command, return its result payload. Raises on a transport-level failure."""
        if self._sock is None:
            raise BridgeError("bridge is not started")
        send_message(self._sock, {"command": command, "params": params})
        response = recv_message(self._sock)
        if response is None:
            raise BridgeError("blender closed the connection")
        if not response.get("ok"):
            raise BridgeError(response.get("error", "unknown bridge error"))
        return response.get("result")

    def ping(self) -> dict:
        return self.call("ping")  # type: ignore[return-value]

    def run_python(self, code: str) -> dict:
        """Run ``bpy`` code. Returns ``{ok, stdout, result|error}`` — domain ``ok`` is inside."""
        return self.call("run_python", code=code)  # type: ignore[return-value]

    def get_scene_graph(self) -> dict:
        return self.call("get_scene_graph")  # type: ignore[return-value]

    def render(self, **params) -> dict:
        return self.call("render_view", **params)  # type: ignore[return-value]

    def render_sequence(self, **params) -> dict:
        return self.call("render_sequence", **params)  # type: ignore[return-value]

    def render_passes(self, **params) -> dict:
        """Render AOVs to a multilayer OpenEXR (Cycles by default) — the passes compositing works from."""
        return self.call("render_passes", **params)  # type: ignore[return-value]

    def save_blend(self, **params) -> dict:
        return self.call("save_blend", **params)  # type: ignore[return-value]

    def import_glb(self, **params) -> dict:
        return self.call("import_glb", **params)  # type: ignore[return-value]

    def normalize_asset(self, **params) -> dict:
        return self.call("normalize_asset", **params)  # type: ignore[return-value]

    def open_blend(self, **params) -> dict:
        return self.call("open_blend", **params)  # type: ignore[return-value]

    def image_stats(self, **params) -> dict:
        return self.call("image_stats", **params)  # type: ignore[return-value]

    # --- diagnostics ----------------------------------------------------------------

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    @property
    def render_dir(self) -> Path:
        return self._render_dir

    def _tail_log(self, limit: int = 4000) -> str:
        try:
            return self._log_path.read_text()[-limit:]
        except OSError:
            return "(no log)"
