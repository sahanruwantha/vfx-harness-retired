"""The bridge client's launch/handshake/call/close logic, tested against a fake server.

No Blender: :meth:`BlenderBridge._spawn` is replaced with a stub that writes the ready-file and
returns a fake process, while a real socket server in a thread speaks the protocol. This exercises
everything except ``bpy`` itself — the live path is covered by ``python -m scene``.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from scene.bridge import BlenderBridge, BridgeError
from scene.protocol import recv_message, send_message


def test_server_dependency_boundary_is_protocol_only() -> None:
    """The in-Blender server must not import the ``scene`` package or any client-only dep.

    Blender's bundled Python lacks ``claude_agent_sdk``; importing the ``scene`` package (whose
    __init__ pulls develop→agents→SDK) or those modules would crash the server at launch. Guarded
    statically so the failure — which only reproduces inside Blender — is caught in plain CI.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "scene" / "server.py").read_text()
    import_lines = [ln.strip() for ln in source.splitlines() if ln.strip().startswith(("import ", "from "))]
    forbidden = ("scene", "develop", "agents", "claude_agent_sdk")  # client-only; absent in Blender
    offenders = [ln for ln in import_lines if any(mod in ln for mod in forbidden)]
    assert not offenders, f"server.py must stay protocol-only; found client imports: {offenders}"


class _FakeProc:
    """Stands in for the Blender subprocess: always 'alive', trivially stoppable."""

    returncode = 0

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class _FakeServer:
    """A minimal protocol-speaking server: one connection, a few canned handlers."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self.received: list[dict] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            while True:
                try:
                    request = recv_message(conn)
                except (ConnectionError, OSError):
                    break
                if request is None:
                    break
                self.received.append(request)
                command = request.get("command")
                if command == "shutdown":
                    send_message(conn, {"ok": True, "result": {"bye": True}})
                    break
                if command == "ping":
                    send_message(conn, {"ok": True, "result": {"pong": True, "blender": "fake"}})
                elif command == "run_python":
                    send_message(conn, {"ok": True, "result": {"ok": True, "stdout": "", "result": None}})
                elif command == "boom":
                    send_message(conn, {"ok": False, "error": "handler exploded"})
                else:
                    send_message(conn, {"ok": False, "error": f"unknown command: {command!r}"})
        self._sock.close()

    def join(self) -> None:
        self._thread.join(timeout=5)


def _make_bridge(tmp_path, server: _FakeServer) -> BlenderBridge:
    bridge = BlenderBridge(
        blender_bin="/nonexistent/blender",  # never launched — _spawn is stubbed
        work_dir=tmp_path / "work",
        render_dir=tmp_path / "renders",
    )

    def fake_spawn() -> _FakeProc:
        bridge._ready_file.parent.mkdir(parents=True, exist_ok=True)
        bridge._ready_file.write_text(json.dumps({"port": server.port, "pid": 4242}))
        return _FakeProc()

    bridge._spawn = fake_spawn  # type: ignore[method-assign]
    return bridge


def test_start_handshake_and_ping(tmp_path) -> None:
    server = _FakeServer()
    bridge = _make_bridge(tmp_path, server)
    try:
        bridge.start()
        assert bridge.ping() == {"pong": True, "blender": "fake"}
    finally:
        bridge.close()
    server.join()


def test_socket_uses_op_timeout_not_connect_timeout(tmp_path) -> None:
    # regression: the 5s connect timeout must NOT govern the persistent socket, or a long
    # build/render raises TimeoutError mid-command. The op_timeout owns the live socket.
    server = _FakeServer()
    bridge = _make_bridge(tmp_path, server)
    bridge._op_timeout = 321.0
    try:
        bridge.start()
        assert bridge._sock.gettimeout() == 321.0
    finally:
        bridge.close()
    server.join()


def test_run_python_roundtrip(tmp_path) -> None:
    server = _FakeServer()
    bridge = _make_bridge(tmp_path, server)
    with bridge:
        assert bridge.run_python("pass") == {"ok": True, "stdout": "", "result": None}
    assert {"command": "run_python", "params": {"code": "pass"}} in server.received


def test_transport_error_raises(tmp_path) -> None:
    server = _FakeServer()
    bridge = _make_bridge(tmp_path, server)
    with bridge:
        with pytest.raises(BridgeError, match="handler exploded"):
            bridge.call("boom")


def test_close_sends_shutdown(tmp_path) -> None:
    server = _FakeServer()
    bridge = _make_bridge(tmp_path, server)
    bridge.start()
    bridge.close()
    server.join()
    assert server.received[-1]["command"] == "shutdown"


def test_call_before_start_raises(tmp_path) -> None:
    bridge = BlenderBridge(blender_bin="/nonexistent/blender", work_dir=tmp_path / "w", render_dir=tmp_path / "r")
    with pytest.raises(BridgeError, match="not started"):
        bridge.ping()


def test_startup_timeout_when_ready_file_never_appears(tmp_path) -> None:
    bridge = BlenderBridge(
        blender_bin="/nonexistent/blender",
        work_dir=tmp_path / "w",
        render_dir=tmp_path / "r",
        startup_timeout=0.3,
    )
    bridge._spawn = lambda: _FakeProc()  # type: ignore[method-assign]  # alive but writes no ready-file
    with pytest.raises(BridgeError, match="timed out"):
        bridge.start()
